from __future__ import annotations

import logging
from dataclasses import dataclass

from llm.base import LLMClient
from llm.chunking import batch_for_quality_and_budget
from gemini.prompts.elaborator import SYSTEM_INSTRUCTION
from gemini.schemas import ElaborationOutput
from storage.models import Evidence, OutlineNote, OutlineSubpoint, Plan, TaskStatus

logger = logging.getLogger(__name__)

MODEL_KNOWLEDGE_SOURCE_ID = "model_knowledge"


@dataclass
class ElaborationUnit:
    note: OutlineNote
    subpoint: OutlineSubpoint


def build_elaboration_units(plan: Plan, already_done_subpoint_ids: set[str]) -> list[ElaborationUnit]:
    return [
        ElaborationUnit(note=note, subpoint=sp)
        for note in plan.notes
        for sp in note.subpoints
        if sp.subpoint_id not in already_done_subpoint_ids
    ]


def elaborate_outline(
    plan: Plan,
    client: LLMClient,
    status: TaskStatus,
    already_done_subpoint_ids: set[str],
    on_batch_done,
    max_subpoints_per_batch: int,
) -> None:
    """on_batch_done(subpoint_ids: list[str], new_evidence: list[Evidence])
    — вызывается после каждого батча; orchestrator обязан сохранить
    subpoint_ids в чекпоинт немедленно (тот же контракт, что был у
    extract_evidence_from_sources/elaborate_subtopics)."""
    units = build_elaboration_units(plan, already_done_subpoint_ids)
    if not units:
        return

    static_overhead = (
        f"Тема исследования: {plan.topic_title}\n\n"
        "Раскрой КАЖДЫЙ раздел ОТДЕЛЬНО, строго по его техзаданию. Для "
        "каждого факта укажи unit_index — номер раздела в квадратных "
        "скобках."
    )

    batches = batch_for_quality_and_budget(
        units,
        client=client,
        system_instruction=SYSTEM_INSTRUCTION,
        response_model=ElaborationOutput,
        render_item=lambda u: f"{u.note.title} :: {u.subpoint.heading}\n{u.subpoint.covers}",
        static_overhead_text=static_overhead,
        max_items_per_batch=max_subpoints_per_batch,
    )

    for batch in batches:
        evidence = _elaborate_batch(batch, plan, client, status)
        on_batch_done([u.subpoint.subpoint_id for u in batch], evidence)


def _elaborate_batch(
    units: list[ElaborationUnit], plan: Plan, client: LLMClient, status: TaskStatus
) -> list[Evidence]:
    if not units:
        return []

    listing = "\n\n".join(
        f"=== Раздел [{i}]: {u.note.title} :: {u.subpoint.heading} ===\n{u.subpoint.covers}"
        for i, u in enumerate(units)
    )
    prompt = (
        f"Тема исследования: {plan.topic_title}\n\n"
        f"Разделы для раскрытия:\n\n{listing}\n\n"
        "Раскрой КАЖДЫЙ раздел выше своими знаниями. Обязательно укажи "
        "unit_index для каждого факта."
    )

    output: ElaborationOutput = client.generate_structured(
        role="elaborator",
        prompt=prompt,
        response_model=ElaborationOutput,
        status=status,
        system_instruction=SYSTEM_INSTRUCTION,
    )

    evidence_list: list[Evidence] = []
    for item in output.evidence:
        if 0 <= item.unit_index < len(units):
            unit = units[item.unit_index]
        else:
            logger.warning(
                "Elaborator вернул unit_index=%d вне диапазона [0, %d) — "
                "приписываем факт первому разделу батча.",
                item.unit_index, len(units),
            )
            unit = units[0]
        evidence_list.append(
            Evidence(
                note_id=unit.note.note_id,
                subpoint_id=unit.subpoint.subpoint_id,
                statement=item.statement,
                source_id=MODEL_KNOWLEDGE_SOURCE_ID,
                confidence=item.confidence,
                is_definition=item.is_definition,
                critic_note=item.critic_note,
            )
        )
    return evidence_list


def elaborate_outline_sync(
    plan: Plan, client: LLMClient, status: TaskStatus, max_subpoints_per_batch: int = 6
) -> list[Evidence]:
    """Без чекпоинтинга — для тестов/прямых вызовов вне Orchestrator."""
    collected: list[Evidence] = []

    def _collect(_ids: list[str], new_evidence: list[Evidence]) -> None:
        collected.extend(new_evidence)

    elaborate_outline(
        plan, client, status, already_done_subpoint_ids=set(),
        on_batch_done=_collect, max_subpoints_per_batch=max_subpoints_per_batch,
    )
    return collected