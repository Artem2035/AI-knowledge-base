"""
GroqClient — реализует ТОТ ЖЕ публичный контракт, что gemini/client.py::GeminiClient
(метод generate_structured с идентичной сигнатурой). Благодаря этому
roles/*.py не знают и не должны знать, какой провайдер активен —
переключение происходит только в llm/factory.py на основании
settings.llm_provider.

Отличие от Gemini: Groq API (OpenAI-совместимый) не поддерживает
Gemini-стиль response_schema (строгую JSON Schema на стороне сервера).
Вместо этого используется JSON mode (response_format={"type":
"json_object"}) + сама схема передаётся текстом в system-промпте как
подсказка модели. Финальная гарантия корректности — как и для Gemini —
через Pydantic-валидацию на стороне кода (response_model.model_validate_json).

ИЗМЕНЕНИЯ (v2):
1. TokenRateLimiter — клиентский sliding-window limiter по TPM.
   Тормозит вызовы ДО отправки запроса, а не после получения 429/413.
   Все вызовы через один инстанс GroqClient сериализуются (один лок),
   что решает проблему параллельных запросов, вместе выжирающих TPM-бюджет.
2. При 429 парсим "Please try again in N.NNs" из текста ошибки Groq
   и ждём именно это время, а не слепой exponential backoff.
3. Перед отправкой можно спросить available_prompt_budget_tokens(), чтобы
   вызывающий код (extractor_critic.py) заранее порезал длинный текст
   на чанки и не словил 413 "Request too large".
4. JSON-repair: модель (gpt-oss-120b) иногда пишет числовые поля словами
   ("confidence":0. Nine вместо 0.9). Добавлен regex-репейр наиболее частых
   паттернов перед Pydantic-валидацией + один retry с "усиленным" промптом
   при провале.
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
from orchestrator.budget import GeminiBudget, GeminiFreeLimitReached
from storage.models import TaskStatus

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Числа словами, которые модель иногда подставляет вместо цифр в JSON
# (наблюдалось в реальных логах: "0. Nine" вместо "0.9").
_WORD_DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}

# "Please try again in 10.02s" -> 10.02
_RETRY_AFTER_RE = re.compile(r"try again in\s+([\d.]+)\s*s", re.IGNORECASE)


class GroqRateLimitError(Exception):
    """Оборачивает 429 от Groq API для retry-логики tenacity."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class GroqSchemaError(Exception):
    """JSON от модели невалиден даже после repair-попыток.
    Отличается от обычных ошибок тем, что вызывающий код (например,
    extractor_critic) может отреагировать уменьшением батча/чанка,
    а не просто ретраем того же запроса."""


class GroqPromptTooLargeError(Exception):
    """Промпт+система+ожидаемый output превышают доступный TPM-бюджет.
    Поднимается ДО сетевого вызова — вызывающий код должен порезать текст."""


def _chars_per_token(text: str) -> float:
    """Единая эвристика chars/token, переиспользуемая и оценкой, и
    авто-обрезкой — чтобы эти две операции не расходились в оценках
    (именно расхождение в 2.0 vs 2.3 приводило к тому, что чанк,
    посчитанный "подходящим", на самом деле не помещался в бюджет)."""
    if not text:
        return 4.0
    cyrillic = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    ratio = cyrillic / max(len(text), 1)
    return 2.3 if ratio > 0.3 else 4.0


def _estimate_tokens(text: str) -> int:
    """Грубая оценка количества токенов без внешних зависимостей.
    Для кириллицы токенизаторы в среднем дают ~2.2-2.5 символа/токен
    (хуже, чем для английского ~4 символа/токен), поэтому оцениваем
    консервативно по доле кириллицы в тексте, чтобы не занижать расход."""
    if not text:
        return 0
    return int(len(text) / _chars_per_token(text)) + 1


def _parse_retry_after(message: str) -> float | None:
    m = _RETRY_AFTER_RE.search(message)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _repair_json(raw: str) -> str:
    """Чинит наиболее частые способы, которыми gpt-oss-120b ломает JSON:
    - markdown-обёртка ```json ... ```
    - число словами после точки: "0. Nine" / "0.Nine" -> "0.9"
    Возвращает исправленную строку (без гарантии, что она валидна —
    вызывающий код обязан обернуть повторный json.loads/model_validate_json
    в try/except)."""
    text = raw.strip()

    # Снять markdown-ограждение, если модель всё же его добавила
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # "0. Nine" / "0 . nine" / "0.Nine" -> "0.9"
    def _replace_word_decimal(match: re.Match) -> str:
        whole = match.group(1)
        word = match.group(2).lower()
        digit = _WORD_DIGITS.get(word)
        if digit is None:
            return match.group(0)
        return f"{whole}.{digit}"

    text = re.sub(
        r"(\d)\.\s*([A-Za-z]+)\b",
        _replace_word_decimal,
        text,
    )

    return text


