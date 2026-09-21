"""
RoleRoutingLLMClient — маршрутизирует generate_structured(role=...) на
РАЗНЫЕ LLMClient в зависимости от переданной роли, сам при этом реализуя
тот же Protocol (llm/base.py::LLMClient) — вызывающий код (roles/*.py,
orchestrator/state_machine.py) не видит разницы между "один клиент на всё"
(как раньше с Groq) и "конкретная модель под конкретную роль" (OpenRouter:
Nemotron 3 Super для reasoning-ролей (planner/elaborator/critic/...),
Gemma 4 для Writer — см. config/settings.py::openrouter_* и
llm/factory.py::_create_openrouter_router).

Никакой логики бюджета/лимитов здесь нет намеренно — каждый клиент в
role_map/default_client уже инкапсулирует свой собственный LLMBudget (см.
GroqClient/OpenRouterClient), роутер — чистый диспетчер.
"""
from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from llm.base import LLMClient
from storage.models import TaskStatus

T = TypeVar("T", bound=BaseModel)


class RoleRoutingLLMClient:
    def __init__(self, default_client: LLMClient, role_map: dict[str, LLMClient] | None = None):
        self._default = default_client
        self._role_map = role_map or {}

    def generate_structured(
        self,
        *,
        role: str,
        prompt: str,
        response_model: type[T],
        status: TaskStatus,
        system_instruction: str | None = None,
    ) -> T:
        client = self._role_map.get(role, self._default)
        return client.generate_structured(
            role=role,
            prompt=prompt,
            response_model=response_model,
            status=status,
            system_instruction=system_instruction,
        )

    def available_prompt_budget_tokens(self, system_instruction: str, response_model: type[BaseModel]):
        """Используется только llm/chunking.py::split_items_into_batches
        (RESEARCH_MODE=web, extractor_critic — сейчас не активен в
        knowledge-режиме). Делегируем дефолтному (planning) клиенту, если
        тот умеет отвечать на этот вопрос; иначе — как и для клиентов без
        этого метода вообще (см. llm/chunking.py) — батчинг по бюджету
        токенов просто не применяется, элементы уходят одним батчем."""
        fn = getattr(self._default, "available_prompt_budget_tokens", None)
        return fn(system_instruction, response_model) if fn else None