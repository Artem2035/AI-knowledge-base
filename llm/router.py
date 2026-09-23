"""
RoleRoutingLLMClient — маршрутизирует generate_structured(role=...) на группу
LLMClient-кандидатов в зависимости от переданной роли, сам при этом
реализуя тот же Protocol (llm/base.py::LLMClient) — вызывающий код
(roles/*.py, orchestrator/state_machine.py) не видит разницы между "один
клиент на всё" (Groq) и "список моделей-кандидатов с failover под роль"
(OpenRouter: Nemotron/GLM/Inkling для planning-ролей, Gemma для Writer).

ИЗМЕНЕНИЕ: слито с прежним отдельным llm/multi_model_client.py::
MultiModelOpenRouterClient. Раньше это были два файла, отвечавшие на два
разных, но тесно связанных вопроса:
  - router.py            — "какая РОЛЬ -> какой клиент/группа?"
  - multi_model_client.py — "клиент из группы недоступен -> следующий?"
По факту это один и тот же логический вопрос ("какой конкретно клиент
обслужит этот вызов роли"), решаемый в два прохода. Слияние:
  1. убирает файл и дублирование структуры (оба класса реализовывали
     один и тот же LLMClient Protocol и делегировали
     available_prompt_budget_tokens похожим образом);
  2. делает failover ПРОВАЙДЕР-НЕЙТРАЛЬНЫМ: раньше
     MultiModelOpenRouterClient ловил OpenRouter-специфичные классы
     исключений напрямую (OpenRouterRateLimitError и т.п.) — теперь
     ловятся только общие типы из llm/common.py, поэтому тот же
     failover-механизм заработает для ЛЮБОГО будущего провайдера со
     списком моделей-кандидатов, не только OpenRouter, без изменения
     этого файла (см. llm/factory.py про точку расширения).

Groq в MVP использует один клиент без списка кандидатов (нет
альтернативных бесплатных моделей с другим TPM-профилем в проекте) —
тогда группа вырождается в список из одного элемента и failover-цикл
просто не имеет, по кому переключаться (эквивалентно прежнему прямому
использованию GroqClient).

Никакой логики бюджета/лимитов здесь нет намеренно — каждый клиент в
группе уже инкапсулирует свой собственный LLMBudget (см.
GroqClient/OpenRouterClient), роутер — чистый диспетчер + failover.
"""
from __future__ import annotations

import logging
from typing import TypeVar

from pydantic import BaseModel

from llm.base import LLMClient
from llm.common import LLMRateLimitError, LLMSchemaError, LLMProviderOverloadedError
from orchestrator.budget import LLMFreeLimitReached
from storage.models import TaskStatus

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Исключения, при которых имеет смысл попробовать СЛЕДУЮЩЕГО кандидата в
# группе, а не просто ретраить того же самого. Все провайдер-нейтральные
# (см. llm/common.py) — конкретный провайдер-специфичный класс (например,
# OpenRouterRateLimitError) наследуется от одного из них, поэтому ловится
# здесь без явного упоминания провайдера.
#
# LLMFreeLimitReached — отдельный случай: например, исчерпан наш
# СОБСТВЕННЫЙ soft-лимит RPD именно у ЭТОЙ модели-кандидата (см.
# orchestrator/budget.py) — у следующего кандидата в группе может быть
# свой независимый бюджет, поэтому тоже триггерит переключение.
#
# LLMPromptTooLargeError НЕ входит: слишком большой промпт — не повод
# пробовать другую модель того же класса задач (у них обычно похожий
# контекст), это должен решать вызывающий код (чанкинг), а не failover.
_FAILOVER_TRIGGERS = (LLMRateLimitError, LLMSchemaError, LLMProviderOverloadedError, LLMFreeLimitReached)


def _as_group(client_or_group: LLMClient | list[LLMClient]) -> list[LLMClient]:
    return client_or_group if isinstance(client_or_group, list) else [client_or_group]


