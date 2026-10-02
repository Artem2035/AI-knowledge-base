"""
GroqClient — реализует ТОТ ЖЕ публичный контракт (llm/base.py::LLMClient
Protocol), что и любой другой провайдер, добавленный в будущем (см.
llm/factory.py).

См. докстринг оригинальной версии (v1-v3) для истории TokenRateLimiter/
TokenEstimateCalibrator/prompt caching — сохранён без изменений ниже.
Этот файл добавляет ЧЕТВЁРТУЮ волну изменений (v4), по итогам разбора
"docs/groq_token_budget.md" (голодание vs недоиспользование бюджета):

v4-A1: Общий TokenRateLimiter/TokenEstimateCalibrator между основным
  клиентом (self.llm) и extraction-клиентом (self.extraction_client),
  когда они используют одну и ту же модель (дефолт: оба —
  "openai/gpt-oss-120b"). Раньше это были два независимых объекта в
  памяти процесса, каждый со своим представлением о "текущем расходе
  TPM" — но физически они делят ОДИН лимит Groq API. Сумма двух
  независимых резервов могла превысить реальный лимит без единого
  предупреждения ни в одном из логов по отдельности (риск голодания,
  невидимый при диагностике одного клиента). См.
  llm/factory.py::create_extraction_llm_client — точка, где решается,
  передавать ли shared_limiter/shared_calibrator.

v4-B1: Резерв под OUTPUT-токены (RESERVED_OUTPUT_TOKENS) больше не
  единая константа на ВСЕ роли. TokenEstimateCalibrator дополнен
  ОТДЕЛЬНЫМ EMA по роли для реального completion_tokens (не путать с
  ratio-калибровкой prompt-оценки, см. ниже) — короткие роли (critic,
  vault_dedup, folder_assignment) быстро получают маленький, честный
  резерв вместо статичных 1500, освобождая место под prompt/батчи.
  Значение по умолчанию для "холодного старта" роли (нет наблюдений)
  ПО-ПРЕЖНЕМУ 1500 — тот же уровень безопасности, что был всегда,
  меняется только поведение ПОСЛЕ первого наблюдения.

v4-B2: TokenRateLimiter поддерживает "linear" восстановление margin
  после 429 (см. Settings.groq_margin_recovery_mode) — вместо резкой
  ступеньки "100% штрафа все 300с, затем мгновенно 0%" margin плавно
  растёт пропорционально прошедшему времени. Дефолт изменён на
  "linear" — это и есть основная причина эффекта "система берёт
  2000-3000 из 8000, хотя лимит 8000": пока действовал 300-секундный
  таймер после ЛЮБОЙ (даже единичной) 429, бюджет был занижен ПОЛНОСТЬЮ
  всё это время.

v4-B4: Коэффициенты chars_per_token читаются из Settings
  (groq_chars_per_token_cyrillic/latin/groq_cyrillic_ratio_threshold),
  а не только из хардкода llm/common.py — позволяет пересчитать точность
  наивной оценки под реальные промпты проекта без правки кода.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from config.settings import Settings
from llm.common import (LLMRateLimitError, LLMSchemaError, LLMPromptTooLargeError,
                        LLMSchemaError,
                        is_rate_limit_error,
                        is_request_too_large_error,
                        parse_retry_after,
                        repair_json, estimate_tokens, chars_per_token)

from orchestrator.budget import LLMBudget, LLMFreeLimitReached
from storage.models import TaskStatus

import httpx
from openai import APIConnectionError, APITimeoutError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

class GroqRateLimitError(LLMRateLimitError):
    """Оборачивает 429 от Groq API для retry-логики tenacity."""


class GroqSchemaError(LLMSchemaError):
    """JSON от модели невалиден даже после repair-попыток."""


class GroqPromptTooLargeError(LLMPromptTooLargeError):
    """Промпт+система+ожидаемый output превышают доступный TPM-бюджет."""

_STRICT_SCHEMA_SUPPORTED_MODELS = {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}
# Модели, у которых Groq принимает reasoning_effort (low/medium/high) и
# include_reasoning. Это ОТДЕЛЬНАЯ возможность от strict json_schema:
# множества сейчас совпадают случайно и могут разойтись. У других
# reasoning-моделей значения effort другие (напр. none/default), поэтому
# для них параметры не передаём вообще.
_REASONING_EFFORT_SUPPORTED_MODELS = {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}

# Уровень рассуждений по роли. Роли, которых здесь нет, идут с default
# модели (параметр не передаётся) — напр. outline_planner: один вызов на
# задачу, качество плана важнее экономии.
_REASONING_EFFORT_BY_ROLE: dict[str, str] = {
    "folder_assignment": "low",
    "vault_dedup": "low",
    "critic": "low",
    "synthesizer_write": "medium",
    "elaborator": "medium",
}

def _to_strict_json_schema(schema: dict) -> dict:
    """Рекурсивно приводит JSON Schema из Pydantic model_json_schema() к виду,
    требуемому Groq strict-режимом: у каждого object-узла additionalProperties=False
    и required содержит ВСЕ ключи properties (Groq strict не поддерживает частично
    опциональные объекты — поля с default в Pydantic всё равно будут возвращены
    моделью, это не мешает валидации, т.к. Pydantic просто примет присланное
    значение). Рекурсия проходит properties, items (массивы), $defs/definitions
    (вложенные Pydantic-модели) и ветки anyOf/oneOf/allOf (Optional[...] в
    Pydantic v2 компилируется в anyOf с веткой {"type": "null"})."""
    schema = dict(schema)

    for defs_key in ("$defs", "definitions"):
        if defs_key in schema:
            schema[defs_key] = {k: _to_strict_json_schema(v) for k, v in schema[defs_key].items()}

    if schema.get("type") == "object" and "properties" in schema:
        schema["properties"] = {
            k: _to_strict_json_schema(v) for k, v in schema["properties"].items()
        }
        schema["additionalProperties"] = False
        schema["required"] = list(schema["properties"].keys())

    if "items" in schema:
        schema["items"] = _to_strict_json_schema(schema["items"])

    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema:
            schema[combinator] = [_to_strict_json_schema(s) for s in schema[combinator]]

    return schema


def _is_schema_unsupported_error(exc: Exception) -> bool:
    """Отличает 'схема отклонена API' (invalid_request/unsupported feature —
        см. известные проблемы gpt-oss-120b с regex/e164 в JSON Schema) от
        остальных ошибок — на эту категорию имеет смысл ОДНОКРАТНО откатиться на
        json_object в рамках того же вызова, а не ронять всю задачу."""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "unsupported_feature",
            "invalid json schema",
            "response_format",
            "json_validate_failed",
            "does not validate",
            "missing properties",
        )
    )

def _is_reasoning_param_error(exc: Exception) -> bool:
        """API отклонил reasoning_effort/include_reasoning (напр. значение не
        поддерживается моделью). Оптимизация не должна ронять вызов."""
        text = str(exc).lower()
        return any(m in text for m in ("reasoning_effort", "include_reasoning", "reasoning_format"))

# Отдельное множество: токенайзер o200k корректен только для gpt-oss,
# даже если другие модели позже получат strict json_schema.
_TIKTOKEN_SUPPORTED_MODELS = {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}

_REQUEST_OVERHEAD_TOKENS = 120
def _try_load_tiktoken_encoding():
    """Пытается загрузить кодировку gpt-oss. Любой сбой (нет пакета, нет
    сети для первой загрузки словаря) — не фатален: возвращаем None, и
    клиент работает на посимвольной эвристике, как раньше."""
    try:
        import tiktoken
    except ImportError:
        logger.warning("tiktoken не установлен — оценка токенов по эвристике.")
        return None
    for name in ("o200k_harmony", "o200k_base"):
        try:
            return tiktoken.get_encoding(name)
        except Exception as exc:
            logger.debug("tiktoken: кодировка %s недоступна: %s", name, exc)
    logger.warning("tiktoken: ни одна кодировка не загрузилась — используем эвристику.")
    return None


class TokenEstimateCalibrator:
    """Адаптивная калибровка "наивной" оценки токенов ОТДЕЛЬНО ПО КАЖДОЙ РОЛИ.

    v4-B1: помимо прежней ratio-калибровки ВХОДНОГО (prompt) расхода,
    теперь дополнительно ведётся ОТДЕЛЬНАЯ EMA по РЕАЛЬНОМУ количеству
    OUTPUT-токенов (completion_tokens) на роль — используется вместо
    единой статичной константы RESERVED_OUTPUT_TOKENS.

    Это ДВЕ РАЗНЫЕ калибровки с разной природой:
    - prompt-ratio (_ratio_by_role) — БЕЗРАЗМЕРНЫЙ коэффициент "во сколько
      раз наивная оценка ошиблась", применяется КАК МНОЖИТЕЛЬ к новой
      наивной оценке (see .correct()). Корректно работает как отношение,
      т.к. наивная оценка и факт растут пропорционально размеру текста.
    - output-EMA (_output_ema_by_role) — АБСОЛЮТНОЕ число токенов
      (completion_tokens), НЕ соотношение к какой-либо "наивной" величине
      (у нас нет наивной оценки длины ответа ДО его генерации — это и есть
      весь смысл резервирования места под output). EMA здесь усредняет
      сам facts, а не ratio.
    Поэтому это два отдельных словаря и два отдельных публичных метода,
    а не переиспользование одного и того же механизма под разные единицы
    измерения — смешение привело бы к концептуальной ошибке (умножать
    константу-резерв на "во сколько раз ошиблись во входе" бессмысленно).
    """

    def __init__(
        self,
        ema_alpha: float = 0.3,
        min_ratio: float = 0.05,
        max_ratio: float = 1.5,
        *,
        output_ema_alpha: float = 0.3,
        output_min_floor: int = 300,
    ) -> None:
        # ema_alpha — вес нового наблюдения; выше — быстрее адаптация, но
        # шумнее. min_ratio/max_ratio — не даём коэффициенту улетать в
        # крайности от одного нетипичного вызова (например, первый вызов
        # роли в задаче — заведомо без кэша, cache_ratio=0, но это не
        # значит, что и ВСЕ следующие вызовы будут без кэша).
        # Значения читаются GroqClient из config/settings.py
        # (groq_calibration_ema_alpha/min_ratio/max_ratio).
        self._ema_alpha = ema_alpha
        self._min_ratio = min_ratio
        self._max_ratio = max_ratio
        self._ratio_by_role: dict[str, float] = {}

        # -- v4-B1: калибровка OUTPUT-резерва --
        self._output_ema_alpha = output_ema_alpha
        self._output_min_floor = output_min_floor
        self._output_ema_by_role: dict[str, float] = {}

        self._lock = threading.Lock()

    # -- prompt-оценка (без изменений в логике, только докстринг выше) --

    def correct(self, role: str, naive_estimate: int) -> int:
        """Возвращает скорректированную оценку. Пока нет ни одного
                наблюдения по роли — возвращает наивную оценку как есть (безопасный
                дефолт, эквивалентный прежнему поведению до внедрения калибровки)."""
        with self._lock:
            ratio = self._ratio_by_role.get(role)
        if ratio is None:
            return naive_estimate
        return max(int(naive_estimate * ratio), 1)

    def observe(self, role: str, naive_estimate: int, actual_effective_tokens: int) -> None:
        """actual_effective_tokens — уже ПОСЛЕ вычета cached_tokens (см.
               GroqClient._call_with_retry) — то есть то, что реально стоило
               роли по TPM-бюджету, а не то, что было формально в prompt_tokens."""
        if naive_estimate <= 0:
            return
        sample_ratio = actual_effective_tokens / naive_estimate
        sample_ratio = max(min(sample_ratio, self._max_ratio), self._min_ratio)
        with self._lock:
            prev = self._ratio_by_role.get(role)
            new_ratio = (
                sample_ratio if prev is None
                else self._ema_alpha * sample_ratio + (1 - self._ema_alpha) * prev
            )
            self._ratio_by_role[role] = new_ratio
            logger.debug(
                "TokenEstimateCalibrator[%s]: наблюдение ratio=%.3f -> EMA=%.3f "
                "(naive=%d, effective=%d)",
                role, sample_ratio, new_ratio, naive_estimate, actual_effective_tokens,
            )

    # -- v4-B1: калибровка OUTPUT-резерва --

    def reserved_output_tokens(self, role: str, default: int) -> int:
        """Сколько токенов резервировать под ОТВЕТ модели для данной роли.

        Пока нет ни одного наблюдения по роли — возвращает `default`
        (в проекте: settings.groq_reserved_output_tokens_default, тот же
        уровень безопасности, что был у статичного RESERVED_OUTPUT_TOKENS
        раньше). После первого наблюдения — EMA реального completion_tokens
        этой роли, но не ниже output_min_floor (см. __init__) — защита от
        того, чтобы серия аномально коротких ответов не обнулила резерв
        для следующего, потенциально более длинного вызова той же роли.
        """
        with self._lock:
            ema = self._output_ema_by_role.get(role)
        if ema is None:
            return default
        return max(int(ema), self._output_min_floor)

    def observe_output(self, role: str, actual_completion_tokens: int) -> None:
        if actual_completion_tokens <= 0:
            return
        with self._lock:
            prev = self._output_ema_by_role.get(role)
            new_ema = (
                float(actual_completion_tokens) if prev is None
                else self._output_ema_alpha * actual_completion_tokens
                     + (1 - self._output_ema_alpha) * prev
            )
            self._output_ema_by_role[role] = new_ema
            logger.debug(
                "TokenEstimateCalibrator[%s]: output-наблюдение completion=%d -> EMA=%.1f",
                role, actual_completion_tokens, new_ema,
            )

class CacheObservability:
    """отслеживает ТОЛЬКО бинарный факт (был кэш-хит / не был) по
    каждой роли — для диагностики и логов. НИКОГДА не используется в
    расчёте бюджета."""

    def __init__(self) -> None:
        self._calls_by_role: dict[str, int] = {}
        self._hits_by_role: dict[str, int] = {}
        self._lock = threading.Lock()

    def observe(self, role: str, cache_hit: bool) -> None:
        with self._lock:
            self._calls_by_role[role] = self._calls_by_role.get(role, 0) + 1
            if cache_hit:
                self._hits_by_role[role] = self._hits_by_role.get(role, 0) + 1

    def hit_rate(self, role: str) -> float | None:
        with self._lock:
            calls = self._calls_by_role.get(role, 0)
            if calls == 0:
                return None
            return self._hits_by_role.get(role, 0) / calls

    def summary(self) -> dict[str, tuple[int, int]]:
        with self._lock:
            return {role: (self._hits_by_role.get(role, 0), calls)
                     for role, calls in self._calls_by_role.items()}

class TokenRateLimiter:
    """Клиентский лимитер по токенам в минуту (TPM).

    Окно ФИКСИРОВАННОЕ, привязанное к календарной минуте (как считает
    Groq Console): лимит действует в интервале [hh:mm:00, hh:mm+1:00) и
    полностью сбрасывается на границе минуты. Расход, накопленный в
    предыдущей минуте, в новую не переносится.

    Два механизма:
    1. Фиксированное минутное окно — сумма зарезервированных токенов в
       ТЕКУЩЕЙ календарной минуте не превышает self._limit. Если места
       нет — ждём начала следующей минуты (+ небольшой запас на рассинхрон
       часов с сервером Groq).
    2. Adaptive safety margin — register_rate_limit_hit() ужимает
       эффективный лимит после РЕАЛЬНОГО 429, затем лимит восстанавливается
       ("linear" или "step") за margin_recovery_seconds.

    Замечание: запрос, зарезервированный в конце минуты, Groq может
    засчитать уже в следующую (сетевая задержка). boundary_margin_seconds
    и safety_margin частично компенсируют это, но не гарантируют.
    """

    def __init__(
        self,
        tpm_limit: int,
        safety_margin: float = 0.85,
        margin_penalty_factor: float = 0.8,
        margin_min_penalty: float = 0.5,
        margin_recovery_seconds: float = 300.0,
        recovery_mode: str = "linear",
        boundary_margin_seconds: float = 0.5,
    ):
        self._base_limit = max(int(tpm_limit * safety_margin), 1)
        self._limit = self._base_limit
        self._lock = threading.RLock()

        # Состояние фиксированного окна: номер календарной минуты
        # (int(unix_time // 60)) и сколько токенов в ней уже зарезервировано.
        self._window_idx: int = int(time.time() // 60)
        self._used: int = 0
        # (номер минуты, размер резерва) последней резервации — нужна для
        # adjust_last_reservation(). Если минута уже сменилась, поправку
        # применять некуда: тот расход давно обнулён.
        self._last_reservation: tuple[int, int] | None = None
        # Запас после границы минуты: часы могут слегка расходиться с
        # серверными, лучше подождать чуть дольше, чем поймать 429.
        self._boundary_margin = boundary_margin_seconds

        self._margin_penalty = 1.0
        self._penalty_value_at_last_hit: float = 1.0
        self._last_penalty_at: float | None = None
        self._recovery_after_seconds = margin_recovery_seconds
        self._penalty_factor = margin_penalty_factor
        self._min_penalty = margin_min_penalty
        self._recovery_mode = recovery_mode if recovery_mode in ("step", "linear") else "linear"
    # -- adaptive safety margin --------------------------------

    def register_rate_limit_hit(self) -> None:
        """Вызывается GroqClient сразу после РЕАЛЬНОГО 429 от API (не после
        локального throttle внутри этого же лимитера) — ужимает эффективный
        TPM-лимит, чтобы не наступать на те же грабли повторно в рамках
        этой же сессии/задачи."""
        with self._lock:
            self._margin_penalty = max(self._margin_penalty * self._penalty_factor, self._min_penalty)
            self._penalty_value_at_last_hit = self._margin_penalty
            self._last_penalty_at = time.monotonic()
            self._limit = max(int(self._base_limit * self._margin_penalty), 1)
            logger.warning(
                "TokenRateLimiter: получен реальный 429 от Groq — эффективный "
                "TPM-лимит ужат до %d (%.0f%% от базового %d), режим "
                "восстановления='%s', полное восстановление через %.0fс без "
                "новых 429.",
                self._limit, 100 * self._margin_penalty, self._base_limit,
                self._recovery_mode, self._recovery_after_seconds,
            )

    def _maybe_recover_margin(self, now: float) -> None:
        if self._margin_penalty >= 1.0 or self._last_penalty_at is None:
            return

        elapsed = now - self._last_penalty_at

        if self._recovery_mode == "step":
            # Прежнее поведение (v1-v3): бинарное восстановление.
            if elapsed >= self._recovery_after_seconds:
                self._margin_penalty = 1.0
                self._limit = self._base_limit
                self._last_penalty_at = None
                logger.info(
                    "TokenRateLimiter: TPM-лимит восстановлен до базового "
                    "значения %d (режим='step', нет 429 последние %.0fс).",
                    self._limit, self._recovery_after_seconds,
                )
            return

        # recovery_mode == "linear" (v4-B2, новый дефолт):
        if self._recovery_after_seconds <= 0:
            progress = 1.0
        else:
            progress = min(elapsed / self._recovery_after_seconds, 1.0)

        start = self._penalty_value_at_last_hit
        self._margin_penalty = start + (1.0 - start) * progress
        self._limit = max(int(self._base_limit * self._margin_penalty), 1)

        if progress >= 1.0:
            self._margin_penalty = 1.0
            self._limit = self._base_limit
            self._last_penalty_at = None
            logger.info(
                "TokenRateLimiter: TPM-лимит полностью восстановлен до %d "
                "(режим='linear', %.0fс без новых 429).",
                self._limit, self._recovery_after_seconds,
            )

    # -- фиксированное минутное окно ---------------------------------------

    def _roll_window(self, wall_now: float) -> None:
        """Если началась новая календарная минута — обнуляем расход.
        Вызывать только под self._lock."""
        idx = int(wall_now // 60)
        if idx != self._window_idx:
            self._window_idx = idx
            self._used = 0

    def available_tokens(self) -> int:
        with self._lock:
            self._maybe_recover_margin(time.monotonic())
            self._roll_window(time.time())
            return max(self._limit - self._used, 0)

    def wait_and_reserve(self, estimated_tokens: int) -> None:
        """Блокирует поток, пока в ТЕКУЩЕЙ календарной минуте не появится
        место, и резервирует его оптимистично (по оценке); реальный расход
        подменяет adjust_last_reservation().

        Сон происходит ВНЕ блокировки: пока один поток ждёт следующей
        минуты, другие клиенты с общим лимитером (available_tokens,
        adjust_last_reservation, register_rate_limit_hit) не блокируются.
        Спим кусками до 5 с, чтобы заново оценивать _limit — он может
        вырасти за счёт восстановления margin."""
        already_logged = False
        while True:
            with self._lock:
                wall_now = time.time()
                self._maybe_recover_margin(time.monotonic())
                self._roll_window(wall_now)

                fits = self._used + estimated_tokens <= self._limit
                # Запрос больше всего лимита никогда не поместится даже в
                # пустую минуту — пропускаем его в пустое окно, иначе
                # вечное ожидание. Реальный отказ, если он будет, придёт
                # от API как 413/429 и обработается клиентом.
                oversized_but_window_empty = self._used == 0 and estimated_tokens > self._limit

                if fits or oversized_but_window_empty:
                    if oversized_but_window_empty:
                        logger.warning(
                            "TokenRateLimiter: запрос (~%d токенов) больше "
                            "лимита минуты (%d) — пропускаем в пустое окно.",
                            estimated_tokens, self._limit,
                        )
                    self._used += estimated_tokens
                    self._last_reservation = (self._window_idx, estimated_tokens)
                    return

                next_minute_start = (self._window_idx + 1) * 60
                sleep_for = max(next_minute_start - wall_now + self._boundary_margin, 0.2)
                if not already_logged:
                    logger.info(
                        "TokenRateLimiter: ждём начала следующей минуты %.1fs "
                        "(used=%d, limit=%d, need=%d, margin=%.0f%%)",
                        sleep_for, self._used, self._limit, estimated_tokens,
                        100 * self._margin_penalty,
                    )
                    already_logged = True
            time.sleep(min(sleep_for, 5.0))

    def adjust_last_reservation(self, actual_tokens: int) -> None:
        """Заменяет оценочный резерв последней резервации фактическим
        расходом (за вычетом закэшированных токенов, см.
        GroqClient._call_with_retry). Если минута резервации уже закончилась,
        поправка не применяется: расход той минуты обнулён."""
        with self._lock:
            if self._last_reservation is None:
                return
            idx, reserved = self._last_reservation
            self._roll_window(time.time())
            if idx != self._window_idx:
                return
            self._used = max(self._used - reserved + actual_tokens, 0)
            self._last_reservation = (idx, actual_tokens)

    def capacity_tokens(self) -> int:
        """Полная ёмкость календарной минуты с учётом текущего штрафа margin
        (БЕЗ вычета уже потраченного в этой минуте).

        Нужна для планирования размера батчей: остаток текущей минуты
        (available_tokens) в конце минуты может быть близок к нулю, хотя
        wait_and_reserve() дождётся следующей минуты и полный лимит снова
        станет доступен. Размер батча должен зависеть от того, что влезет
        в ПУСТУЮ минуту, а не от того, что осталось прямо сейчас.
        """
        with self._lock:
            self._maybe_recover_margin(time.monotonic())
            return self._limit


class GroqClient:
    DEFAULT_TPM_LIMIT = 8000
    # Оставлен как class-level fallback для обратной совместимости (напр.
    # существующие тесты/код могут ссылаться на GroqClient.RESERVED_OUTPUT_TOKENS
    # напрямую). С v4-B1 РЕАЛЬНО используемое значение резерва теперь
    # приходит из settings.groq_reserved_output_tokens_default (см.
    # __init__) и далее КОРРЕКТИРУЕТСЯ по роли через
    # TokenEstimateCalibrator.reserved_output_tokens — эта константа
    # используется только если Settings почему-то не передал своё значение.
    RESERVED_OUTPUT_TOKENS = 1500

    def __init__(
        self,
        settings: Settings,
        budget: LLMBudget,
        *,
        shared_limiter: "TokenRateLimiter | None" = None,
        shared_calibrator: "TokenEstimateCalibrator | None" = None,
        shared_cache_observability: "CacheObservability | None" = None,
    ):
        settings.validate_free_only()
        if not settings.groq_api_key:
            raise RuntimeError(
                "GROQ_API_KEY не задан. Получите бесплатный ключ на "
                "https://console.groq.com/keys и укажите в .env"
            )
        self.settings = settings
        self.budget = budget

        # v4-B4: коэффициенты оценки токенов теперь настраиваемы из
        # Settings (значения по умолчанию идентичны прежнему хардкоду в
        # llm/common.py — см. обоснование там).
        self._chars_per_token_kwargs = dict(
            cyrillic_ratio_threshold=getattr(settings, "groq_cyrillic_ratio_threshold", 0.3),
            cyrillic_chars_per_token=getattr(settings, "groq_chars_per_token_cyrillic", 2.3),
            latin_chars_per_token=getattr(settings, "groq_chars_per_token_latin", 4.0),
        )
        # Новый ключ Settings (в репозитории его пока нет): groq_use_tiktoken: bool = True
        self._encoding = None
        if (
                getattr(settings, "groq_use_tiktoken", True)
                and settings.groq_model in _TIKTOKEN_SUPPORTED_MODELS
        ):
            self._encoding = _try_load_tiktoken_encoding()
        # v4-A1: если передан shared_limiter/shared_calibrator (см.
        # llm/factory.py::create_extraction_llm_client) — переиспользуем
        # ИХ вместо создания новых. Это единственный способ, которым два
        # разных GroqClient (основной и extraction), физически бьющих в
        # один и тот же TPM Groq при совпадающей модели, могут видеть
        # расход друг друга. tpm_limit/safety_margin и т.п. в этом случае
        # ИГНОРИРУЮТСЯ (лимитер уже сконструирован с параметрами клиента,
        # который создал его первым) — это ожидаемо: конфликт параметров
        # двух клиентов с ОДНОЙ моделью не должен возникать, если
        # groq_share_limiter_when_same_model включён именно потому, что
        # модель (а значит и её реальный лимит) — одна.
        if shared_limiter is not None:
            self._limiter = shared_limiter
        else:
            tpm_limit = getattr(settings, "groq_tpm_limit", None) or self.DEFAULT_TPM_LIMIT
            self._limiter = TokenRateLimiter(
                tpm_limit=tpm_limit,
                safety_margin=getattr(settings, "groq_limiter_safety_margin", 0.85),
                margin_penalty_factor=getattr(settings, "groq_margin_penalty_factor", 0.8),
                margin_min_penalty=getattr(settings, "groq_margin_min_penalty", 0.5),
                margin_recovery_seconds=getattr(settings, "groq_margin_recovery_seconds", 300.0),
                recovery_mode=getattr(settings, "groq_margin_recovery_mode", "linear"),
            )

        if shared_calibrator is not None:
            self._calibrator = shared_calibrator
        else:
            self._calibrator = TokenEstimateCalibrator(
                ema_alpha=getattr(settings, "groq_calibration_ema_alpha", 0.3),
                min_ratio=getattr(settings, "groq_calibration_min_ratio", 0.05),
                max_ratio=getattr(settings, "groq_calibration_max_ratio", 1.5),
                output_ema_alpha=getattr(settings, "groq_output_calibration_ema_alpha", 0.3),
                output_min_floor=getattr(settings, "groq_reserved_output_min_tokens", 300),
            )

        # v4-B1: дефолтный (холодный старт роли) резерв под output —
        # читается из Settings, но численно равен прежнему хардкоду 1500,
        # если Settings не переопределяет (см. обоснование в
        # config/settings.py::groq_reserved_output_tokens_default).
        self._reserved_output_default = getattr(
            settings, "groq_reserved_output_tokens_default", self.RESERVED_OUTPUT_TOKENS
        )
        self._account_for_prompt_cache = getattr(settings, "groq_account_for_prompt_cache", False)
        self._cache_observability = shared_cache_observability or CacheObservability()
        self._strict_schema_supported = settings.groq_model in _STRICT_SCHEMA_SUPPORTED_MODELS
        self._reasoning_supported = settings.groq_model in _REASONING_EFFORT_SUPPORTED_MODELS

        from openai import OpenAI

        _http_client = httpx.Client(
            limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=20.0),
            http2=False,
        )

        self._client = OpenAI(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
            http_client=_http_client,
        )

    def _estimate_tokens(self, text: str) -> int:
        """Точный подсчёт через tiktoken для gpt-oss; иначе — эвристика из
        llm/common.py с коэффициентами из Settings."""
        if not text:
            return 0
        if self._encoding is not None:
            # encode_ordinary: спецтокены в тексте заметок не должны ронять подсчёт
            return len(self._encoding.encode_ordinary(text))
        return estimate_tokens(text, **self._chars_per_token_kwargs)

    def available_prompt_budget_tokens(
            self,
            system_instruction: str,
            response_model: type[BaseModel],
    ) -> int:
        """Сколько токенов остаётся под сам prompt (без system/schema/output),
        чтобы вызывающий код (llm/chunking.py) мог заранее разбить список
        элементов на батчи.

        Бюджет считается от ПОЛНОЙ ёмкости минутного окна (capacity_tokens),
        а не от остатка текущей минуты: фиксированное окно сбрасывается на
        границе минуты, и wait_and_reserve() при необходимости дождётся её.
        Иначе в конце минуты остаток близок к нулю, батчинг раздробил бы
        список по одному элементу и лишний раз потратил вызовы
        (MAX_LLM_CALLS_PER_TASK).
        """

        #schema_hint = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        schema_hint = json.dumps(
            _to_strict_json_schema(response_model.model_json_schema()),
            ensure_ascii=False, separators=(",", ":"),
        )
        overhead = self._estimate_tokens(system_instruction) + self._estimate_tokens(schema_hint)
        overhead += _REQUEST_OVERHEAD_TOKENS
        total_capacity = self._limiter.capacity_tokens()
        # Консервативный дефолтный резерв под output, без role-калибровки:
        # метод вызывается до того, как известна роль (см. прежний комментарий).
        budget = total_capacity - overhead - self._reserved_output_default
        return max(budget, 0)

    def generate_structured(
        self,
        *,
        role: str,
        prompt: str,
        response_model: type[T],
        status: TaskStatus,
        system_instruction: str | None = None,
    ) -> T:
        self.budget.check_and_register_task_call(status)
        self.budget.check_rpd_soft_limit()
        self.budget.wait_if_needed_for_rpm()

        response_format, full_system = self._build_response_format_and_system(
            response_model, system_instruction
        )
        fallback_format: dict | None = None
        fallback_system: str | None = None
        if self._strict_schema_supported:
            fallback_format, fallback_system = self._build_response_format_and_system(
                response_model, system_instruction, force_json_object=True
            )
        # в generate_structured, вместо system_tokens = self._estimate_tokens(full_system):
        system_tokens = self._estimate_tokens(full_system) + _REQUEST_OVERHEAD_TOKENS
        if self._strict_schema_supported:
            # В strict-режиме схема не входит в full_system, но входит в промпт Groq
            schema_json = json.dumps(
                _to_strict_json_schema(response_model.model_json_schema()),
                ensure_ascii=False, separators=(",", ":"),
            )
            system_tokens += self._estimate_tokens(schema_json)
        system_tokens = self._estimate_tokens(full_system)

        # v4-B1: РАНЬШЕ здесь стоял self.RESERVED_OUTPUT_TOKENS (статичная
        # константа на все роли). Теперь — калиброванный по роли резерв:
        # для роли без наблюдений это по-прежнему self._reserved_output_default
        # (тот же уровень безопасности, что и раньше), а после нескольких
        # вызовов роли — реальная EMA её completion_tokens (не ниже пола
        # groq_reserved_output_min_tokens). Это освобождает бюджет под
        # prompt/батчи для коротких по ответу ролей (critic, vault_dedup,
        # folder_assignment) без риска для длинных (synthesizer_write).
        by_role = getattr(self.settings, "groq_reserved_output_by_role", {})
        reserved_output = self._calibrator.reserved_output_tokens(
            role, default=by_role.get(role, self._reserved_output_default)
        )

        # reserved_output = self._calibrator.reserved_output_tokens(
        #     role, default=self._reserved_output_default
        # )
        max_prompt_tokens = self._limiter._limit - reserved_output - system_tokens

        if max_prompt_tokens <= 200:
            raise GroqPromptTooLargeError(
                f"Системный промпт+схема (~{system_tokens} токенов) сами по "
                f"себе не влезают в TPM-бюджет (~{self._limiter._limit}, "
                f"резерв под output={reserved_output}). Сократите "
                "system_instruction/response_model или увеличьте "
                "groq_tpm_limit в настройках, если это не соответствует "
                "реальному тарифу Groq."
            )

        prompt_tokens = self._estimate_tokens(prompt)
        if prompt_tokens > max_prompt_tokens:
            prompt = self._auto_truncate_prompt(prompt, max_prompt_tokens, role=role)
            prompt_tokens = self._estimate_tokens(prompt)

        # Калибруем только ВХОД (system+prompt); резерв под output — отдельное
        # слагаемое со своей EMA (иначе коэффициент, выученный при одном
        # резерве, применяется к оценке с другим).
        naive_input = system_tokens + prompt_tokens
        calibrated_estimate = self._calibrator.correct(role, naive_input) + reserved_output

        #naive_estimate = system_tokens + prompt_tokens + reserved_output
        #calibrated_estimate = self._calibrator.correct(role, naive_estimate)

        extra_body = self._build_reasoning_extra_body(role)
        try:
            raw_json, completion_tokens = self._call_with_retry(
                prompt=prompt,
                system_instruction=full_system,
                estimated_tokens=calibrated_estimate,
                response_format=response_format,
                fallback_format=fallback_format,
                fallback_system=fallback_system,
                role=role,
                naive_estimate=naive_input,
                extra_body=extra_body,
            )

            parsed = self._parse_with_repair(raw_json, response_model)
            self.budget.register_call(status, role=role, ok=True)
            # v4-B1: обучаем калибратор реальной длиной ОТВЕТА (отдельно
            # от prompt-ratio, который обновляется внутри _call_with_retry).
            if completion_tokens:
                self._calibrator.observe_output(role, completion_tokens)
            return parsed
        except GroqRateLimitError as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise LLMFreeLimitReached(
                "Свободный лимит Groq API исчерпан (устойчивая 429 после retry). "
                "Задача остановлена. Прогресс сохранён — можно продолжить позже."
            ) from exc
        except GroqSchemaError as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise
        except Exception as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise

    def _build_response_format_and_system(
        self,
        response_model: type[BaseModel],
        system_instruction: str | None,
        *,
        force_json_object: bool = False,
    ) -> tuple[dict, str]:
        """Строгий json_schema для gpt-oss-20b/120b (constrained decoding,
        схема НЕ дублируется текстом в промпте — Groq применяет её сам).
        Для остальных моделей, а также при force_json_object=True (fallback
        после отказа API от строгой схемы) — json_object + текстовая схема
        как подсказка (best-effort). Служебные суффиксы — на английском;
        язык ТЕКСТОВЫХ значений задаётся LANGUAGE_RULE в инструкции роли."""
        base_instruction = (system_instruction or "").strip()

        if self._strict_schema_supported and not force_json_object:
            schema = _to_strict_json_schema(response_model.model_json_schema())
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": schema,
                },
            }
            full_system = (
                base_instruction
                + "\n\nRespond with a valid JSON object. The response format "
                  "is enforced by the API according to the given schema — do "
                  "not add markdown fences (```), text before/after the JSON, "
                  "or fields that are not in the schema. Field names and "
                  "enum values must stay exactly as in the schema (never "
                  "translate them). ALWAYS include ALL schema fields, even "
                  "if they do not apply — use an empty string \"\" or an "
                  "empty list [] instead of omitting the field (omitting a "
                  "required field is a format error, even if the role does "
                  "not need it right now)."
            )
            return response_format, full_system

        response_format = {"type": "json_object"}
        schema_hint = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        full_system = (
            base_instruction
            + "\n\nRespond STRICTLY with a valid JSON object matching the "
              "following JSON Schema. Include ALL schema fields, even if "
              "there is no meaningful value (use \"\" or []), do not omit "
              "fields. Field names and enum values must stay exactly as in "
              "the schema (never translate them). No text before/after the "
              "JSON, no markdown fences (```), only raw JSON. Write all "
              "numeric fields (e.g. confidence) ONLY as digits like 0.9, "
              "NEVER as words (not 'Nine', not 'девять') and do not put a "
              "space between the integer and fractional parts.\n\n"
              "JSON Schema:\n"
            + schema_hint
        )
        return response_format, full_system

    def _build_reasoning_extra_body(self, role: str) -> dict | None:
        """Параметры рассуждений для конкретной роли. None — модель их не
        поддерживает, ничего не передаём (совместимость с другими моделями).
        Через extra_body, а не именованными аргументами: include_reasoning нет
        в типизации openai SDK, а reasoning_effort появился только в
        поздних версиях (requirements.txt допускает openai>=1.30)."""
        if not self._reasoning_supported:
            return None
        body: dict = {"include_reasoning": False}  # текст рассуждений нам не нужен
        effort = _REASONING_EFFORT_BY_ROLE.get(role)
        if effort:
            body["reasoning_effort"] = effort
        return body

    def _auto_truncate_prompt(
        self, prompt: str, max_prompt_tokens: int, *, role: str
    ) -> str:
        """Автоматически укорачивает prompt под доступный TPM-бюджет —
        без участия вызывающего кода. Режет с конца (предполагая, что
        инструкции/контекст важнее хвоста текста, как это обычно бывает
        в наших промптах: "тема + концепции + текст источника"), оставляя
        небольшой запас на предупреждающую пометку.

        Это safety-net на уровне клиента: даже если вызывающая роль сама
        не умеет резать длинные источники (в отличие от extractor_critic,
        где чанкинг уже есть), запрос всё равно уйдёт и не уронит задачу
        ошибкой GroqPromptTooLargeError."""
        text_chars_per_token = len(prompt) / max(self._estimate_tokens(prompt), 1)
        #tiktokentext_chars_per_token = chars_per_token(prompt, **self._chars_per_token_kwargs)

        marker = "\n\n[…текст автоматически обрезан из-за лимита токенов Groq API…]"
        marker_tokens = self._estimate_tokens(marker)
        allowed_tokens = max(max_prompt_tokens - marker_tokens, 50)
        allowed_chars = int(allowed_tokens * text_chars_per_token * 0.92)

        if len(prompt) <= allowed_chars:
            return prompt

        truncated = prompt[:allowed_chars].rstrip()
        boundary = max(truncated.rfind("\n"), truncated.rfind(". "))
        if boundary > allowed_chars * 0.7:
            truncated = truncated[: boundary + 1]

        logger.warning(
            "Автообрезка промпта для роли '%s': %d символов -> %d символов "
            "(было ~%d токенов, доступно ~%d). Если это происходит часто — "
            "стоит добавить чанкинг на уровне вызывающей роли, как в "
            "extractor_critic.py, чтобы не терять хвост текста.",
            role, len(prompt), len(truncated),
            self._estimate_tokens(prompt), max_prompt_tokens,
        )
        return truncated + marker

    def _parse_with_repair(self, raw_json: str, response_model: type[T]) -> T:
        """Пытается распарсить как есть; при неудаче применяет repair-эвристики
        (снятие markdown-ограждения, числа словами -> цифры) и пробует снова.
        Если и это не помогло — поднимает GroqSchemaError с обоими вариантами
        текста в сообщении для диагностики."""
        try:
            return response_model.model_validate_json(raw_json)
        except (ValidationError, ValueError) as first_exc:
            repaired = repair_json(raw_json)
            if repaired == raw_json:
                raise GroqSchemaError(
                    f"Groq вернул невалидный JSON, repair не применим: {first_exc}"
                ) from first_exc
            try:
                parsed = response_model.model_validate_json(repaired)
                logger.warning(
                    "Groq вернул JSON с ошибками, но repair-слой исправил его "
                    "(например, число словами -> цифры)."
                )
                return parsed
            except (ValidationError, ValueError) as second_exc:
                raise GroqSchemaError(
                    "Groq вернул невалидный JSON даже после repair. "
                    f"Исходная ошибка: {first_exc}; после repair: {second_exc}"
                ) from second_exc

    _RETRYABLE_EXCEPTIONS = (
        GroqRateLimitError,
        APIConnectionError,
        APITimeoutError,
    )

    @retry(
        retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
        wait=wait_random_exponential(multiplier=1, max=15),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _call_with_retry(
        self,
        *,
        prompt: str,
        system_instruction: str,
        estimated_tokens: int,
        response_format: dict,
        fallback_format: dict | None = None,
        fallback_system: str | None = None,
        role: str = "",
        naive_estimate: int = 0,
        extra_body: dict | None = None
    ) -> tuple[str, int]:
        """Возвращает (raw_json_content, completion_tokens). completion_tokens
        добавлен к прежней сигнатуре (v4-B1) — вызывающий код
        (generate_structured) использует его для observe_output(); 0, если
        Groq не вернул usage (не должно происходить в норме, но не
        считаем это ошибкой)."""
        self._limiter.wait_and_reserve(estimated_tokens)

        try:
            create_kwargs = dict(
                model=self.settings.groq_model,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                response_format=response_format,
                timeout=self.settings.groq_timeout_seconds,
            )
            if extra_body:
                create_kwargs["extra_body"] = extra_body
            response = self._client.chat.completions.create(**create_kwargs)
            content = response.choices[0].message.content
            if content is None:
                raise RuntimeError("Groq вернул пустой ответ (content=None)")

            usage = getattr(response, "usage", None)
            actual_total = getattr(usage, "total_tokens", None) if usage else None
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0

            if actual_total is not None:
                details = getattr(usage, "prompt_tokens_details", None)
                cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
                cache_hit = cached > 0

                # Отдельно логируем usage по роли: input/output/reasoning/кэш,
                # оценку резерва против факта и тайминги Groq (extra-поля usage).
                prompt_tokens_real = int(getattr(usage, "prompt_tokens", 0) or 0)
                out_details = getattr(usage, "completion_tokens_details", None)
                reasoning = getattr(out_details, "reasoning_tokens", None) if out_details else None
                finish_reason = getattr(response.choices[0], "finish_reason", None)

                def _t(name: str) -> str:
                    v = getattr(usage, name, None)
                    return f"{v:.2f}" if isinstance(v, (int, float)) else "-"

                logger.info(
                    "\n Groq usage [%s]: input=%d (кэш=%d) output=%d (reasoning=%s) total=%d | "
                    "резерв: naive=%d, откалиброван=%d, факт/резерв=%.2f | finish=%s | "
                    "время, с: queue=%s prompt=%s completion=%s total=%s",
                    role, prompt_tokens_real, cached, completion_tokens,
                    reasoning if reasoning is not None else "-", int(actual_total),
                    naive_estimate, estimated_tokens,
                    (int(actual_total) / estimated_tokens) if estimated_tokens else 0.0,
                    finish_reason, _t("queue_time"), _t("prompt_time"),
                    _t("completion_time"), _t("total_time"),
                )
                if finish_reason == "length":
                    logger.warning(
                        "Groq [%s]: ответ обрезан по длине (finish_reason=length) — "
                        "ожидайте GroqSchemaError.", role,
                    )

                if self._account_for_prompt_cache:
                    effective_tokens = max(int(actual_total) - cached, 0)
                else:
                    effective_tokens = int(actual_total)   # кэш игнорируется в бюджете

                self._limiter.adjust_last_reservation(effective_tokens)

                if role and naive_estimate and prompt_tokens_real > 0:
                    # naive_estimate теперь — оценка только входа; сверяем с
                    # реальным prompt_tokens (без кэша, если он учитывается в бюджете).
                    actual_input = prompt_tokens_real - (cached if self._account_for_prompt_cache else 0)
                    self._calibrator.observe(role, naive_estimate, max(actual_input, 1))

                if role:
                    self._cache_observability.observe(role, cache_hit)  # только факт хита

                if cache_hit:
                    logger.info(
                        "Groq prompt cache hit для роли '%s': %d/%d токенов "
                        "промпта из кэша (%.0f%%) — НЕ учтено в бюджете "
                        "(groq_account_for_prompt_cache=%s), только "
                        "зафиксировано для диагностики.",
                        role, cached, actual_total,
                        100 * cached / actual_total if actual_total else 0,
                        self._account_for_prompt_cache,
                    )

            return content, completion_tokens
        except Exception as exc:
            if extra_body and _is_reasoning_param_error(exc):
                logger.warning(
                    "Groq отклонил параметры рассуждений (%s) — разовый повтор без них.", exc
                )
                return self._call_with_retry(
                    prompt=prompt, system_instruction=system_instruction,
                    estimated_tokens=estimated_tokens, response_format=response_format,
                    fallback_format=fallback_format, fallback_system=fallback_system,
                    role=role, naive_estimate=naive_estimate, extra_body=None,
                )

            if (
                response_format.get("type") == "json_schema"
                and _is_schema_unsupported_error(exc)
                and fallback_format is not None
            ):
                logger.warning(
                    "Groq отклонил strict json_schema (%s) — разовый откат "
                    "на json_object С ТЕКСТОВОЙ JSON Schema в system-промпте "
                    "(без constrained decoding) для этого вызова.",
                    exc,
                )
                return self._call_with_retry(
                    prompt=prompt,
                    system_instruction=fallback_system,
                    estimated_tokens=estimated_tokens,
                    response_format=fallback_format,
                    role=role,
                    naive_estimate=naive_estimate,
                )

            logger.exception(
                "Groq request failed: type=%s, message=%s", type(exc).__name__, str(exc)
            )

            if is_rate_limit_error(exc):
                retry_after = parse_retry_after(str(exc))
                if retry_after is not None:
                    self._limiter.force_wait(retry_after)
                self._limiter.register_rate_limit_hit()
                raise GroqRateLimitError(str(exc), retry_after=retry_after) from exc
            if is_request_too_large_error(exc):
                raise GroqPromptTooLargeError(
                    f"Groq вернул 413 Request Entity Too Large: {exc}"
                ) from exc
            raise