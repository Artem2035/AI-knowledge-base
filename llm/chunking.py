"""
Общая утилита для деления списка элементов на батчи под доступный
prompt-бюджет клиента. Нужна, чтобы избежать silent auto-truncate
(GroqClient._auto_truncate_prompt режет промпт с конца — для списков
кандидатов это означает потерю "хвостовых" элементов до того, как их
вообще увидела модель).

Для клиентов без available_prompt_budget_tokens() (сейчас — GeminiClient,
у него существенно более широкие лимиты) деление не нужно — возвращается
один батч со всеми элементами, поведение не меняется.
"""
from __future__ import annotations

from typing import Callable, Sequence, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


def split_items_into_batches(
    items: Sequence[T],
    *,
    client,
    system_instruction: str,
    response_model: type[BaseModel],
    render_item: Callable[[T], str],
    static_overhead_text: str = "",
) -> list[list[T]]:
    if not items:
        return []

    budget_fn = getattr(client, "available_prompt_budget_tokens", None)
    if budget_fn is None:
        return [list(items)]

    from llm.groq_client import _estimate_tokens  # та же эвристика, что и у клиента

    available_tokens = budget_fn(system_instruction, response_model)
    overhead_tokens = _estimate_tokens(static_overhead_text)
    text_budget_tokens = max(available_tokens - overhead_tokens, 0)

    if text_budget_tokens <= 0:
        # бюджета не хватает даже без элементов — пусть каждый идёт
        # отдельным батчем, дальше клиент сам решит (auto-truncate/ошибка),
        # но хотя бы не потеряем весь список сразу
        return [[item] for item in items]

    batches: list[list[T]] = []
    current: list[T] = []
    current_tokens = 0

    for item in items:
        item_tokens = _estimate_tokens(render_item(item))
        if current and current_tokens + item_tokens > text_budget_tokens:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(item)
        current_tokens += item_tokens

    if current:
        batches.append(current)

    return batches