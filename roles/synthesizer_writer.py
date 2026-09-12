from __future__ import annotations

import logging
from datetime import datetime, timezone

from llm.base import LLMClient
from gemini.prompts.synthesizer_writer import (
    PLAN_SYSTEM_INSTRUCTION,
    WRITE_SYSTEM_INSTRUCTION,
    _DEPTH_WORD_RANGES,
)
from gemini.schemas import DraftNoteOutput
from storage.models import DraftNote, Evidence, NoteAction, OutlineNote, Relationship, TaskStatus
from tools.markdown_tools import (
    build_note_path,
    normalize_link_title,
    sanitize_wikilinks,
    strip_wikilink_brackets, slugify_filename,
)

logger = logging.getLogger(__name__)

def prepare_linking_context(plan_notes: list[OutlineNote]) -> tuple[list[str], dict[str, str]]:
    """Строит (1) полный список известных заголовков заметок — существующих
    в Vault + всех запланированных в этой задаче (даже ещё не написанных —
    их заголовки уже зафиксированы планом), и (2) карту normalized->canonical
    для снаппинга ссылок модели к точному написанию (дефисы и т.п.)."""
    title_map = {normalize_link_title(n.title): n.title for n in plan_notes}
    return sorted(set(title_map.values())), title_map

def write_note(
    note: OutlineNote,
    evidence: list[Evidence],
    known_titles: list[str],
    title_map: dict[str, str],
    client: LLMClient,
    status: TaskStatus,
    extra_instructions: str = "",
    mark_source: str | None = None,
) -> DraftNote:
    assigned = [e for e in evidence if e.note_id == note.note_id]
    by_subpoint = {sp.subpoint_id: sp for sp in note.subpoints}

    sections_listing = "\n\n".join(
        f"### Раздел: {by_subpoint[sp_id].heading}\n"
        f"Техзадание: {by_subpoint[sp_id].covers}\n"
        f"Факты:\n" + "\n".join(
            f"- {e.statement} (confidence={e.confidence:.2f}"
            f"{', ПРОТИВОРЕЧИВО: ' + e.critic_note if e.critic_note else ''})"
            for e in assigned if e.subpoint_id == sp_id
        )
        for sp_id in by_subpoint
    ) or "(фактов не назначено — опирайся на заголовки разделов)"

    titles_listing = "\n".join(f"- {t}" for t in known_titles) or "(других заметок пока нет)"
    headings_listing = "\n".join(f"- {sp.heading}" for sp in note.subpoints)

    prompt = (
        f"Заголовок заметки (зафиксирован планом, не менять): {note.title}\n"
        f"Action: {note.action}\n"
        + (f"Существующий путь (для update): {note.existing_path}\n" if note.action == "update" else "")
        + f"\nРазделы для заполнения (заголовки ## в этом порядке):\n{headings_listing}\n\n"
        f"Материал по разделам:\n{sections_listing}\n\n"
        f"Все известные заголовки заметок (для [[wikilink]]):\n{titles_listing}\n\n"
        + (f"ЗАМЕЧАНИЯ ПО ПРЕДЫДУЩЕЙ ВЕРСИИ:\n{extra_instructions}\n\n" if extra_instructions else "")
        + "Напиши содержимое этой заметки согласно роли."
    )

    output: DraftNoteOutput = client.generate_structured(
        role="synthesizer_write", prompt=prompt, response_model=DraftNoteOutput,
        status=status, system_instruction=WRITE_SYSTEM_INSTRUCTION,
    )
    return _to_draft_note(note, output, title_map, mark_source=mark_source)



def _to_draft_note(
    note: OutlineNote, output: DraftNoteOutput, title_map: dict[str, str], mark_source: str | None = None,
) -> DraftNote:
    # Путь/action/folder/title решает ПЛАН (шаг 1), а не то, что модель
    # вернула на шаге 2 — план уже согласован с существующими путями Vault
    # и с заголовками других заметок; давать модели право переопределить их
    # здесь означало бы риск рассинхрона (см. комментарий в gemini/schemas.py
    # про то, что foreign keys/пути не должны придумываться LLM).
    if note.action == "update":
        if not note.existing_path:
            raise ValueError(f"Для action='update' не указан existing_path: {note.title!r}")
        path, action = note.existing_path, NoteAction.UPDATE
    else:
        path, action = build_note_path(note.folder, note.title), NoteAction.CREATE

    frontmatter = {"created": datetime.now(timezone.utc).date().isoformat()}
    if mark_source:
        frontmatter["source"] = mark_source
    # Остальные ключи frontmatter_extra от модели намеренно игнорируются —
    # состав frontmatter ограничен title/tags/created(/source) на уровне
    # кода (см. tools/markdown_tools.py::render_frontmatter), это
    # единственная точка правды, а не промпт.

    def _resolve_link(raw: str) -> str | None:
        link = strip_wikilink_brackets(raw).strip()
        if not link:
            return None
        if "://" in link or link.startswith("www."):
            # Модель по ошибке положила ссылку на источник в links_out —
            # туда должны попадать только заголовки заметок Vault.
            logger.warning(
                "Игнорируем URL-подобное значение в links_out (это не "
                "заголовок заметки): %r", link,
            )
            return None
        return title_map.get(normalize_link_title(link), link)

    links_out_fixed = [
        resolved for raw in output.links_out
        if (resolved := _resolve_link(raw)) is not None
    ]

    return DraftNote(
        note_id=note.note_id,
        action=action,
        path=path,
        title=note.title,
        folder=note.folder,
        frontmatter=frontmatter,
        body_md=sanitize_wikilinks(output.body_md),
        tags=output.tags,
        links_out=links_out_fixed,
        append_section=sanitize_wikilinks(output.append_section) or None,
    )


def build_relationships(drafts: list[DraftNote]) -> list[Relationship]:
    title_to_path = {d.title: d.path for d in drafts}
    return [
        Relationship(from_note=d.path, to_note=title_to_path.get(t, t), link_type="wikilink")
        for d in drafts for t in d.links_out
    ]