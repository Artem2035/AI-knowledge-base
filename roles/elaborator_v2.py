from __future__ import annotations

import logging
from dataclasses import dataclass

from llm.base import LLMClient
from llm.chunking import batch_for_quality_and_budget
from llm.common import LLMPromptTooLargeError, LLMSchemaError
from llm.prompts.elaborator_v2 import get_system_instruction
from llm.schemas import SectionBatchOutput
from storage.models import OutlineNote, OutlineSubpoint, Plan, SectionDraft, TaskStatus
from tools.note_assembly import PLACEHOLDER_MARKDOWN, close_unbalanced_fence, prepare_section

logger = logging.getLogger(__name__)

# Сколько раз делим батч пополам при LLMSchemaError (как в extractor_critic).
_MAX_SPLIT_DEPTH = 2
# Сколько заголовков соседних разделов показываем модели («не повторяй их»).
_MAX_NEIGHBOR_HEADINGS = 15


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


def _render_unit(u: ElaborationUnit, index: int | None = None) -> str:
    head = f"=== Раздел [{index}] ===\n" if index is not None else ""
    neighbors = [
        sp.heading for sp in u.note.subpoints if sp.subpoint_id != u.subpoint.subpoint_id
    ][:_MAX_NEIGHBOR_HEADINGS]
    return (
        f"{head}Заметка: {u.note.title}\n"
        f"Заголовок раздела: {u.subpoint.heading}\n"
        f"Тип (kind): {u.subpoint.kind}\n"
        f"Техзадание: {u.subpoint.covers}\n"
        f"Другие разделы этой заметки (не повторяй их): {', '.join(neighbors) or '—'}"
    )


def _frame(plan: Plan) -> str:
    return (
        f"Тема исследования: {plan.topic_title}\n"
        f"Домен: {plan.domain}\n\n"
        "Напиши markdown КАЖДОГО раздела ниже ОТДЕЛЬНО, строго по его "
        "техзаданию и типу. Для каждого раздела укажи unit_index — номер "
        "раздела в квадратных скобках."
    )


def _placeholder(u: ElaborationUnit) -> SectionDraft:
    logger.warning(
        "Раздел «%s :: %s» не получен от модели — подставлен placeholder (needs_check).",
        u.note.title, u.subpoint.heading,
    )
    return SectionDraft(
        note_id=u.note.note_id, subpoint_id=u.subpoint.subpoint_id,
        markdown=PLACEHOLDER_MARKDOWN, needs_check=True,
    )


def elaborate_outline(
    plan: Plan,
    client: LLMClient,
    status: TaskStatus,
    already_done_subpoint_ids: set[str],
    on_batch_done,
    max_subpoints_per_batch: int,
) -> None:
    """on_batch_done(subpoint_ids: list[str], sections: list[SectionDraft]) —
    вызывается после каждого батча; в нём ВСЕ подпункты батча (при сбое —
    с placeholder), Orchestrator обязан немедленно персистить их в чекпоинт."""
    units = build_elaboration_units(plan, already_done_subpoint_ids)
    if not units:
        return

    system_instruction = get_system_instruction(plan.domain)
    batches = batch_for_quality_and_budget(
        units,
        client=client,
        system_instruction=system_instruction,
        response_model=SectionBatchOutput,
        render_item=_render_unit,
        static_overhead_text=_frame(plan),
        max_items_per_batch=max_subpoints_per_batch,
    )
    for batch in batches:
        sections = _elaborate_batch(batch, plan, client, status, system_instruction)
        on_batch_done([u.subpoint.subpoint_id for u in batch], sections)


def _request_sections(
    units: list[ElaborationUnit], plan: Plan, client: LLMClient, status: TaskStatus, system_instruction: str,
) -> SectionBatchOutput:
    listing = "\n\n".join(_render_unit(u, i) for i, u in enumerate(units))
    prompt = f"{_frame(plan)}\n\nРазделы для написания:\n\n{listing}"
    return client.generate_structured(
        role="elaborator",
        prompt=prompt,
        response_model=SectionBatchOutput,
        status=status,
        system_instruction=system_instruction,
    )


