from __future__ import annotations

import sys
import types

import pytest
from pydantic import BaseModel

from config.settings import Settings
from orchestrator.budget import LLMBudget, LLMFreeLimitReached
from storage.models import TaskStatus


class _Out(BaseModel):
    value: str


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


def test_auto_mode_fails_over_to_second_model(monkeypatch):
    def fake_create(**kwargs):
        if kwargs["model"] == "model-a:free":
            return _overloaded_response()
        return _ok_response('{"value": "from-b"}')

    _install_fake_openai(monkeypatch, fake_create)
    from llm.openrouter_client import OpenRouterClient
    from llm.multi_model_client import MultiModelOpenRouterClient

    settings = _settings()
    client_a = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-a:free")
    client_b = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-b:free")
    multi = MultiModelOpenRouterClient([client_a, client_b], selection_mode="auto")
    status = TaskStatus(task_id="m1")

    result = multi.generate_structured(role="r", prompt="p", response_model=_Out, status=status)
    assert result.value == "from-b"


def test_manual_mode_does_not_fail_over(monkeypatch):
    _install_fake_openai(monkeypatch, lambda **kw: _overloaded_response())
    from llm.openrouter_client import OpenRouterClient
    from llm.multi_model_client import MultiModelOpenRouterClient

    settings = _settings()
    client_a = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-a:free")
    client_b = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-b:free")
    multi = MultiModelOpenRouterClient([client_a, client_b], selection_mode="manual")
    status = TaskStatus(task_id="m2")

    with pytest.raises(LLMFreeLimitReached, match="manual"):
        multi.generate_structured(role="r", prompt="p", response_model=_Out, status=status)


def test_all_candidates_exhausted_raises_llm_free_limit_reached(monkeypatch):
    _install_fake_openai(monkeypatch, lambda **kw: _overloaded_response())
    from llm.openrouter_client import OpenRouterClient
    from llm.multi_model_client import MultiModelOpenRouterClient

    settings = _settings()
    client_a = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-a:free")
    client_b = OpenRouterClient(settings=settings, budget=LLMBudget(10, 100, 100), model="model-b:free")
    multi = MultiModelOpenRouterClient([client_a, client_b], selection_mode="auto")
    status = TaskStatus(task_id="m3")

    with pytest.raises(LLMFreeLimitReached):
        multi.generate_structured(role="r", prompt="p", response_model=_Out, status=status)


def test_settings_parses_csv_models_from_env_style_string():
    settings = Settings(
        llm_provider="openrouter", openrouter_api_key="k",
        openrouter_planning_models="model-x:free, model-y:free",
    )
    assert settings.openrouter_planning_models == ["model-x:free", "model-y:free"]


def test_settings_default_selection_mode_is_auto():
    settings = Settings(llm_provider="openrouter", openrouter_api_key="k")
    assert settings.openrouter_selection_mode == "auto"