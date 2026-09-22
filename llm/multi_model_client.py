"""
MultiModelOpenRouterClient — оборачивает упорядоченный список
OpenRouterClient (один на модель-кандидата для роли/группы ролей) и
реализует тот же Protocol (llm/base.py::LLMClient) — RoleRoutingLLMClient
не видит разницы между "одна модель" и "список кандидатов с failover".

Режим — config/settings.py::openrouter_selection_mode:
- "auto" — при сбое текущей модели (перегрузка апстрима, rate limit,
  невалидный JSON даже после repair, исчерпанный RPD именно у этой
  модели) автоматически пробует следующую модель из списка.
- "manual" — failover выключен: используется только первая модель
  списка; при её сбое — остановка с подсказкой про резервные модели.

В обоих режимах, если ни одна доступная модель не ответила, поднимается
LLMFreeLimitReached — тот же тип, что Orchestrator уже ловит и превращает
в контролируемую остановку с сохранением чекпоинта. Прогресс не
теряется: resume позже, когда модель/провайдер восстановится.

ВАЖНО: LLMTaskBudgetExceeded (MAX_LLM_CALLS_PER_TASK) сознательно НЕ
входит в список failover-триггеров — это общий потолок вызовов на
задачу (единый TaskStatus для всех кандидатов), смена модели её не
решает.
"""
from __future__ import annotations

import logging
from typing import TypeVar

from pydantic import BaseModel

from llm.openrouter_client import (
    OpenRouterClient,
    OpenRouterProviderOverloadedError,
    OpenRouterRateLimitError,
    OpenRouterSchemaError,
)
from orchestrator.budget import LLMFreeLimitReached
from storage.models import TaskStatus

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

_FAILOVER_TRIGGERS = (
    OpenRouterRateLimitError,
    OpenRouterProviderOverloadedError,
    OpenRouterSchemaError,
    LLMFreeLimitReached,  # напр. RPD soft-limit ЭТОЙ модели — у другой может быть свой бюджет
)


class MultiModelOpenRouterClient:
    def __init__(self, candidates: list[OpenRouterClient], selection_mode: str = "auto"):
        if not candidates:
            raise ValueError("MultiModelOpenRouterClient требует минимум одного кандидата.")
        self._candidates = candidates
        self._mode = selection_mode
        # В manual-режиме задействуем ТОЛЬКО первую модель — остальные
        # остаются в конфиге для подсказки пользователю, но не вызываются
        # автоматически.
        self._active = candidates if selection_mode == "auto" else candidates[:1]

    def generate_structured(
        self, *, role: str, prompt: str, response_model: type[T],
        status: TaskStatus, system_instruction: str | None = None,
    ) -> T:
        last_exc: Exception | None = None
        for i, client in enumerate(self._active):
            try:
                result = client.generate_structured(
                    role=role, prompt=prompt, response_model=response_model,
                    status=status, system_instruction=system_instruction,
                )
                if i > 0:
                    logger.info(
                        "Роль '%s': ответила резервная модель %s.", role, client.model,
                    )
                return result
            except _FAILOVER_TRIGGERS as exc:
                last_exc = exc
                if self._mode == "auto" and i < len(self._active) - 1:
                    logger.warning(
                        "Роль '%s': модель %s недоступна (%s) — пробуем %s.",
                        role, client.model, exc, self._active[i + 1].model,
                    )
                    continue
                break

        remaining_hint = ""
        if self._mode == "manual" and len(self._candidates) > 1:
            other_models = ", ".join(c.model for c in self._candidates[1:])
            remaining_hint = (
                f" В конфиге есть резервные модели ({other_models}), но "
                "OPENROUTER_SELECTION_MODE=manual — переключение требует "
                "вашего явного решения (смените основную модель роли в .env "
                "и повторите resume)."
            )

        raise LLMFreeLimitReached(
            f"Роль '{role}': все доступные бесплатные модели OpenRouter "
            f"({', '.join(c.model for c in self._active)}) сейчас недоступны. "
            f"Последняя ошибка: {last_exc}.{remaining_hint} "
            "Задача остановлена, прогресс сохранён — можно продолжить resume."
        ) from last_exc

    def available_prompt_budget_tokens(self, system_instruction: str, response_model: type[BaseModel]):
        """Используется только llm/chunking.py::split_items_into_batches
        (RESEARCH_MODE=web, extractor_critic — сейчас не активен в
        knowledge-режиме). Делегируем дефолтному (planning) клиенту, если
        тот умеет отвечать на этот вопрос; иначе — как и для клиентов без
        этого метода вообще (см. llm/chunking.py) — батчинг по бюджету
        токенов просто не применяется, элементы уходят одним батчем."""
        fn = getattr(self._active, "available_prompt_budget_tokens", None)
        return fn(system_instruction, response_model) if fn else None