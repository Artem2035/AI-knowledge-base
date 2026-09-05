"""
Структурная типизация (Protocol) для LLM-клиента. roles/*.py импортируют
этот тип для аннотаций вместо конкретного GeminiClient/GroqClient — клиент,
переданный в роль, может быть любым из них (см. llm/factory.py), и роли не
должны знать, какой именно.

Это только для статической типизации/читаемости — Python не проверяет это
в рантайме, оба клиента и так совместимы по duck typing.
"""
from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

from storage.models import TaskStatus

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    def generate_structured(
        self,
        *,
        role: str,
        prompt: str,
        response_model: type[T],
        status: TaskStatus,
        system_instruction: str | None = None,
    ) -> T: ...
