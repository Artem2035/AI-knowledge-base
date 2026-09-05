from __future__ import annotations

import sys
import types

import pytest
from pydantic import BaseModel

from config.settings import Settings
from orchestrator.budget import GeminiBudget, GeminiFreeLimitReached, GeminiTaskBudgetExceeded
from storage.models import TaskStatus


class _DummyOutput(BaseModel):
    value: str


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_provider="groq",
        groq_api_key="fake-groq-key",
        free_only=True,
        max_gemini_calls_per_task=3,
        groq_rpm_soft_limit=100,
        groq_rpd_soft_limit=100,
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
        def __init__(self, api_key, base_url):
            self.api_key = api_key
            self.base_url = base_url
            self.chat = FakeChat()

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_module)


def _make_response(content: str):
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message)
    return types.SimpleNamespace(choices=[choice])


def test_groq_missing_api_key_raises(monkeypatch):
    from llm.groq_client import GroqClient

    settings = _settings(groq_api_key="")
    budget = GeminiBudget(3, 100, 100)
    with pytest.raises(RuntimeError):
        GroqClient(settings=settings, budget=budget)


def test_groq_successful_structured_call(monkeypatch):
    def fake_create(**kwargs):
        assert kwargs["response_format"] == {"type": "json_object"}
        return _make_response('{"value": "ok"}')

    _install_fake_openai(monkeypatch, fake_create)

    from llm.groq_client import GroqClient

    settings = _settings()
    budget = GeminiBudget(3, 100, 100)
    client = GroqClient(settings=settings, budget=budget)
    status = TaskStatus(task_id="g1")

    result = client.generate_structured(
        role="test_role", prompt="hi", response_model=_DummyOutput, status=status
    )
    assert result.value == "ok"
    assert status.gemini_calls_used == 1


def test_groq_persistent_429_stops_without_paid_fallback(monkeypatch):
    def always_rate_limited(**kwargs):
        raise Exception("Error 429: rate_limit_exceeded")

    _install_fake_openai(monkeypatch, always_rate_limited)

    from llm.groq_client import GroqClient

    settings = _settings()
    budget = GeminiBudget(5, 100, 100)
    client = GroqClient(settings=settings, budget=budget)
    status = TaskStatus(task_id="g2")

    with pytest.raises(GeminiFreeLimitReached):
        client.generate_structured(role="r", prompt="p", response_model=_DummyOutput, status=status)


def test_factory_selects_groq(monkeypatch):
    def fake_create(**kwargs):
        return _make_response('{"value": "ok"}')

    _install_fake_openai(monkeypatch, fake_create)

    from llm.factory import budget_limits_for_provider, create_llm_client

    settings = _settings(llm_provider="groq")
    rpm, rpd = budget_limits_for_provider(settings)
    assert rpm == settings.groq_rpm_soft_limit
    assert rpd == settings.groq_rpd_soft_limit

    budget = GeminiBudget(3, rpm, rpd)
    client = create_llm_client(settings, budget)
    from llm.groq_client import GroqClient

    assert isinstance(client, GroqClient)


def test_factory_unknown_provider_raises():
    from llm.factory import create_llm_client

    settings = _settings(llm_provider="does_not_exist")
    budget = GeminiBudget(3, 100, 100)
    with pytest.raises(ValueError):
        create_llm_client(settings, budget)
