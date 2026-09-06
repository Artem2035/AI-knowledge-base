from __future__ import annotations

import logging
from datetime import datetime, timezone

from llm.base import LLMClient
from gemini.schemas import DraftNoteOutput, NotePlanItem, NotePlanOutput
from storage.models import (
    DraftNote,
    Evidence,
    ExistingNote,
    NoteAction,
    Plan,
    Relationship,
    SourceCandidate,
    TaskStatus,
)
from tools.markdown_tools import build_note_path, normalize_link_title, sanitize_wikilinks

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Шаг 1 — Note Planner: лёгкий вызов, определяет ТОЛЬКО структуру заметок
# (create/update, заголовки, распределение evidence), без текста самих
# заметок. Промпт компактный (evidence без critic_note) — не должен упираться
# в TPM-бюджет даже на богатых темах.
# ---------------------------------------------------------------------------

PLAN_SYSTEM_INSTRUCTION = (
    "Ты — Synthesizer (этап планирования) в системе управления знаниями "
    "Obsidian. Тебе даны тема исследования, пронумерованный список "
    "извлечённых фактов и список существующих заметок Vault, похожих на "
    "новые концепции (с decision: reuse/extend/distinct). Раздели материал "
    "на атомарные заметки: одна заметка = одна самостоятельная концепция, "
    "без избыточного дробления (смежные мелкие подтемы объединяй в одну "
    "заметку со структурой из подзаголовков). Для каждой заметки укажи: "
    "заголовок; action='update' И existing_path (ТОЧНО совпадающий с path "
    "из списка существующих заметок), если для концепции есть существующая "
    "заметка с decision='reuse' или 'extend' — иначе action='create'; "
    "evidence_indices — номера фактов из списка, относящихся к этой "
    "заметке (каждый релевантный факт должен войти хотя бы в одну заметку, "
    "один факт может относиться к нескольким заметкам). Здесь НЕ нужно "
    "писать текст заметки — только план."
)

# ---------------------------------------------------------------------------
# Шаг 2 — Writer: по одному вызову на КАЖДУЮ заметку из плана. Получает
# только evidence, назначенный этой заметке (а не весь список), поэтому
# промпт на порядок меньше, чем был в одном большом вызове.
# ---------------------------------------------------------------------------

WRITE_SYSTEM_INSTRUCTION = (
    "Ты — Obsidian Writer в системе управления знаниями. Тебе дано задание "
    "написать ОДНУ конкретную заметку по уже составленному плану (заголовок "
    "и action заданы и не обсуждаются). Для action='create' сформируй "
    "frontmatter_extra, tags, body_md (минимум 3 содержательных абзаца, "
    "примеры кода с указанием языка где уместно) и links_out. Для "
    "action='update' верни append_section — новый материал для добавления "
    "к существующей заметке (НЕ переписывай и не повторяй существующее "
    "содержимое; body_md можно не возвращать). Пиши на русском языке "
    "(кроме общепринятых технических терминов вроде SQL, Python, RAG — их "
    "не переводить). Не копируй текст источника дословно — только "
    "пересказ своими словами, с указанием, из какого источника факт. "
    "Правила по ссылкам: тебе дан список ВСЕХ заголовков заметок (уже "
    "существующих в Vault и остальных, запланированных в этой же задаче) — "
    "используй [[wikilink]] ТОЛЬКО на заголовки из этого списка, написанные "
    "ТОЧНО так, как они даны (включая дефисы). Ссылку на тему, которой нет "
    "в списке, добавляй только если это действительно самостоятельная "
    "концепция, которая может стать отдельной группой заметок в будущем — "
    "не на каждый незнакомый термин."
)


def plan_notes(
    plan: Plan,
    evidence: list[Evidence],
    existing_notes: list[ExistingNote],
    client: LLMClient,
    status: TaskStatus,
) -> NotePlanOutput:
    evidence_listing = "\n".join(
        f"[{i}] [{e.concept}] {e.statement}" for i, e in enumerate(evidence)
    ) or "(фактов нет)"
    existing_listing = "\n".join(
        f"- path={n.path!r} title={n.title!r} decision={n.decision} "
        f"(похоже на концепцию {n.matched_concept!r}, similarity={n.similarity_score:.2f})"
        for n in existing_notes
    ) or "(похожих существующих заметок не найдено)"

    prompt = (
        f"Тема: {plan.topic_title}\n"
        f"Описание: {plan.summary}\n"
        f"Подтемы: {', '.join(s.title for s in plan.subtopics)}\n\n"
        f"Факты (номер в квадратных скобках):\n{evidence_listing}\n\n"
        f"Существующие заметки в Vault, похожие на новые концепции:\n{existing_listing}\n\n"
        "Составь план заметок (create/update, заголовки, распределение фактов по индексам)."
    )

    output: NotePlanOutput = client.generate_structured(
        role="synthesizer_plan",
        prompt=prompt,
        response_model=NotePlanOutput,
        status=status,
        system_instruction=PLAN_SYSTEM_INSTRUCTION,
    )

    _warn_on_unassigned_evidence(output, evidence)
    return output


def _warn_on_unassigned_evidence(plan_output: NotePlanOutput, evidence: list[Evidence]) -> None:
    assigned: set[int] = set()
    for item in plan_output.notes:
        assigned.update(i for i in item.evidence_indices if 0 <= i < len(evidence))
    unassigned = [i for i in range(len(evidence)) if i not in assigned]
    if unassigned:
        logger.warning(
            "%d из %d извлечённых фактов не были распределены Planner-ом ни "
            "в одну заметку (индексы: %s) — эта информация не попадёт в "
            "итоговые заметки.",
            len(unassigned), len(evidence), unassigned,
        )