class RoleRoutingLLMClient:
    def __init__(
        self,
        default_client: LLMClient | list[LLMClient],
        role_map: dict[str, LLMClient | list[LLMClient]] | None = None,
        selection_mode: str = "auto",
    ):
        """
        default_client / значения role_map могут быть либо ОДНИМ клиентом
        (прежнее поведение router.py — например, Groq, где нет
        альтернативных моделей), либо СПИСКОМ клиентов-кандидатов в
        порядке приоритета (прежнее поведение
        multi_model_client.py::MultiModelOpenRouterClient — например,
        группа моделей OpenRouter под одну роль). Оба случая
        нормализуются в список одинаковой длины (1 или больше) через
        _as_group.

        selection_mode (тот же контракт, что раньше был у
        MultiModelOpenRouterClient, см. config/settings.py::
        openrouter_selection_mode):
        - "auto" (по умолчанию) — при сбое текущего кандидата (см.
          _FAILOVER_TRIGGERS) автоматически пробуется следующий в группе
          для ЭТОЙ роли.
        - "manual" — используется ТОЛЬКО первый кандидат каждой группы;
          при его сбое — контролируемая остановка (LLMFreeLimitReached) с
          подсказкой про резервные модели в конфиге, без автоматического
          переключения — смена модели остаётся явным решением
          пользователя.
        """
        self._default_group = _as_group(default_client)
        self._role_groups: dict[str, list[LLMClient]] = {
            role: _as_group(client) for role, client in (role_map or {}).items()
        }
        self._mode = selection_mode

    def _group_for_role(self, role: str) -> list[LLMClient]:
        return self._role_groups.get(role, self._default_group)

    def _active_candidates(self, group: list[LLMClient]) -> list[LLMClient]:
        # В manual-режиме задействуем ТОЛЬКО первого кандидата группы —
        # остальные остаются в конфиге как резерв/подсказка, но не
        # вызываются автоматически (см. remaining_hint ниже).
        return group if self._mode == "auto" else group[:1]

    def generate_structured(
        self,
        *,
        role: str,
        prompt: str,
        response_model: type[T],
        status: TaskStatus,
        system_instruction: str | None = None,
    ) -> T:
        group = self._group_for_role(role)
        active = self._active_candidates(group)

        last_exc: Exception | None = None
        for i, client in enumerate(active):
            try:
                result = client.generate_structured(
                    role=role, prompt=prompt, response_model=response_model,
                    status=status, system_instruction=system_instruction,
                )
                if i > 0:
                    logger.info("Роль '%s': ответил резервный кандидат #%d.", role, i)
                return result
            except _FAILOVER_TRIGGERS as exc:
                last_exc = exc
                if self._mode == "auto" and i < len(active) - 1:
                    logger.warning(
                        "Роль '%s': кандидат #%d недоступен (%s) — пробуем следующего.",
                        role, i, exc,
                    )
                    continue
                break

        remaining_hint = ""
        if self._mode == "manual" and len(group) > 1:
            remaining_hint = (
                f" В конфиге есть {len(group) - 1} резервных кандидатов для "
                f"роли '{role}', но selection_mode=manual — переключение "
                "требует явного решения (смените модель в конфиге и "
                "повторите resume)."
            )

        raise LLMFreeLimitReached(
            f"Роль '{role}': все доступные бесплатные кандидаты "
            f"({len(active)}) сейчас недоступны. Последняя ошибка: {last_exc}."
            f"{remaining_hint} Задача остановлена, прогресс сохранён — можно "
            "продолжить resume."
        ) from last_exc

    def available_prompt_budget_tokens(self, system_instruction: str, response_model: type[BaseModel]):
        """Используется только llm/chunking.py::split_items_into_batches.
        Делегируем ПЕРВОМУ кандидату дефолтной группы, если тот умеет
        отвечать на этот вопрос; иначе — как и для клиентов без этого
        метода вообще (см. llm/chunking.py) — батчинг по бюджету токенов
        просто не применяется, элементы уходят одним батчем.

        ВАЖНО (исправление бага): раньше (в multi_model_client.py) здесь
        по ошибке делался getattr на весь список кандидатов (self._active)
        вместо одного клиента — у list нет метода
        available_prompt_budget_tokens, поэтому fn всегда был None, метод
        всегда молча возвращал None, а split_items_into_batches падал с
        TypeError на `None - overhead_tokens`. Теперь берём конкретного
        клиента _default_group[0]."""
        if not self._default_group:
            return None
        client = self._default_group[0]
        fn = getattr(client, "available_prompt_budget_tokens", None)
        return fn(system_instruction, response_model) if fn else None