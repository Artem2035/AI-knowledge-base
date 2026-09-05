"""
GeminiClient — единственный модуль, который физически обращается к сети для
LLM-вызовов. Ни одна роль не должна импортировать google.genai напрямую.

Ответственность:
- structured JSON output (Pydantic response_schema)
- retry с exponential backoff + jitter при 429/5xx (через tenacity)
- строгая остановка (без fallback на платный tier) при устойчивом
  исчерпании лимита
- ведение бюджета вызовов совместно с orchestrator.budget.GeminiBudget
"""
from __future__ import annotations

import logging
from typing import TypeVar

from pydantic import BaseModel
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


class GeminiRateLimitError(Exception):
    """Оборачивает 429 от API для retry-логики tenacity."""


class GeminiClient:
    def __init__(self, settings: Settings, budget: GeminiBudget):
        settings.validate_free_only()  # жёсткая проверка FREE_ONLY при создании клиента
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY не задан. Укажите бесплатный ключ из "
                "https://aistudio.google.com/apikey в .env"
            )
        self.settings = settings
        self.budget = budget

        from google import genai  # локальный импорт — чтобы модуль импортировался
        # даже без установленного пакета (например, при тестировании прочей логики)

        self._genai = genai
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def generate_structured(
        self,
        *,
        role: str,
        prompt: str,
        response_model: type[T],
        status: TaskStatus,
        system_instruction: str | None = None,
    ) -> T:
        """
        Основной метод для всех ролей: делает 1 (иногда единственный
        допустимый) вызов Gemini с structured output и возвращает
        валидированный Pydantic-объект.
        """
        self.budget.check_and_register_task_call(status)
        self.budget.check_rpd_soft_limit()
        self.budget.wait_if_needed_for_rpm()

        try:
            raw_json = self._call_with_retry(
                prompt=prompt,
                system_instruction=system_instruction,
                response_schema=response_model,
            )
            parsed = response_model.model_validate_json(raw_json)
            self.budget.register_call(status, role=role, ok=True)
            return parsed
        except GeminiRateLimitError as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise GeminiFreeLimitReached(
                "Свободный лимит Gemini API исчерпан (устойчивая 429 после retry). "
                "Задача остановлена. FREE_ONLY=true запрещает переход на платный API. "
                "Прогресс сохранён — можно продолжить позже."
            ) from exc
        except Exception as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise

    @retry(
        retry=retry_if_exception_type(GeminiRateLimitError),
        wait=wait_random_exponential(multiplier=1, max=30),
        stop=stop_after_attempt(4),  # 1 первая попытка + до 3 повторов = MAX_GEMINI_RETRIES по умолчанию
        reraise=True,
    )
    def _call_with_retry(
        self, *, prompt: str, system_instruction: str | None, response_schema: type[BaseModel]
    ) -> str:
        try:
            config: dict = {
                "response_mime_type": "application/json",
                "response_schema": response_schema,
            }
            if system_instruction:
                config["system_instruction"] = system_instruction

            response = self._client.models.generate_content(
                model=self.settings.gemini_model,
                contents=prompt,
                config=config,
            )
            return response.text
        except Exception as exc:
            if _is_rate_limit_error(exc):
                raise GeminiRateLimitError(str(exc)) from exc
            raise


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "resource_exhausted" in text or "rate limit" in text