class TokenRateLimiter:
    """Клиентский sliding-window limiter по токенам в минуту (TPM).

    Цель: предотвратить 429/413 ДО отправки запроса, а не реагировать
    постфактум. Один инстанс на процесс/GroqClient — все вызовы идут через
    один лок, поэтому конкурентные запросы из разных потоков сериализуются
    и не съедают TPM-бюджет одновременно.
    """

    def __init__(self, tpm_limit: int, safety_margin: float = 0.85):
        self._limit = max(int(tpm_limit * safety_margin), 1)
        self._window: deque[tuple[float, int]] = deque()
        self._lock = threading.RLock()

    def _prune(self, now: float) -> int:
        while self._window and now - self._window[0][0] > 60:
            self._window.popleft()
        return sum(tokens for _, tokens in self._window)

    def available_tokens(self) -> int:
        with self._lock:
            used = self._prune(time.monotonic())
            return max(self._limit - used, 0)

    def wait_and_reserve(self, estimated_tokens: int) -> None:
        """Блокирует поток, пока в скользящем окне не появится место
        под estimated_tokens. Резервирует место сразу (оптимистично);
        реальный расход корректируется через adjust_last_reservation()."""
        with self._lock:
            while True:
                now = time.monotonic()
                used = self._prune(now)
                if used + estimated_tokens <= self._limit:
                    self._window.append((now, estimated_tokens))
                    return
                oldest_ts, _ = self._window[0]
                sleep_for = max(60 - (now - oldest_ts) + 0.1, 0.2)
                logger.info(
                    "TokenRateLimiter: ждём %.1fs (used=%d, limit=%d, need=%d)",
                    sleep_for, used, self._limit, estimated_tokens,
                )
                time.sleep(min(sleep_for, 5.0))  # спим короткими интервалами,
                # чтобы не залипать надолго при неточной оценке

    def adjust_last_reservation(self, actual_tokens: int) -> None:
        """Подменяет оценочный резерв фактическим расходом из response.usage,
        если он доступен — повышает точность лимитера со временем."""
        with self._lock:
            if self._window:
                ts, _ = self._window[-1]
                self._window[-1] = (ts, actual_tokens)

    def force_wait(self, seconds: float) -> None:
        """Используется, когда Groq всё же вернул 429 с явным retry-after —
        держим лок, чтобы никто другой не полез параллельно в это окно."""
        with self._lock:
            time.sleep(max(seconds, 0.1))


