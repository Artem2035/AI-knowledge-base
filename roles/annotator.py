"""
Роль Annotator: один лёгкий батчевый вызов на несколько заметок, возвращает
ТОЛЬКО теги, links_out и abstract. Тело заметки собирает код
(tools/note_assembly.py), модель его не пишет.

Вход модели — детерминированный дайджест (заголовки + начало каждой
секции, без кода/таблиц), а не полный текст: так вызов остаётся дешёвым по
TPM. Для action="update" аннотатор не вызывается: vault/writer.py
дописывает только append_section, теги и резюме там не используются.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from llm.base import LLMClient
from llm.chunking import batch_for_quality_and_budget
from llm.common import LLMPromptTooLargeError, LLMSchemaError
from llm.prompts.annotator import SYSTEM_INSTRUCTION
from llm.schemas import AnnotationBatchOutput
from storage.models import NoteAnnotation, OutlineNote, Plan, SectionDraft, TaskStatus
from tools.markdown_tools import normalize_link_title, snap_link
from tools.note_assembly import ABSTRACT_MIN_SECTIONS, plain_text

logger = logging.getLogger(__name__)

# Качественный потолок заметок на один вызов: вывод растёт линейно
# (~200 токенов на заметку), а резерв вывода у роли ограничен.
_MAX_NOTES_PER_BATCH = 6
# Сколько раз делим батч пополам при LLMSchemaError (как в elaborator).
_MAX_SPLIT_DEPTH = 2
_MAX_TAGS = 6
# Длина начала секции в дайджесте и общий потолок дайджеста заметки.
_SECTION_DIGEST_CHARS = 160
_MIN_SECTION_DIGEST_CHARS = 40
_NOTE_DIGEST_MAX_CHARS = 1500

_TAG_BAD_CHARS_RE = re.compile(r"[^\w\-/]", re.UNICODE)


@dataclass
class AnnotationUnit:
    note: OutlineNote
    digest: str
    wants_abstract: bool


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def build_digest(note: OutlineNote, sections: list[SectionDraft]) -> str:
    """Детерминированный дайджест заметки для аннотатора (без LLM)."""
    by_subpoint = {s.subpoint_id: s for s in sections if s.note_id == note.note_id}
    per_section = max(
        _MIN_SECTION_DIGEST_CHARS,
        min(_SECTION_DIGEST_CHARS, _NOTE_DIGEST_MAX_CHARS // max(len(note.subpoints), 1)),
    )
    lines = [f"Заметка: {note.title}"]
    for sp in note.subpoints:
        sec = by_subpoint.get(sp.subpoint_id)
        start = _truncate(plain_text(sec.markdown), per_section) if sec else ""
        lines.append(f"- {sp.heading}: {start}" if start else f"- {sp.heading}")
    return "\n".join(lines)


def build_annotation_units(
    plan: Plan, sections: list[SectionDraft], already_done_note_ids: set[str],
) -> list[AnnotationUnit]:
    return [
        AnnotationUnit(
            note=note,
            digest=build_digest(note, sections),
            wants_abstract=len(note.subpoints) >= ABSTRACT_MIN_SECTIONS,
        )
        for note in plan.notes
        if note.action == "create" and note.note_id not in already_done_note_ids
    ]


def _render(u: AnnotationUnit, index: int | None = None) -> str:
    head = f"=== Заметка [{index}] ===\n" if index is not None else ""
    flag = "required" if u.wants_abstract else "not needed"
    return f"{head}{u.digest}\nabstract: {flag}"


def _frame(plan: Plan, known_titles: list[str]) -> str:
    titles = "\n".join(f"- {t}" for t in known_titles) or "(нет)"
    return (
        f"Тема исследования: {plan.topic_title}\n"
        f"Известные заголовки заметок (для links_out):\n{titles}\n\n"
        "Для КАЖДОЙ заметки ниже верни index, tags, links_out и abstract."
    )


def normalize_tags(raw_tags: list[str]) -> list[str]:
    """Теги в формате Obsidian: нижний регистр, без '#', пробелы -> '-',
    без дублей и чисто числовых тегов, не более _MAX_TAGS."""
    result: list[str] = []
    for raw in raw_tags:
        tag = raw.strip().lstrip("#").strip().lower()
        tag = re.sub(r"\s+", "-", tag)
        tag = _TAG_BAD_CHARS_RE.sub("", tag).strip("-/")
        if not tag or tag.isdigit() or tag in result:
            continue
        result.append(tag)
        if len(result) >= _MAX_TAGS:
            break
    return result


def _clean_links(raw_links: list[str], own_title: str, title_map: dict[str, str]) -> list[str]:
    """Оставляет только заголовки из title_map (никаких «красных» ссылок
    от модели), без ссылки заметки на саму себя и без дублей."""
    known = set(title_map.values())
    own = normalize_link_title(own_title)
    result: list[str] = []
    for raw in raw_links:
        link = snap_link(raw, title_map)
        if link is None or link not in known:
            continue
        if normalize_link_title(link) == own or link in result:
            continue
        result.append(link)
    return result


def _empty(u: AnnotationUnit) -> NoteAnnotation:
    return NoteAnnotation(note_id=u.note.note_id)


def _to_annotations(
    output: AnnotationBatchOutput, units: list[AnnotationUnit], title_map: dict[str, str],
) -> list[NoteAnnotation]:
    by_index = {}
    for item in output.items:
        if not (0 <= item.index < len(units)):
            logger.warning(
                "Аннотатор вернул index=%d вне диапазона [0, %d) — элемент отброшен.",
                item.index, len(units),
            )
            continue
        if item.index in by_index:
            logger.warning("Дубль index=%d в ответе аннотатора — повтор отброшен.", item.index)
            continue
        by_index[item.index] = item

    result: list[NoteAnnotation] = []
    for i, u in enumerate(units):
        item = by_index.get(i)
        if item is None:
            logger.warning("Для заметки «%s» аннотация не получена — пустая.", u.note.title)
            result.append(_empty(u))
            continue
        result.append(NoteAnnotation(
            note_id=u.note.note_id,
            tags=normalize_tags(item.tags),
            links_out=_clean_links(item.links_out, u.note.title, title_map),
            abstract=item.abstract.strip() if u.wants_abstract else "",
        ))
    return result


def _split_and_retry(units, plan, known_titles, title_map, client, status, depth) -> list[NoteAnnotation]:
    mid = len(units) // 2
    return (
        _annotate_batch(units[:mid], plan, known_titles, title_map, client, status, depth)
        + _annotate_batch(units[mid:], plan, known_titles, title_map, client, status, depth)
    )


def _annotate_batch(
    units: list[AnnotationUnit], plan: Plan, known_titles: list[str],
    title_map: dict[str, str], client: LLMClient, status: TaskStatus, depth: int = 0,
) -> list[NoteAnnotation]:
    """Возвращает аннотацию для КАЖДОЙ заметки батча (при неудаче — пустую:
    теги/ссылки/резюме не критичны, сбой аннотатора не ошибка валидации)."""
    if not units:
        return []

    listing = "\n\n".join(_render(u, i) for i, u in enumerate(units))
    prompt = f"{_frame(plan, known_titles)}\n\nЗаметки:\n\n{listing}"
    try:
        output: AnnotationBatchOutput = client.generate_structured(
            role="annotator",
            prompt=prompt,
            response_model=AnnotationBatchOutput,
            status=status,
            system_instruction=SYSTEM_INSTRUCTION,
        )
    except LLMPromptTooLargeError:
        if len(units) <= 1:
            logger.warning("Дайджест заметки «%s» не влез в бюджет — пустая аннотация.", units[0].note.title)
            return [_empty(units[0])]
        return _split_and_retry(units, plan, known_titles, title_map, client, status, depth)
    except LLMSchemaError as exc:
        if depth >= _MAX_SPLIT_DEPTH or len(units) <= 1:
            logger.warning(
                "Батч из %d заметок пропущен: невалидный JSON аннотатора (%s).", len(units), exc,
            )
            return [_empty(u) for u in units]
        logger.info("LLMSchemaError аннотатора на %d заметках — делим пополам (depth=%d).", len(units), depth)
        return _split_and_retry(units, plan, known_titles, title_map, client, status, depth + 1)

    return _to_annotations(output, units, title_map)


def annotate_notes(
    plan: Plan,
    sections: list[SectionDraft],
    title_map: dict[str, str],
    client: LLMClient,
    status: TaskStatus,
    already_done_note_ids: set[str],
    on_batch_done,
) -> None:
    """on_batch_done(note_ids: list[str], annotations: list[NoteAnnotation]) —
    вызывается после каждого батча; Orchestrator обязан немедленно
    персистить note_ids в чекпоинт. Только action="create"; заметки
    action="update" в note_ids не попадают."""
    units = build_annotation_units(plan, sections, already_done_note_ids)
    if not units:
        return

    known_titles = sorted(set(title_map.values()))
    batches = batch_for_quality_and_budget(
        units,
        client=client,
        system_instruction=SYSTEM_INSTRUCTION,
        response_model=AnnotationBatchOutput,
        render_item=_render,
        static_overhead_text=_frame(plan, known_titles),
        max_items_per_batch=_MAX_NOTES_PER_BATCH,
    )
    for batch in batches:
        annotations = _annotate_batch(batch, plan, known_titles, title_map, client, status)
        on_batch_done([u.note.note_id for u in batch], annotations)


def annotate_notes_sync(
    plan: Plan, sections: list[SectionDraft], title_map: dict[str, str],
    client: LLMClient, status: TaskStatus,
) -> list[NoteAnnotation]:
    """Без чекпоинтинга: для тестов и прямых вызовов вне Orchestrator."""
    collected: list[NoteAnnotation] = []

    def _collect(_ids: list[str], annotations: list[NoteAnnotation]) -> None:
        collected.extend(annotations)

    annotate_notes(
        plan, sections, title_map, client, status,
        already_done_note_ids=set(), on_batch_done=_collect,
    )
    return collected