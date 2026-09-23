"""
Общие для ВСЕХ провайдеров LLM (Groq, OpenRouter, ...) утилиты и
провайдер-нейтральная иерархия исключений.

Почему это выделено отдельно, а не оставлено дублированным по клиентам:
repair_json/_WORD_DIGITS были продублированы между groq_client.py и
openrouter_client.py осознанно (см. докстринг openrouter_client.py —
"чтобы не тянуть зависимость друг на друга") и это было безопасно, т.к.
это чистые функции без побочных эффектов.

Дублирование КЛАССОВ ИСКЛЮЧЕНИЙ безопасным не было: roles/extractor_critic.py
ловит конкретные GroqPromptTooLargeError/GroqSchemaError (импортируя их
напрямую из llm.groq_client). Если активный провайдер — OpenRouter (см.
llm/router.py::RoleRoutingLLMClient), тот поднимает СВОИ собственные
OpenRouterSchemaError и т.п. — except в extractor_critic.py на них не
срабатывает, ошибка улетает наружу необработанной и роняет задачу целиком.

Итог: роли (roles/*.py) и общий код (llm/chunking.py и т.п.) должны
ловить ТОЛЬКО типы из этого модуля (LLMRateLimitError/LLMSchemaError/
LLMPromptTooLargeError), никогда Groq*/OpenRouter*-специфичные классы
напрямую — это и есть содержание Protocol'а llm/base.py::LLMClient,
только для ошибок, а не для методов.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Провайдер-нейтральная иерархия исключений
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Общий базовый класс для всех ошибок, поднимаемых любым клиентом,
    реализующим llm/base.py::LLMClient.generate_structured()."""


class LLMRateLimitError(LLMError):
    """429 / rate limit от API провайдера, устойчивый после retry на
    уровне клиента. На верхнем уровне (в generate_structured каждого
    клиента) оборачивается в orchestrator.budget.LLMFreeLimitReached —
    это единственное место, которое остаётся провайдер-специфичным."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMSchemaError(LLMError):
    """Модель вернула JSON, невалидный по response_model даже после
    repair-эвристик (см. repair_json ниже). Вызывающий код (например,
    roles/extractor_critic.py) может отреагировать уменьшением батча,
    а не просто повторным тем же запросом."""


class LLMPromptTooLargeError(LLMError):
    """Промпт+система+ожидаемый output превышают доступный бюджет
    контекста. Поднимается ДО (или вместо) сетевого вызова — вызывающий
    код должен порезать текст (см. llm/chunking.py)."""

class LLMProviderOverloadedError(LLMError):
    """Апстрим-провайдер модели временно перегружен (напр. OpenRouter
    503/provider_overloaded). Retryable и failover-triggering — не
    проблема качества ответа модели, а временная недоступность
    инфраструктуры за конкретной моделью-кандидатом."""

# ---------------------------------------------------------------------------
# JSON repair (раньше было продублировано в groq_client.py/openrouter_client.py)
# ---------------------------------------------------------------------------

# Числа словами, которые слабые модели иногда подставляют вместо цифр в
# JSON (наблюдалось в реальных логах: "0. Nine" вместо "0.9").
WORD_DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


def repair_json(raw: str) -> str:
    """Чинит наиболее частые способы, которыми слабые модели ломают JSON:
    - markdown-обёртка ```json ... ```
    - число словами после точки: "0. Nine" / "0.Nine" -> "0.9"
    Возвращает исправленную строку без гарантии валидности — вызывающий
    код обязан обернуть повторный model_validate_json в try/except."""
    text = raw.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    def _replace_word_decimal(match: re.Match) -> str:
        whole = match.group(1)
        word = match.group(2).lower()
        digit = WORD_DIGITS.get(word)
        if digit is None:
            return match.group(0)
        return f"{whole}.{digit}"

    text = re.sub(r"(\d)\.\s*([A-Za-z]+)\b", _replace_word_decimal, text)
    return text


# ---------------------------------------------------------------------------
# Распознавание типа ошибки по тексту — общее для OpenAI-совместимых
# HTTP-клиентов (и Groq, и OpenRouter в проекте построены на пакете `openai`)
# ---------------------------------------------------------------------------

_RETRY_AFTER_RE = re.compile(r"try again in\s+([\d.]+)\s*s", re.IGNORECASE)


def is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


def is_request_too_large_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "413" in text or "request_too_large" in text or "request entity too large" in text


def parse_retry_after(message: str) -> float | None:
    """"Please try again in 10.02s" -> 10.02"""
    m = _RETRY_AFTER_RE.search(message)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Грубая оценка числа токенов без внешних зависимостей. У Groq поверх этого
# есть калибровка по роли (TokenEstimateCalibrator в groq_client.py) — она
# остаётся там, т.к. специфична для TPM-лимитера Groq. Здесь — только
# базовая "наивная" оценка, достаточная и для OpenRouter (где нет TPM
# leaky-bucket, но нужна хотя бы грубая прикидка бюджета промпта).
# ---------------------------------------------------------------------------


def chars_per_token(text: str) -> float:
    if not text:
        return 4.0
    cyrillic = sum(1 for ch in text if "а" <= ch.lower() <= "я" or ch.lower() == "ё")
    ratio = cyrillic / max(len(text), 1)
    return 2.3 if ratio > 0.3 else 4.0


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return int(len(text) / chars_per_token(text)) + 1