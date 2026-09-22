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

ИЗМЕНЕНИЯ (v2) — унификация с GroqClient через llm/common.py:
1. JSON-repair (_repair_json/_WORD_DIGITS) и распознавание типа ошибки по
   тексту (429/413) больше не дублируются здесь текстом — импортируются
   из llm/common.py как общие, провайдер-нейтральные утилиты.
2. Классы исключений (OpenRouterRateLimitError/OpenRouterSchemaError)
   теперь наследуются от общих LLMRateLimitError/LLMSchemaError
   (llm/common.py) — это КРИТИЧНО для ролей вроде
   roles/extractor_critic.py, которые ловят ошибки по типу: раньше они
   ловили только Groq*-специфичные классы и пропускали OpenRouter*-ошибки
   необработанными при переключении провайдера.
3. Добавлен OpenRouterPromptTooLargeError (413 / промпт больше бюджета
   контекста) — раньше такого случая не существовало вовсе, ошибка ушла
   бы наружу как сырой Exception от OpenAI SDK.
4. Добавлен available_prompt_budget_tokens() — раньше отсутствовал,
   из-за чего llm/chunking.py::split_items_into_batches для OpenRouter
   не резал элементы по токен-бюджету вообще (возвращал один батч со
   всеми элементами при любом объёме).
5. Ретрай (tenacity) расширен на APIConnectionError/APITimeoutError —
   раньше ретраился только rate limit, любой сетевой сбой падал наружу
   необработанным (тот же паттерн, что уже был у GroqClient).
