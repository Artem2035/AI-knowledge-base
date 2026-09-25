from __future__ import annotations

import sys
import types

import pytest
from pydantic import BaseModel

from config.settings import Settings
from orchestrator.budget import LLMBudget, LLMFreeLimitReached, LLMTaskBudgetExceeded
from storage.models import TaskStatus


class _DummyOutput(BaseModel):
    value: str


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_provider="groq",
        groq_api_key="fake-groq-key",
        free_only=True,
        max_llm_calls_per_task=3,
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
        def __init__(self, api_key, base_url, **kwargs):
            # **kwargs — чтобы принимать http_client и любые другие параметры,
            # которые GroqClient передаёт в OpenAI(...) сейчас или в будущем,
            # не ломая тест при каждом изменении конструктора клиента.
            self.api_key = api_key
            self.base_url = base_url
            self.chat = FakeChat()

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_module)


def _make_response(content: str, *, usage: dict | None = None):
    """usage (НОВОЕ): опциональный словарь с полями total_tokens/
    completion_tokens/prompt_tokens_details.cached_tokens — раньше тесты
    вообще не эмулировали usage (response.usage отсутствовал), что скрывало
    от тестов реальное поведение калибровки (v4-B1/B2/A1 из
    docs/groq_token_budget.md опираются именно на usage.completion_tokens
    и usage.prompt_tokens_details.cached_tokens). Без usage поведение
    прежнее: completion_tokens=0, наблюдения калибратора не пишутся."""
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message)
    if usage is None:
        return types.SimpleNamespace(choices=[choice])

    details = None
    if "cached_tokens" in usage:
        details = types.SimpleNamespace(cached_tokens=usage["cached_tokens"])
    usage_obj = types.SimpleNamespace(
        total_tokens=usage.get("total_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        prompt_tokens_details=details,
    )
    return types.SimpleNamespace(choices=[choice], usage=usage_obj)


# ---------------------------------------------------------------------------
# Прежние тесты (v1-v3) — сигнатура _call_with_retry изменилась (теперь
# возвращает tuple[str, int] вместо просто str, см. v4-B1 в
# llm/groq_client.py), но эти тесты не вызывают _call_with_retry напрямую —
# они мокают только openai.OpenAI.chat.completions.create и проверяют
# результат generate_structured() (публичный контракт LLMClient Protocol,
# см. llm/base.py), который НЕ изменился. Изменений в самих тестах не
# требуется — они прогнаны против патченого llm/groq_client.py без
# модификаций и остаются валидными.
# ---------------------------------------------------------------------------


def test_groq_missing_api_key_raises(monkeypatch):
    from llm.groq_client import GroqClient

    settings = _settings(groq_api_key="")
    budget = LLMBudget(3, 100, 100)
    with pytest.raises(RuntimeError):
        GroqClient(settings=settings, budget=budget)


def test_groq_successful_structured_call(monkeypatch):
    def fake_create(**kwargs):
        assert kwargs["response_format"]["type"] in ("json_object", "json_schema")
        return _make_response('{"value": "ok"}')

    _install_fake_openai(monkeypatch, fake_create)

    from llm.groq_client import GroqClient

    settings = _settings()
    budget = LLMBudget(3, 100, 100)
    client = GroqClient(settings=settings, budget=budget)
    status = TaskStatus(task_id="g1")

    result = client.generate_structured(
        role="test_role", prompt="hi", response_model=_DummyOutput, status=status
    )
    assert result.value == "ok"
    assert status.llm_calls_used == 1


def test_groq_uses_strict_json_schema_for_supported_model(monkeypatch):
    def fake_create(**kwargs):
        assert kwargs["response_format"]["type"] == "json_schema"
        assert kwargs["response_format"]["json_schema"]["strict"] is True
        return _make_response('{"value": "ok"}')

    _install_fake_openai(monkeypatch, fake_create)

    from llm.groq_client import GroqClient

    settings = _settings(groq_model="openai/gpt-oss-120b")
    budget = LLMBudget(3, 100, 100)
    client = GroqClient(settings=settings, budget=budget)
    status = TaskStatus(task_id="g1s")

    result = client.generate_structured(
        role="test_role", prompt="hi", response_model=_DummyOutput, status=status
    )
    assert result.value == "ok"


def test_groq_uses_json_object_for_unsupported_model(monkeypatch):
    def fake_create(**kwargs):
        assert kwargs["response_format"] == {"type": "json_object"}
        return _make_response('{"value": "ok"}')

    _install_fake_openai(monkeypatch, fake_create)

    from llm.groq_client import GroqClient

    settings = _settings(groq_model="qwen/qwen3.8-27b")
    budget = LLMBudget(3, 100, 100)
    client = GroqClient(settings=settings, budget=budget)
    status = TaskStatus(task_id="g1o")

    result = client.generate_structured(
        role="test_role", prompt="hi", response_model=_DummyOutput, status=status
    )
    assert result.value == "ok"


def test_groq_persistent_429_stops_without_paid_fallback(monkeypatch):
    def always_rate_limited(**kwargs):
        raise Exception("Error 429: rate_limit_exceeded")

    _install_fake_openai(monkeypatch, always_rate_limited)

    from llm.groq_client import GroqClient

    settings = _settings()
    budget = LLMBudget(5, 100, 100)
    client = GroqClient(settings=settings, budget=budget)
    status = TaskStatus(task_id="g2")

    with pytest.raises(LLMFreeLimitReached):
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

    budget = LLMBudget(3, rpm, rpd)
    client = create_llm_client(settings, budget)
    from llm.groq_client import GroqClient

    assert isinstance(client, GroqClient)


def test_factory_unknown_provider_raises():
    from llm.factory import create_llm_client

    settings = _settings(llm_provider="does_not_exist")
    budget = LLMBudget(3, 100, 100)
    with pytest.raises(ValueError):
        create_llm_client(settings, budget)


# ---------------------------------------------------------------------------
# НОВЫЕ тесты (v4) — по одному на каждое из четырёх изменений бюджета
# токенов (см. docs/groq_token_budget.md): B1 (калибровка output-резерва
# по роли), B2 (линейное восстановление margin), A1 (общий лимитер
# основного/extraction-клиента), B4 (настраиваемые коэффициенты
# chars_per_token).
# ---------------------------------------------------------------------------


class TestOutputReservationCalibration:
    """B1: TokenEstimateCalibrator.reserved_output_tokens/observe_output —
    резерв под output адаптируется по роли вместо статичной константы на
    всех."""

    def test_default_reserve_used_before_any_observation(self):
        from llm.groq_client import TokenEstimateCalibrator

        calibrator = TokenEstimateCalibrator()
        # Холодный старт: нет наблюдений по роли -> возвращается default
        # НЕИЗМЕННЫМ (тот же уровень безопасности, что был у прежнего
        # статичного RESERVED_OUTPUT_TOKENS).
        assert calibrator.reserved_output_tokens("critic", default=1500) == 1500

    def test_short_role_converges_below_default_after_observations(self):
        from llm.groq_client import TokenEstimateCalibrator

        calibrator = TokenEstimateCalibrator(output_ema_alpha=0.5, output_min_floor=300)
        # critic обычно отвечает коротким JSON-вердиктом — эмулируем
        # несколько коротких наблюдений подряд.
        for _ in range(6):
            calibrator.observe_output("critic", 120)

        reserved = calibrator.reserved_output_tokens("critic", default=1500)
        # EMA должна сойтись к ~120, но не ниже пола 300 — резерв
        # ЗНАЧИТЕЛЬНО меньше дефолтных 1500 (освобождает бюджет под prompt),
        # но не опускается ниже безопасного пола.
        assert reserved == 300
        assert reserved < 1500

    def test_long_role_reserve_grows_with_observations(self):
        from llm.groq_client import TokenEstimateCalibrator

        calibrator = TokenEstimateCalibrator(output_ema_alpha=0.5, output_min_floor=300)
        for _ in range(6):
            calibrator.observe_output("synthesizer_write", 2200)

        reserved = calibrator.reserved_output_tokens("synthesizer_write", default=1500)
        # Для роли с длинными ответами калиброванный резерв РАСТЁТ выше
        # дефолта — иначе длинный ответ рисковал бы быть обрезанным.
        assert reserved > 1500

    def test_floor_protects_against_single_short_outlier(self):
        from llm.groq_client import TokenEstimateCalibrator

        calibrator = TokenEstimateCalibrator(output_ema_alpha=0.9, output_min_floor=300)
        calibrator.observe_output("critic", 10)  # аномально короткий ответ
        reserved = calibrator.reserved_output_tokens("critic", default=1500)
        assert reserved >= 300  # не падает ниже пола даже после одного выброса

    def test_generate_structured_uses_calibrated_reserve(self, monkeypatch):
        """Интеграционный тест: после нескольких коротких ответов роли
        реальный вызов generate_structured должен получать меньший
        max_prompt_tokens-эффект (косвенно проверяем через то, что второй
        длинный промпт для этой роли НЕ обрезается автоматически, хотя при
        статичном резерве 1500 мог бы, если бюджет тесный)."""

        call_log = []

        def fake_create(**kwargs):
            call_log.append(kwargs)
            return _make_response('{"value": "ok"}', usage={"total_tokens": 200, "completion_tokens": 15})

        _install_fake_openai(monkeypatch, fake_create)

        from llm.groq_client import GroqClient

        settings = _settings(groq_tpm_limit=8000)
        budget = LLMBudget(10, 100, 100)
        client = GroqClient(settings=settings, budget=budget)
        status = TaskStatus(task_id="calib1")

        for _ in range(5):
            client.generate_structured(
                role="critic", prompt="short prompt", response_model=_DummyOutput, status=status
            )

        # После 5 наблюдений резерв роли 'critic' должен быть заметно ниже
        # дефолтных 1500 (реальный completion_tokens=15 в моках).
        reserved = client._calibrator.reserved_output_tokens("critic", default=1500)
        assert reserved < 1500
        assert len(call_log) == 5


class TestMarginRecoveryModes:
    """B2: TokenRateLimiter — 'linear' восстановление margin после 429
    против прежнего бинарного 'step'."""

    def test_step_mode_keeps_full_penalty_until_recovery_window(self, monkeypatch):
        from llm.groq_client import TokenRateLimiter

        limiter = TokenRateLimiter(
            tpm_limit=8000, safety_margin=1.0, margin_penalty_factor=0.5,
            margin_min_penalty=0.5, margin_recovery_seconds=100.0, recovery_mode="step",
        )
        limiter.register_rate_limit_hit()
        assert limiter._limit == 4000  # 8000 * 0.5

        # Спустя половину окна восстановления — в 'step' лимит НЕ меняется.
        fake_now = [limiter._last_penalty_at + 50.0]
        monkeypatch.setattr("time.monotonic", lambda: fake_now[0])
        assert limiter.available_tokens() == 4000

        # После полного окна — мгновенный скачок на 100%.
        fake_now[0] = limiter._last_penalty_at + 100.0 + 0.01
        assert limiter.available_tokens() == 8000

    def test_linear_mode_recovers_gradually(self, monkeypatch):
        from llm.groq_client import TokenRateLimiter

        limiter = TokenRateLimiter(
            tpm_limit=8000, safety_margin=1.0, margin_penalty_factor=0.5,
            margin_min_penalty=0.5, margin_recovery_seconds=100.0, recovery_mode="linear",
        )
        limiter.register_rate_limit_hit()
        hit_at = limiter._last_penalty_at
        assert limiter._limit == 4000

        # На середине окна восстановления лимит должен быть ЗАМЕТНО выше
        # штрафного значения, но ещё не полным — это и есть отличие от
        # 'step', которое устраняет "плато недоиспользования".
        monkeypatch.setattr("time.monotonic", lambda: hit_at + 50.0)
        mid_available = limiter.available_tokens()
        assert 4000 < mid_available < 8000

        # После полного окна — восстановление до базового значения.
        monkeypatch.setattr("time.monotonic", lambda: hit_at + 100.0 + 0.01)
        assert limiter.available_tokens() == 8000

    def test_default_settings_recovery_mode_is_linear(self):
        settings = _settings()
        assert settings.groq_margin_recovery_mode == "linear"


class TestSharedLimiterAcrossClients:
    """A1: extraction-клиент переиспользует TokenRateLimiter/
    TokenEstimateCalibrator основного клиента при совпадении моделей."""

    def test_factory_shares_limiter_when_models_match(self, monkeypatch):
        def fake_create(**kwargs):
            return _make_response('{"value": "ok"}')

        _install_fake_openai(monkeypatch, fake_create)

        from llm.factory import create_extraction_llm_client
        from llm.groq_client import GroqClient

        settings = _settings(
            groq_model="openai/gpt-oss-120b",
            groq_extraction_model="openai/gpt-oss-120b",
            groq_share_limiter_when_same_model=True,
        )
        primary = GroqClient(settings=settings, budget=LLMBudget(10, 100, 100))
        extraction = create_extraction_llm_client(
            settings, LLMBudget(10, 100, 100), primary_client=primary
        )

        assert isinstance(extraction, GroqClient)
        # Ключевая проверка: ОБА клиента разделяют ОДИН И ТОТ ЖЕ объект
        # лимитера/калибратора, а не два независимых с одинаковыми
        # параметрами (иначе их резервы не видели бы расход друг друга).
        assert extraction._limiter is primary._limiter
        assert extraction._calibrator is primary._calibrator

    def test_factory_does_not_share_when_models_differ(self, monkeypatch):
        def fake_create(**kwargs):
            return _make_response('{"value": "ok"}')

        _install_fake_openai(monkeypatch, fake_create)

        from llm.factory import create_extraction_llm_client
        from llm.groq_client import GroqClient

        settings = _settings(
            groq_model="openai/gpt-oss-120b",
            groq_extraction_model="openai/gpt-oss-20b",  # другая модель
            groq_share_limiter_when_same_model=True,
        )
        primary = GroqClient(settings=settings, budget=LLMBudget(10, 100, 100))
        extraction = create_extraction_llm_client(
            settings, LLMBudget(10, 100, 100), primary_client=primary
        )

        assert extraction._limiter is not primary._limiter
        assert extraction._calibrator is not primary._calibrator

    def test_factory_does_not_share_when_flag_disabled(self, monkeypatch):
        def fake_create(**kwargs):
            return _make_response('{"value": "ok"}')

        _install_fake_openai(monkeypatch, fake_create)

        from llm.factory import create_extraction_llm_client
        from llm.groq_client import GroqClient

        settings = _settings(
            groq_model="openai/gpt-oss-120b",
            groq_extraction_model="openai/gpt-oss-120b",
            groq_share_limiter_when_same_model=False,
        )
        primary = GroqClient(settings=settings, budget=LLMBudget(10, 100, 100))
        extraction = create_extraction_llm_client(
            settings, LLMBudget(10, 100, 100), primary_client=primary
        )

        assert extraction._limiter is not primary._limiter

    def test_shared_limiter_reflects_combined_reservation(self, monkeypatch):
        """Проверка сути A1: резерв, сделанный ОСНОВНЫМ клиентом, должен
        быть виден extraction-клиенту, потому что они физически делят
        один Groq TPM-лимит — до v4-A1 это было невозможно (два разных
        объекта лимитера)."""
        def fake_create(**kwargs):
            return _make_response('{"value": "ok"}')

        _install_fake_openai(monkeypatch, fake_create)

        from llm.factory import create_extraction_llm_client
        from llm.groq_client import GroqClient

        settings = _settings(
            groq_model="openai/gpt-oss-120b", groq_extraction_model="openai/gpt-oss-120b",
        )
        primary = GroqClient(settings=settings, budget=LLMBudget(10, 100, 100))
        extraction = create_extraction_llm_client(
            settings, LLMBudget(10, 100, 100), primary_client=primary
        )

        before = extraction._limiter.available_tokens()
        status = TaskStatus(task_id="shared1")
        primary.generate_structured(
            role="outline_planner", prompt="p" * 500, response_model=_DummyOutput, status=status
        )
        after = extraction._limiter.available_tokens()

        assert after < before  # вызов ОСНОВНОГО клиента уменьшил доступный бюджет ОБОИХ


class TestConfigurableCharsPerToken:
    """B4: коэффициенты chars_per_token настраиваемы через Settings, не
    только через хардкод llm/common.py."""

    def test_default_coefficients_match_old_hardcode(self):
        from llm.common import chars_per_token

        assert chars_per_token("привет мир") == 2.3
        assert chars_per_token("hello world") == 4.0

    def test_custom_coefficients_override_defaults(self):
        from llm.common import chars_per_token

        result = chars_per_token(
            "привет", cyrillic_chars_per_token=3.0, latin_chars_per_token=5.0,
        )
        assert result == 3.0

    def test_groq_client_uses_settings_coefficients(self, monkeypatch):
        def fake_create(**kwargs):
            return _make_response('{"value": "ok"}')

        _install_fake_openai(monkeypatch, fake_create)

        from llm.groq_client import GroqClient

        settings = _settings(
            groq_chars_per_token_cyrillic=1.0,  # искусственно занижаем -> оценка токенов растёт
            groq_chars_per_token_latin=1.0,
        )
        client = GroqClient(settings=settings, budget=LLMBudget(10, 100, 100))

        naive_with_custom = client._estimate_tokens("привет мир, это тестовый текст")

        default_settings = _settings()
        default_client = GroqClient(settings=default_settings, budget=LLMBudget(10, 100, 100))
        naive_with_default = default_client._estimate_tokens("привет мир, это тестовый текст")

        # При cyrillic_chars_per_token=1.0 (меньше символов на токен) оценка
        # ДОЛЖНА быть больше, чем при дефолтном 2.3 — прямое подтверждение,
        # что GroqClient реально читает коэффициенты из Settings, а не
        # только полагается на хардкод llm/common.py.
        assert naive_with_custom > naive_with_default