class GroqClient:
    # Групповой лимит по TPM для gpt-oss-120b на free/on-demand tier
    # (см. лог: Limit 8000). Вынесено в константу, т.к. могло бы отличаться
    # для другой модели — в этом случае стоит прокинуть через Settings.
    DEFAULT_TPM_LIMIT = 8000
    # Сколько токенов резервируем под сам ответ модели (output),
    # чтобы не упереться в TPM уже во время генерации.
    RESERVED_OUTPUT_TOKENS = 1500

    def __init__(self, settings: Settings, budget: GeminiBudget):
        settings.validate_free_only()
        if not settings.groq_api_key:
            raise RuntimeError(
                "GROQ_API_KEY не задан. Получите бесплатный ключ на "
                "https://console.groq.com/keys и укажите в .env"
            )
        self.settings = settings
        self.budget = budget

        tpm_limit = getattr(settings, "groq_tpm_limit", None) or self.DEFAULT_TPM_LIMIT
        self._limiter = TokenRateLimiter(tpm_limit=tpm_limit)

        from openai import OpenAI  # локальный импорт — модуль не требует пакет,
        # если Groq вообще не используется (LLM_PROVIDER=gemini)

        self._client = OpenAI(
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
        )

    def available_prompt_budget_tokens(
        self,
        system_instruction: str,
        response_model: type[BaseModel],
    ) -> int:
        """Сколько токенов остаётся под сам prompt (без system/schema/output),
        чтобы вызывающий код (extractor_critic) мог заранее решить, резать
        ли текст источника на чанки, вместо того чтобы ловить 413."""
        schema_hint = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        overhead = _estimate_tokens(system_instruction) + _estimate_tokens(schema_hint)
        total_available = self._limiter.available_tokens()
        # берём min с полным лимитом окна (available_tokens уже учитывает
        # то, что "занято" другими вызовами в последнюю минуту)
        budget = total_available - overhead - self.RESERVED_OUTPUT_TOKENS
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

        schema_hint = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        full_system = (
            (system_instruction or "").strip()
            + "\n\nОтвечай СТРОГО валидным JSON-объектом, соответствующим "
              "следующей JSON Schema. Никакого текста до/после JSON, никакой "
              "markdown-разметки (```), только сырой JSON. Все числовые поля "
              "(например confidence) пиши ТОЛЬКО цифрами в формате 0.9, "
              "НИКОГДА не пиши число словами (не пиши 'Nine', не пиши "
              "'девять') и не ставь пробел между целой и дробной частью.\n\n"
              "JSON Schema:\n"
            + schema_hint
        )

        system_tokens = _estimate_tokens(full_system)
        max_prompt_tokens = self._limiter._limit - self.RESERVED_OUTPUT_TOKENS - system_tokens

        if max_prompt_tokens <= 200:
            # Даже без пользовательского prompt (голая система+схема) уже
            # не влезаем ни при каком раскладе — это не решается обрезкой
            # текста, это ошибка конфигурации (модель/лимит/схема).
            raise GroqPromptTooLargeError(
                f"Системный промпт+схема (~{system_tokens} токенов) сами по "
                f"себе не влезают в TPM-бюджет (~{self._limiter._limit}). "
                "Сократите system_instruction/response_model или увеличьте "
                "groq_tpm_limit в настройках, если это не соответствует "
                "реальному тарифу Groq."
            )

        prompt_tokens = _estimate_tokens(prompt)
        if prompt_tokens > max_prompt_tokens:
            prompt = self._auto_truncate_prompt(prompt, max_prompt_tokens, role=role)
            prompt_tokens = _estimate_tokens(prompt)

        estimated = system_tokens + prompt_tokens

        try:
            raw_json = self._call_with_retry(
                prompt=prompt,
                system_instruction=full_system,
                estimated_tokens=estimated + self.RESERVED_OUTPUT_TOKENS,
            )
            parsed = self._parse_with_repair(raw_json, response_model)
            self.budget.register_call(status, role=role, ok=True)
            return parsed
        except GroqRateLimitError as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise GeminiFreeLimitReached(
                "Свободный лимит Groq API исчерпан (устойчивая 429 после retry). "
                "Задача остановлена. Прогресс сохранён — можно продолжить позже."
            ) from exc
        except GroqSchemaError as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise
        except Exception as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise

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
        chars_per_token = _chars_per_token(prompt)
        marker = "\n\n[…текст автоматически обрезан из-за лимита токенов Groq API…]"
        marker_tokens = _estimate_tokens(marker)
        allowed_tokens = max(max_prompt_tokens - marker_tokens, 50)
        allowed_chars = int(allowed_tokens * chars_per_token * 0.92)  # запас 8%

        if len(prompt) <= allowed_chars:
            return prompt

        truncated = prompt[:allowed_chars].rstrip()
        # стараемся не рвать посреди слова/предложения
        boundary = max(truncated.rfind("\n"), truncated.rfind(". "))
        if boundary > allowed_chars * 0.7:
            truncated = truncated[: boundary + 1]

        logger.warning(
            "Автообрезка промпта для роли '%s': %d символов -> %d символов "
            "(было ~%d токенов, доступно ~%d). Если это происходит часто — "
            "стоит добавить чанкинг на уровне вызывающей роли, как в "
            "extractor_critic.py, чтобы не терять хвост текста.",
            role, len(prompt), len(truncated),
            _estimate_tokens(prompt), max_prompt_tokens,
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
            repaired = _repair_json(raw_json)
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

    @retry(
        retry=retry_if_exception_type(GroqRateLimitError),
        wait=wait_random_exponential(multiplier=1, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _call_with_retry(
        self, *, prompt: str, system_instruction: str, estimated_tokens: int
    ) -> str:
        # Блокируемся, пока в TPM-окне не появится место. Это сериализует
        # ВСЕ вызовы через этот клиент (включая параллельные потоки),
        # что и решает проблему "запросы идут часто и превышают rpm/tpm".
        self._limiter.wait_and_reserve(estimated_tokens)

        try:
            response = self._client.chat.completions.create(
                model=self.settings.groq_model,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                timeout=self.settings.groq_timeout_seconds,
            )
            content = response.choices[0].message.content
            if content is None:
                raise RuntimeError("Groq вернул пустой ответ (content=None)")

            # Если Groq вернул реальный usage — используем его, чтобы лимитер
            # со временем становился точнее вместо грубой char-based оценки.
            usage = getattr(response, "usage", None)
            actual_total = getattr(usage, "total_tokens", None) if usage else None
            if actual_total:
                self._limiter.adjust_last_reservation(int(actual_total))

            return content
        except Exception as exc:
            logger.exception(
                "Groq request failed: type=%s, message=%s",
                type(exc).__name__,
                str(exc),
            )

            if _is_rate_limit_error(exc):
                retry_after = _parse_retry_after(str(exc))
                if retry_after is not None:
                    # Groq явно сказал, сколько ждать — уважаем это вместо
                    # случайного exponential backoff, и держим лимитер
                    # заблокированным на это время, чтобы никто не влез.
                    self._limiter.force_wait(retry_after)
                raise GroqRateLimitError(str(exc), retry_after=retry_after) from exc
            raise


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text