"""
from __future__ import annotations

import json
import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from openai import APIConnectionError, APITimeoutError

from config.settings import Settings
from llm.common import (LLMRateLimitError, LLMSchemaError, LLMPromptTooLargeError,
                        LLMSchemaError,
                        is_rate_limit_error,
                        is_request_too_large_error,
                        parse_retry_after,
                        repair_json, estimate_tokens)

from orchestrator.budget import LLMBudget, LLMFreeLimitReached
from storage.models import TaskStatus

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class OpenRouterRateLimitError(LLMRateLimitError):
    """Оборачивает 429 от OpenRouter API для retry-логики tenacity."""


class OpenRouterSchemaError(LLMSchemaError):
    """JSON от модели невалиден даже после repair-попыток. Тот же
    контракт, что и у любого другого LLMSchemaError — вызывающий код
    (extractor_critic и т.п.) может уменьшить батч, а не просто ретраить
    тот же запрос."""


class OpenRouterPromptTooLargeError(LLMPromptTooLargeError):
    """413 от API, либо промпт заведомо больше context_budget_tokens
    (см. OpenRouterClient.available_prompt_budget_tokens)."""

class OpenRouterProviderOverloadedError(Exception):
    """Апстрим-провайдер модели (за OpenRouter) временно перегружен —
    ответ вида {"choices": None, "error": {"code": 503, "metadata":
    {"error_type": "provider_overloaded"}}}. Retryable и failover-triggering
    (см. llm/multi_model_client.py) — в отличие от OpenRouterSchemaError,
    это не проблема качества ответа модели, а временная недоступность
    инфраструктуры за конкретной моделью."""


class OpenRouterClient:
    # Свободные модели OpenRouter в проекте заявляют контекст 256K-1M
    # токенов (см. config/settings.py::openrouter_planning_model /
    # openrouter_writing_model). Дефолт здесь НАМНОГО консервативнее
    # реального лимита контекста — не из-за технического ограничения, а
    # потому что бесплатные модели на очень больших батчах в одном вызове
    # чаще теряют структуру ответа (тот же практический риск, из-за
    # которого в elaborator/roles введён max_subpoints_per_generation_batch
    # как отдельный, качественный, а не только токенный потолок — см.
    # llm/chunking.py::batch_for_quality_and_budget). Можно переопределить
    # через context_budget_tokens при создании клиента (см.
    # llm/factory.py) под конкретную модель.
    DEFAULT_CONTEXT_BUDGET_TOKENS = 60_000
    RESERVED_OUTPUT_TOKENS = 2000

    def __init__(
        self,
        settings: Settings,
        budget: LLMBudget,
        model: str,
        context_budget_tokens: int | None = None,
    ):
        settings.validate_free_only()
        if not settings.openrouter_api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY не задан. Получите бесплатный ключ на "
                "https://openrouter.ai/keys и укажите в .env"
            )
        self.settings = settings
        self.budget = budget
        self.model = model
        self.context_budget_tokens = context_budget_tokens or self.DEFAULT_CONTEXT_BUDGET_TOKENS

        from openai import OpenAI  # локальный импорт, как в GroqClient —
        # модуль не должен требовать пакет, если провайдер не используется.

        self._client = OpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
        )

    def available_prompt_budget_tokens(
        self,
        system_instruction: str,
        response_model: type[BaseModel],
    ) -> int:
        """Сколько токенов остаётся под сам prompt (без system/schema/output).
        Используется llm/chunking.py::split_items_into_batches, чтобы резать
        длинные списки элементов на батчи под доступный бюджет контекста —
        тот же контракт, что у GroqClient.available_prompt_budget_tokens,
        только без реального TPM-лимитера под капотом (см. докстринг
        модуля, п.4 «Изменения»)."""
        schema_hint = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        overhead = estimate_tokens(system_instruction) + estimate_tokens(schema_hint)
        budget = self.context_budget_tokens - overhead - self.RESERVED_OUTPUT_TOKENS
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
              "следующей JSON Schema. Включай ВСЕ поля из схемы (используй "
              "\"\" или [] для неприменимых), без markdown-разметки и текста "
              "до/после JSON. Все числа пиши только цифрами (0.9), никогда "
              "словами.\n\nJSON Schema:\n" + schema_hint
        )

        # Проактивная проверка бюджета ДО сетевого вызова — тот же принцип,
        # что и в GroqClient.generate_structured (см. groq_client.py):
        # лучше явная GroqPromptTooLargeError-совместимая ошибка, которую
        # вызывающий код (extractor_critic и т.п.) умеет ловить и резать
        # батч, чем 413 от API постфактум.
        prompt_tokens = estimate_tokens(prompt)
        available = self.available_prompt_budget_tokens(system_instruction or "", response_model)
        if prompt_tokens > available > 0:
            # Не падаем сразу — OpenRouter-модели с их 256K-1M контекстом
            # почти всегда физически вмещают промпт даже при превышении
            # нашего консервативного DEFAULT_CONTEXT_BUDGET_TOKENS, поэтому
            # только предупреждаем и пробуем отправить как есть; реальный
            # 413 (если он всё-таки случится) будет корректно превращён в
            # OpenRouterPromptTooLargeError ниже, в _call_with_retry.
            logger.info(
                "OpenRouter (%s): промпт роли '%s' (~%d токенов) превышает "
                "консервативный бюджет ~%d токенов — отправляем как есть, "
                "полагаясь на реальный контекст модели.",
                self.model, role, prompt_tokens, available,
            )

        try:
            raw_json = self._call_with_retry(prompt=prompt, system_instruction=full_system)
            parsed = self._parse_with_repair(raw_json, response_model)
            self.budget.register_call(status, role=role, ok=True)
            return parsed
        except (OpenRouterRateLimitError, OpenRouterProviderOverloadedError, OpenRouterSchemaError) as exc:
            # Решение "сдаться окончательно или попробовать следующую
            # модель-кандидата" принимает MultiModelOpenRouterClient.
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise
        except Exception as exc:
            self.budget.register_call(status, role=role, ok=False, error=str(exc))
            raise

    # Тот же принцип, что и у GroqClient._RETRYABLE_EXCEPTIONS: ретраим не
    # только rate limit, но и обрывы соединения/таймауты — раньше здесь
    # ретраился только OpenRouterRateLimitError, и любой сетевой сбой
    # (обрыв TLS, таймаут) падал наружу необработанным с первой попытки.
    _RETRYABLE_EXCEPTIONS = (OpenRouterRateLimitError, APIConnectionError, APITimeoutError)

    @retry(
        retry=retry_if_exception_type((OpenRouterRateLimitError, OpenRouterProviderOverloadedError)),
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
            if is_rate_limit_error(exc):
                raise OpenRouterRateLimitError(str(exc)) from exc
            raise

        code, message, error_type = _extract_error_info(response)
        if code is not None:
            if code == 503 or error_type == "provider_overloaded":
                raise OpenRouterProviderOverloadedError(
                    f"OpenRouter ({self.model}): апстрим-провайдер временно "
                    f"перегружен — {message} (code={code})"
                )
            if is_rate_limit_error(message or ""):
                raise OpenRouterRateLimitError(message or f"rate limited (code={code})")
            raise RuntimeError(f"OpenRouter ({self.model}) вернул ошибку: {message} (code={code})")

        choices = getattr(response, "choices", None)
        if not choices:
            raise RuntimeError(
                f"OpenRouter ({self.model}) вернул пустой choices без явного "
                f"поля error в ответе: {response}"
            )

        content = choices[0].message.content
        if content is None:
            raise RuntimeError(f"OpenRouter ({self.model}) вернул пустой ответ (content=None)")
        return content

    def _parse_with_repair(self, raw_json: str, response_model: type[T]) -> T:
        try:
            return response_model.model_validate_json(raw_json)
        except (ValidationError, ValueError) as first_exc:
            repaired = repair_json(raw_json)
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

def _extract_error_info(response) -> tuple[int | None, str | None, str | None]:
    err = getattr(response, "error", None)
    if not err:
        return None, None, None
    if isinstance(err, dict):
        code = err.get("code")
        message = err.get("message")
        metadata = err.get("metadata") or {}
    else:
        code = getattr(err, "code", None)
        message = getattr(err, "message", None)
        metadata = getattr(err, "metadata", None) or {}
    error_type = (
        metadata.get("error_type") if isinstance(metadata, dict)
        else getattr(metadata, "error_type", None)
    )
    return code, message, error_type