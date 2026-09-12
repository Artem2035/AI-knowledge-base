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

def batch_for_quality_and_budget(
    items: Sequence[T],
    *,
    client,
    system_instruction: str,
    response_model: type[BaseModel],
    render_item: Callable[[T], str],
    static_overhead_text: str,
    max_items_per_batch: int,
) -> list[list[T]]:
    """
    Комбинирует два НЕЗАВИСИМЫХ предела на размер батча:
    1) токен-бюджет (split_items_into_batches) — как раньше;
    2) качественный потолок max_items_per_batch — не про бюджет, а про то,
       что при большом числе элементов в одном вызове модель скатывается в
       однострочные поверхностные ответы на каждый (напр. заметка с 30
       подпунктами).

    Порядок элементов сохраняется, поэтому границы батчей чаще всего
    совпадают с границами заметок. Исключение: если шаг (1) уже разрезал
    ПОСЕРЕДИНЕ заметки по бюджету (при длинных элементах) — тогда шаг (2)
    режет уже этот кусок, и последний под-батч может оказаться короче
    max_items_per_batch. Для коротких heading+covers это практически не
    происходит.
    """
    budget_batches = split_items_into_batches(
        items, client=client, system_instruction=system_instruction,
        response_model=response_model, render_item=render_item,
        static_overhead_text=static_overhead_text,
    )
    final_batches: list[list[T]] = []
    for batch in budget_batches:
        for i in range(0, len(batch), max_items_per_batch):
            final_batches.append(batch[i : i + max_items_per_batch])
    return final_batches
