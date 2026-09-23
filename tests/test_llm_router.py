from __future__ import annotations

import sys
import types

import pytest
from pydantic import BaseModel

from config.settings import Settings
from llm.router import RoleRoutingLLMClient
from orchestrator.budget import LLMBudget, LLMFreeLimitReached
from storage.models import TaskStatus


class _Out(BaseModel):
    value: str


# ---------------------------------------------------------------------------
# Часть 1 — простой роутинг по роли (fake-клиенты без сети), прежние тесты
# test_llm_router.py, без изменений в поведении.
# ---------------------------------------------------------------------------


class _FakeClient:
    """Мок LLMClient (см. llm/base.py::LLMClient Protocol) — просто
    помечает, каким тегом он был вызван, и по каким ролям."""

    def __init__(self, tag: str):
        self.tag = tag
        self.calls: list[str] = []

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        self.calls.append(role)
        return response_model(value=self.tag)


def test_router_sends_mapped_role_to_specific_client():
    """Регрессия для основного сценария: synthesizer_write должен уходить
    именно на writing-клиент (Gemma), а не на дефолтный (Nemotron)."""
    planning = _FakeClient("nemotron")
    writing = _FakeClient("gemma")
    router = RoleRoutingLLMClient(planning, {"synthesizer_write": writing})
    status = TaskStatus(task_id="t1")

    out = router.generate_structured(
        role="synthesizer_write", prompt="p", response_model=_Out, status=status,
    )

    assert out.value == "gemma"
    assert writing.calls == ["synthesizer_write"]
    assert planning.calls == []


def test_router_falls_back_to_default_for_unmapped_role():
    """outline_planner/elaborator/critic/vault_dedup/folder_assignment —
    всё, что не 'synthesizer_write', должно уходить на дефолтный
    (planning) клиент."""
    planning = _FakeClient("nemotron")
    writing = _FakeClient("gemma")
    router = RoleRoutingLLMClient(planning, {"synthesizer_write": writing})
    status = TaskStatus(task_id="t2")

    for role in ("outline_planner", "elaborator", "critic", "vault_dedup", "folder_assignment"):
        out = router.generate_structured(
            role=role, prompt="p", response_model=_Out, status=status,
        )
        assert out.value == "nemotron"

    assert planning.calls == ["outline_planner", "elaborator", "critic", "vault_dedup", "folder_assignment"]
    assert writing.calls == []


def test_router_with_empty_role_map_always_uses_default():
    planning = _FakeClient("nemotron")
    router = RoleRoutingLLMClient(planning)
    status = TaskStatus(task_id="t3")

    router.generate_structured(role="critic", prompt="p", response_model=_Out, status=status)

    assert planning.calls == ["critic"]


def test_router_available_prompt_budget_tokens_delegates_to_default_when_present():
    class _WithBudget(_FakeClient):
        def available_prompt_budget_tokens(self, system_instruction, response_model):
            return 4242

    router = RoleRoutingLLMClient(_WithBudget("nemotron"))
    assert router.available_prompt_budget_tokens("sys", _Out) == 4242


def test_router_available_prompt_budget_tokens_returns_none_when_default_lacks_it():
    router = RoleRoutingLLMClient(_FakeClient("nemotron"))
    assert router.available_prompt_budget_tokens("sys", _Out) is None


def test_router_available_prompt_budget_tokens_uses_first_candidate_of_list_default():
    """Регрессия для бага, из-за которого раньше (в отдельном
    MultiModelOpenRouterClient) available_prompt_budget_tokens всегда
    возвращал None при СПИСКЕ кандидатов в default — getattr делался на
    сам список, а не на конкретного клиента (у list нет такого метода).
    Дальше это роняло llm/chunking.py::split_items_into_batches с
    TypeError на `None - overhead_tokens`."""

    class _WithBudget(_FakeClient):
        def available_prompt_budget_tokens(self, system_instruction, response_model):
            return 999

    router = RoleRoutingLLMClient([_WithBudget("a"), _FakeClient("b")])
    assert router.available_prompt_budget_tokens("sys", _Out) == 999