def _split_and_retry(units, plan, client, status, system_instruction, *, depth, final) -> list[SectionDraft]:
    mid = len(units) // 2
    return (
        _elaborate_batch(units[:mid], plan, client, status, system_instruction, depth=depth, final=final)
        + _elaborate_batch(units[mid:], plan, client, status, system_instruction, depth=depth, final=final)
    )


def _elaborate_batch(
    units: list[ElaborationUnit], plan: Plan, client: LLMClient, status: TaskStatus,
    system_instruction: str, *, depth: int = 0, final: bool = False,
) -> list[SectionDraft]:
    """Возвращает секцию для КАЖДОГО unit (при неудаче — placeholder).
    final=True — повторная попытка: дальнейших повторов за пропавшими
    разделами нет, незакрытый fence закрывается кодом с needs_check."""
    if not units:
        return []

    try:
        output = _request_sections(units, plan, client, status, system_instruction)
    except LLMPromptTooLargeError:
        if len(units) <= 1:
            return [_placeholder(units[0])]
        return _split_and_retry(units, plan, client, status, system_instruction, depth=depth, final=final)
    except LLMSchemaError as exc:
        if depth >= _MAX_SPLIT_DEPTH or len(units) <= 1:
            logger.warning("Батч из %d разделов пропущен: невалидный JSON (%s).", len(units), exc)
            return [_placeholder(u) for u in units]
        logger.info("LLMSchemaError на батче из %d разделов — делим пополам (depth=%d).", len(units), depth)
        return _split_and_retry(units, plan, client, status, system_instruction, depth=depth + 1, final=final)

    accepted, missing = _accept_sections(output, units, final=final)
    if missing and not final:
        # Один повтор только за пропавшими/битыми разделами.
        retried = _elaborate_batch(
            [units[i] for i in missing], plan, client, status, system_instruction, depth=depth, final=True,
        )
        accepted.update(dict(zip(missing, retried)))
    else:
        accepted.update({i: _placeholder(units[i]) for i in missing})
    return [accepted[i] for i in range(len(units))]


def _accept_sections(
    output: SectionBatchOutput, units: list[ElaborationUnit], *, final: bool,
) -> tuple[dict[int, SectionDraft], list[int]]:
    """Принимает валидные секции. Индекс вне диапазона и дубли отбрасываются
    (приписывать текст «первому разделу» нельзя — это портит содержимое)."""
    accepted: dict[int, SectionDraft] = {}
    for item in output.sections:
        i = item.unit_index
        if not (0 <= i < len(units)):
            logger.warning("Модель вернула unit_index=%d вне диапазона [0, %d) — секция отброшена.", i, len(units))
            continue
        if i in accepted:
            logger.warning("Дубль unit_index=%d — повторная секция отброшена.", i)
            continue
        text, fences_ok = prepare_section(item.markdown, heading=units[i].subpoint.heading)
        if not text:
            continue
        needs_check = item.needs_check
        if not fences_ok:
            if not final:
                continue  # попадёт в missing -> повторим
            text, needs_check = close_unbalanced_fence(text), True
        accepted[i] = SectionDraft(
            note_id=units[i].note.note_id, subpoint_id=units[i].subpoint.subpoint_id,
            markdown=text, needs_check=needs_check,
        )
    missing = [i for i in range(len(units)) if i not in accepted]
    return accepted, missing


def elaborate_outline_sync(
    plan: Plan, client: LLMClient, status: TaskStatus, max_subpoints_per_batch: int = 3
) -> list[SectionDraft]:
    """Без чекпоинтинга — для тестов/прямых вызовов вне Orchestrator."""
    collected: list[SectionDraft] = []

    def _collect(_ids: list[str], sections: list[SectionDraft]) -> None:
        collected.extend(sections)

    elaborate_outline(
        plan, client, status, already_done_subpoint_ids=set(),
        on_batch_done=_collect, max_subpoints_per_batch=max_subpoints_per_batch,
    )
    return collected