def prepare_linking_context(
    note_plan: NotePlanOutput, existing_notes: list[ExistingNote]
) -> tuple[list[str], dict[str, str]]:
    """Строит (1) полный список известных заголовков заметок — существующих
    в Vault + всех запланированных в этой задаче (даже ещё не написанных —
    их заголовки уже зафиксированы планом), и (2) карту normalized->canonical
    для снаппинга ссылок модели к точному написанию (дефисы и т.п.)."""
    title_map: dict[str, str] = {}
    for n in existing_notes:
        title_map[normalize_link_title(n.title)] = n.title
    for item in note_plan.notes:
        title_map[normalize_link_title(item.title)] = item.title
    return sorted(set(title_map.values())), title_map


def write_note(
    item: NotePlanItem,
    evidence: list[Evidence],
    known_titles: list[str],
    title_map: dict[str, str],
    sources: list[SourceCandidate],
    client: LLMClient,
    status: TaskStatus,
    default_folder: str,
) -> DraftNote:
    assigned = [evidence[i] for i in item.evidence_indices if 0 <= i < len(evidence)]
    evidence_listing = "\n".join(
        f"- [{e.concept}] {e.statement} (confidence={e.confidence:.2f}"
        f"{', ПРОТИВОРЕЧИВО: ' + e.critic_note if e.critic_note else ''})"
        for e in assigned
    ) or "(фактов для этой заметки не назначено — опирайся на заголовок и общий контекст темы)"
    titles_listing = "\n".join(f"- {t}" for t in known_titles) or "(других заметок пока нет)"
    sources_listing = "\n".join(f"- {s.title}: {s.url}" for s in sources) or "(источники не переданы)"

    prompt = (
        f"Заголовок заметки (зафиксирован планом, не менять): {item.title}\n"
        f"Action: {item.action}\n"
        + (f"Существующий путь (для update): {item.existing_path}\n" if item.action == "update" else "")
        + f"\nФакты, относящиеся к этой заметке:\n{evidence_listing}\n\n"
        f"Все известные заголовки заметок (для [[wikilink]]):\n{titles_listing}\n\n"
        f"Источники:\n{sources_listing}\n\n"
        "Напиши содержимое этой заметки согласно роли."
    )

    output: DraftNoteOutput = client.generate_structured(
        role="synthesizer_write",
        prompt=prompt,
        response_model=DraftNoteOutput,
        status=status,
        system_instruction=WRITE_SYSTEM_INSTRUCTION,
    )

    return _to_draft_note(item, output, sources, title_map, default_folder)


def _to_draft_note(
    item: NotePlanItem,
    output: DraftNoteOutput,
    sources: list[SourceCandidate],
    title_map: dict[str, str],
    default_folder: str,
) -> DraftNote:
    # Путь/action/folder/title решает ПЛАН (шаг 1), а не то, что модель
    # вернула на шаге 2 — план уже согласован с существующими путями Vault
    # и с заголовками других заметок; давать модели право переопределить их
    # здесь означало бы риск рассинхрона (см. комментарий в gemini/schemas.py
    # про то, что foreign keys/пути не должны придумываться LLM).
    if item.action == "update":
        if not item.existing_path:
            raise ValueError(f"Для action='update' не указан existing_path: {item.title!r}")
        path = item.existing_path
        action = NoteAction.UPDATE
    else:
        path = build_note_path(item.folder or default_folder, item.title)
        action = NoteAction.CREATE

    now_iso = datetime.now(timezone.utc).date().isoformat()
    frontmatter = {"created": now_iso}
    for f in output.frontmatter_extra:
        frontmatter[f.key] = f.value

    def _resolve_link(raw: str) -> str:
        link = sanitize_wikilinks(raw).strip()
        return title_map.get(normalize_link_title(link), link)

    links_out_fixed = [_resolve_link(t) for t in output.links_out]

    return DraftNote(
        action=action,
        path=path,
        title=item.title,
        folder=item.folder or default_folder,
        frontmatter=frontmatter,
        body_md=sanitize_wikilinks(output.body_md),
        tags=output.tags or item.tags_hint,
        links_out=links_out_fixed,
        source_refs=[s.url for s in sources],
        append_section=sanitize_wikilinks(output.append_section) or None,
    )


def build_relationships(drafts: list[DraftNote]) -> list[Relationship]:
    relationships: list[Relationship] = []
    title_to_path = {d.title: d.path for d in drafts}
    for d in drafts:
        for linked_title in d.links_out:
            target_path = title_to_path.get(linked_title, linked_title)
            relationships.append(
                Relationship(from_note=d.path, to_note=target_path, link_type="wikilink")
            )
    return relationships


def synthesize_and_write(
    plan: Plan,
    evidence: list[Evidence],
    sources: list[SourceCandidate],
    existing_notes: list[ExistingNote],
    client: LLMClient,
    status: TaskStatus,
    default_folder: str,
) -> tuple[list[DraftNote], list[Relationship]]:
    """Удобная обёртка без чекпоинтинга — оба шага map-reduce одним вызовом
    функции. Используется там, где резюмируемость не нужна (тесты, прямой
    вызов вне Orchestrator). Сам Orchestrator использует plan_notes()/
    write_note() по отдельности, чтобы персистить прогресс после каждой
    заметки — см. orchestrator/state_machine.py."""
    note_plan = plan_notes(plan, evidence, existing_notes, client, status)
    known_titles, title_map = prepare_linking_context(note_plan, existing_notes)

    drafts = [
        write_note(item, evidence, known_titles, title_map, sources, client, status, default_folder)
        for item in note_plan.notes
    ]
    relationships = build_relationships(drafts)
    return drafts, relationships