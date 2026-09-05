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
        gemini_api_key="fake-key-for-tests",
        free_only=True,
        max_gemini_calls_per_task=3,
        max_gemini_retries=3,
        gemini_rpm_soft_limit=100,
        gemini_rpd_soft_limit=100,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


def _install_fake_genai(monkeypatch, generate_content_impl):
    fake_models = types.SimpleNamespace(generate_content=generate_content_impl)
    fake_client_instance = types.SimpleNamespace(models=fake_models)

    class FakeClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        @property
        def models(self):
            return fake_models

    fake_genai_module = types.SimpleNamespace(Client=lambda api_key: fake_client_instance)

    fake_google_module = types.ModuleType("google")
    fake_google_module.genai = fake_genai_module
    monkeypatch.setitem(sys.modules, "google", fake_google_module)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai_module)


def test_free_only_false_is_rejected():
    settings = _settings(free_only=False)
    with pytest.raises(RuntimeError):
        settings.validate_free_only()


def test_missing_api_key_raises(monkeypatch):
    from gemini.client import GeminiClient

    settings = _settings(gemini_api_key="")
    budget = GeminiBudget(3, 100, 100)
    with pytest.raises(RuntimeError):
        GeminiClient(settings=settings, budget=budget)


def test_successful_structured_call(monkeypatch):
    def fake_generate_content(model, contents, config):
        return _FakeResponse('{"value": "ok"}')

    _install_fake_genai(monkeypatch, fake_generate_content)

    from gemini.client import GeminiClient

    settings = _settings()
    budget = GeminiBudget(3, 100, 100)
    client = GeminiClient(settings=settings, budget=budget)
    status = TaskStatus(task_id="t1")

    result = client.generate_structured(
        role="test_role", prompt="hi", response_model=_DummyOutput, status=status
    )
    assert result.value == "ok"
    assert status.gemini_calls_used == 1
    assert status.gemini_calls_log[0].ok is True


def test_task_budget_exceeded_before_calling_api(monkeypatch):
    calls = {"n": 0}

    def fake_generate_content(model, contents, config):
        calls["n"] += 1
        return _FakeResponse('{"value": "ok"}')

    _install_fake_genai(monkeypatch, fake_generate_content)

    from gemini.client import GeminiClient

    settings = _settings(max_gemini_calls_per_task=1)
    budget = GeminiBudget(1, 100, 100)
    client = GeminiClient(settings=settings, budget=budget)
    status = TaskStatus(task_id="t2")

    client.generate_structured(role="r", prompt="p", response_model=_DummyOutput, status=status)

    with pytest.raises(GeminiTaskBudgetExceeded):
        client.generate_structured(role="r", prompt="p", response_model=_DummyOutput, status=status)

    assert calls["n"] == 1  # второй вызов не должен был дойти до API вообще


def test_persistent_429_stops_without_paid_fallback(monkeypatch):
    def always_rate_limited(model, contents, config):
        raise Exception("429 RESOURCE_EXHAUSTED: quota exceeded")

    _install_fake_genai(monkeypatch, always_rate_limited)

    from gemini.client import GeminiClient

    settings = _settings()
    budget = GeminiBudget(5, 100, 100)
    client = GeminiClient(settings=settings, budget=budget)
    status = TaskStatus(task_id="t3")

    with pytest.raises(GeminiFreeLimitReached):
        client.generate_structured(role="r", prompt="p", response_model=_DummyOutput, status=status)

    assert status.gemini_calls_log[-1].ok is False


def test_rpd_soft_limit_stops_task():
    budget = GeminiBudget(max_calls_per_task=100, rpm_soft_limit=100, rpd_soft_limit=1)
    status = TaskStatus(task_id="t4")
    budget.register_call(status, role="r", ok=True)
    with pytest.raises(GeminiFreeLimitReached):
        budget.check_rpd_soft_limit()