# ---------------------------------------------------------------------------
# Часть 2 — failover внутри группы кандидатов роли, перенесено из бывшего
# tests/test_multi_model_openrouter.py (класс MultiModelOpenRouterClient
# слит в RoleRoutingLLMClient, см. llm/router.py). Интеграционные тесты
# через реальный OpenRouterClient + фейковый пакет openai.
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_provider="openrouter", openrouter_api_key="fake-key", free_only=True,
        max_llm_calls_per_task=10,
        openrouter_planning_rpm_soft_limit=100, openrouter_planning_rpd_soft_limit=100,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _install_fake_openai(monkeypatch, create_impl):
    class FakeCompletions:
        def create(self, **kwargs):
            return create_impl(**kwargs)

    class FakeChat:
        def __init__(self):
            self.completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, api_key, base_url, **kwargs):
            self.chat = FakeChat()

    # --- Заглушки исключений, которые ждёт production-код ---
    class APIError(Exception):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIError):
        pass

    class APIStatusError(APIError):
        def __init__(self, message="", *, response=None, body=None):
            super().__init__(message)
            self.response = response
            self.status_code = getattr(response, "status_code", None)

    class RateLimitError(APIStatusError):
        pass

    class AuthenticationError(APIStatusError):
        pass

    class BadRequestError(APIStatusError):
        pass

    class NotFoundError(APIStatusError):
        pass

    class InternalServerError(APIStatusError):
        pass

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = FakeOpenAI
    fake_module.APIError = APIError
    fake_module.APIConnectionError = APIConnectionError
    fake_module.APITimeoutError = APITimeoutError
    fake_module.APIStatusError = APIStatusError
    fake_module.RateLimitError = RateLimitError
    fake_module.AuthenticationError = AuthenticationError
    fake_module.BadRequestError = BadRequestError
    fake_module.NotFoundError = NotFoundError
    fake_module.InternalServerError = InternalServerError

    monkeypatch.setitem(sys.modules, "openai", fake_module)


def _ok_response(content: str):
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message)
    return types.SimpleNamespace(choices=[choice], error=None)


def _overloaded_response():
    return types.SimpleNamespace(
        choices=None,
        error={"message": "Service temporarily overloaded", "code": 503,
               "metadata": {"error_type": "provider_overloaded"}},
    )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


def test_auto_mode_fails_over_to_second_candidate(monkeypatch):
    def fake_create(**kwargs):
        if kwargs["model"] == "model-a:free":
            return _overloaded_response()
        return _ok_response('{"value": "from-b"}')

    _install_fake_openai(monkeypatch, fake_create)
    from llm.openrouter_client import OpenRouterClient

    settings = _settings()
    client_a = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-a:free")
    client_b = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-b:free")
    router = RoleRoutingLLMClient([client_a, client_b], selection_mode="auto")
    status = TaskStatus(task_id="m1")

    result = router.generate_structured(role="r", prompt="p", response_model=_Out, status=status)
    assert result.value == "from-b"


def test_manual_mode_does_not_fail_over(monkeypatch):
    _install_fake_openai(monkeypatch, lambda **kw: _overloaded_response())
    from llm.openrouter_client import OpenRouterClient

    settings = _settings()
    client_a = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-a:free")
    client_b = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-b:free")
    router = RoleRoutingLLMClient([client_a, client_b], selection_mode="manual")
    status = TaskStatus(task_id="m2")

    with pytest.raises(LLMFreeLimitReached, match="manual"):
        router.generate_structured(role="r", prompt="p", response_model=_Out, status=status)


def test_all_candidates_exhausted_raises_llm_free_limit_reached(monkeypatch):
    _install_fake_openai(monkeypatch, lambda **kw: _overloaded_response())
    from llm.openrouter_client import OpenRouterClient

    settings = _settings()
    client_a = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-a:free")
    client_b = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-b:free")
    router = RoleRoutingLLMClient([client_a, client_b], selection_mode="auto")
    status = TaskStatus(task_id="m3")

    with pytest.raises(LLMFreeLimitReached):
        router.generate_structured(role="r", prompt="p", response_model=_Out, status=status)


def test_settings_parses_csv_models_from_env_style_string():
    settings = Settings(
        llm_provider="openrouter", openrouter_api_key="k",
        openrouter_planning_models="model-x:free, model-y:free",
    )
    assert settings.openrouter_planning_models == ["model-x:free", "model-y:free"]


def test_settings_default_selection_mode_is_auto():
    settings = Settings(llm_provider="openrouter", openrouter_api_key="k")
    assert settings.openrouter_selection_mode == "auto"