"""
OpenRouterClient — второй провайдер (llm/base.py::LLMClient Protocol),
маршрутизируемый по роли через llm/router.py::RoleRoutingLLMClient (см.
llm/factory.py::_create_openrouter_router). В отличие от GroqClient, здесь
НЕТ TPM leaky-bucket/калибровки по роли — свободные модели OpenRouter,
используемые в проекте (config/settings.py::openrouter_planning_model /
openrouter_writing_model), имеют кратно больший контекст (256K-1M токенов
против 8K TPM у Groq gpt-oss), и узкое место free tier у них — RPM/RPD
запроса, а не TPM одного вызова. Throttle поэтому ограничен тем же
LLMBudget (soft RPM/RPD), что уже используется во всей системе
(orchestrator/budget.py) — без отдельного токен-лимитера.

Один инстанс = одна конкретная модель (передаётся в конструктор). Под
разные роли создаются РАЗНЫЕ инстансы (см. llm/factory.py), а не один
клиент с переключением модели "на лету" — чтобы у каждой модели был свой
независимый LLMBudget (у разных моделей OpenRouter разные free-tier
лимиты, и мы не хотим, чтобы редкие вызовы Writer (Gemma) ждали из-за
частых вызовов Planner (Nemotron) в общем окне throttle).
"""
from __future__ import annotations

import json
import logging
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from config.settings import Settings
from orchestrator.budget import LLMBudget, LLMFreeLimitReached
from storage.models import TaskStatus

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# Те же repair-эвристики, что в llm/groq_client.py::_repair_json —
# продублированы намеренно (а не импортированы оттуда), чтобы
# openrouter_client.py не тянул зависимость на groq_client.py: это два
# независимых, взаимозаменяемых провайдера (см. llm/base.py::LLMClient),
# а не один зависит от другого.
_WORD_DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


class OpenRouterRateLimitError(Exception):
    """Оборачивает 429 от OpenRouter API для retry-логики tenacity."""


class OpenRouterSchemaError(Exception):
    """JSON от модели невалиден даже после repair-попыток. Тот же контракт,
    что GroqSchemaError — вызывающий код (extractor_critic и т.п.) может
    уменьшить батч, а не просто ретраить тот же запрос."""


def _repair_json(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    def _replace_word_decimal(match: re.Match) -> str:
        whole, word = match.group(1), match.group(2).lower()
        digit = _WORD_DIGITS.get(word)
        return f"{whole}.{digit}" if digit is not None else match.group(0)

    return re.sub(r"(\d)\.\s*([A-Za-z]+)\b", _replace_word_decimal, text)


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


class OpenRouterClient:
    def __init__(self, settings: Settings, budget: LLMBudget, model: str):
        settings.validate_free_only()
        if not settings.openrouter_api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY не задан. Получите бесплатный ключ на "
                "https://openrouter.ai/keys и укажите в .env"
            )
        self.settings = settings
        self.budget = budget
        self.model = model

        from openai import OpenAI  # локальный импорт, как в GroqClient —
        # модуль не должен требовать пакет, если провайдер не используется.

        self._client = OpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
        )

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
              "следующей JSON Schema. Включай ВСЕ поля из схемы (используй "
              "\"\" или [] для неприменимых), без markdown-разметки и текста "
              "до/после JSON. Все числа пиши только цифрами (0.9), никогда "
              "словами.\n\nJSON Schema:\n" + schema_hint
        )

        try:
            raw_json = self._call_with_retry(prompt=prompt, system_instruction=full_system)
            parsed = self._parse_with_repair(raw_json, response_model)
            self.budget.register_call(status, role=role, ok=True)
            return parsed
        except OpenRouterRateLimitError as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise LLMFreeLimitReached(
                f"Свободный лимит OpenRouter ({self.model}) исчерпан (устойчивая "
                "429 после retry). Задача остановлена. Прогресс сохранён — можно "
                "продолжить позже."
            ) from exc
        except OpenRouterSchemaError as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise
        except Exception as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise

    @retry(
        retry=retry_if_exception_type(OpenRouterRateLimitError),
        wait=wait_random_exponential(multiplier=1, max=15),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _call_with_retry(self, *, prompt: str, system_instruction: str) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                timeout=self.settings.openrouter_timeout_seconds,
            )
        except Exception as exc:
            logger.exception("OpenRouter request failed (%s): %s", self.model, exc)
            if _is_rate_limit_error(exc):
                raise OpenRouterRateLimitError(str(exc)) from exc
            raise

        content = response.choices[0].message.content
        if content is None:
            raise RuntimeError(f"OpenRouter ({self.model}) вернул пустой ответ (content=None)")
        return content

    def _parse_with_repair(self, raw_json: str, response_model: type[T]) -> T:
        try:
            return response_model.model_validate_json(raw_json)
        except (ValidationError, ValueError) as first_exc:
            repaired = _repair_json(raw_json)
            if repaired == raw_json:
                raise OpenRouterSchemaError(
                    f"OpenRouter ({self.model}) вернул невалидный JSON, repair не применим: {first_exc}"
                ) from first_exc
            try:
                parsed = response_model.model_validate_json(repaired)
                logger.warning("OpenRouter (%s): JSON с ошибками исправлен repair-слоем.", self.model)
                return parsed
            except (ValidationError, ValueError) as second_exc:
                raise OpenRouterSchemaError(
                    f"OpenRouter ({self.model}) вернул невалидный JSON даже после repair. "
                    f"Исходная ошибка: {first_exc}; после repair: {second_exc}"
                ) from second_exc