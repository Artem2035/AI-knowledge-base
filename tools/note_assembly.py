"""
Детерминированная сборка заметки из готовых секций Elaborator v2 — без LLM.

Заголовки внутри секции приводятся к диапазону ## … ####: # поднимается
до ##, всё глубже #### опускается до ####. Строки внутри fenced code
block (```) не трогаются, иначе '# comment' в примерах кода превратился бы
в заголовок.
"""
from __future__ import annotations

import re

from datetime import datetime, timezone

from storage.models import DraftNote, NoteAction, NoteAnnotation, OutlineNote, SectionDraft
from tools.markdown_tools import build_note_path
# Порог вставки резюме-callout: короткой заметке резюме не нужно.
ABSTRACT_MIN_SECTIONS = 8

MIN_HEADING_LEVEL = 2
MAX_HEADING_LEVEL = 4

PLACEHOLDER_MARKDOWN = "_Раздел не удалось сгенерировать автоматически — заполните его вручную._"

_FENCE_LINE_RE = re.compile(r"^\s{0,3}```")
_HEADING_LINE_RE = re.compile(r"^(#{1,6})(\s+.*)$")

_HUMANITIES_CALLOUT = (
    "> [!warning] Проверьте факты\n"
    "> Даты, цитаты и имена в этой заметке сгенерированы без источников — "
    "сверьте их перед использованием."
)


def has_balanced_fences(text: str) -> bool:
    """Чётное число строк-ограждений ``` (открытие + закрытие)."""
    return sum(1 for ln in text.splitlines() if _FENCE_LINE_RE.match(ln)) % 2 == 0


def close_unbalanced_fence(text: str) -> str:
    """Закрывает висящий блок кода (если ограждений нечётное число)."""
    if has_balanced_fences(text):
        return text
    return text.rstrip() + "\n```"


def normalize_headings(text: str) -> str:
    """Приводит уровни заголовков вне code-fence к диапазону ## … ####."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence:
            m = _HEADING_LINE_RE.match(line)
            if m:
                level = min(max(len(m.group(1)), MIN_HEADING_LEVEL), MAX_HEADING_LEVEL)
                line = "#" * level + m.group(2)
        out.append(line)
    return "\n".join(out)


def strip_leading_duplicate_heading(text: str, heading: str) -> str:
    """Если модель начала секцию с собственного заголовка, совпадающего с
    заголовком из плана, — убираем его (код сам добавляет '## heading')."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = _HEADING_LINE_RE.match(line)
        if m and m.group(2).strip().rstrip("#").strip().casefold() == heading.strip().casefold():
            return "\n".join(lines[i + 1:]).lstrip("\n")
        break
    return text


def prepare_section(markdown: str, heading: str = "") -> tuple[str, bool]:
    """Нормализует секцию. Возвращает (текст, fence_сбалансирован)."""
    text = markdown.strip()
    if heading:
        text = strip_leading_duplicate_heading(text, heading)
    text = normalize_headings(text).strip()
    return text, has_balanced_fences(text)


def _callout(kind: str, title: str, text: str) -> str:
    body = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in text.splitlines())
    return f"> [!{kind}] {title}\n{body}"


def assemble_note_markdown(
    note: OutlineNote,
    sections: list[SectionDraft],
    *,
    domain: str,
    abstract: str = "",
    for_update: bool = False,
) -> tuple[str, list[str]]:
    """Собирает тело заметки: '## heading' из плана + текст секции.

    Возвращает (markdown, заголовки разделов с needs_check). Для
    for_update=True результат предназначен для append_section: резюме
    не вставляется. Подпункт без секции получает placeholder с needs_check.
    """
    by_subpoint = {s.subpoint_id: s for s in sections if s.note_id == note.note_id}
    blocks: list[str] = []
    unverified: list[str] = []

    if domain == "humanities":
        blocks.append(_HUMANITIES_CALLOUT)
    if not for_update and abstract.strip() and len(note.subpoints) >= ABSTRACT_MIN_SECTIONS:
        blocks.append(_callout("abstract", "Кратко", abstract.strip()))

    for sp in note.subpoints:
        sec = by_subpoint.get(sp.subpoint_id)
        needs_check = sec.needs_check if sec else True
        text = sec.markdown.strip() if sec and sec.markdown.strip() else PLACEHOLDER_MARKDOWN
        parts = [f"## {sp.heading}"]
        if needs_check:
            parts.append(_callout("warning", "Требует проверки", "Детали этого раздела могут быть неточными."))
            unverified.append(sp.heading)
        parts.append(text)
        blocks.append("\n\n".join(parts))

    return "\n\n".join(blocks), unverified


def add_domain_tag(tags: list[str], domain: str) -> list[str]:
    """Добавляет тег домена вида 'domain/technical' (без дублей)."""
    tag = f"domain/{domain}"
    return tags if tag in tags else [*tags, tag]

def build_draft_note(
    note: OutlineNote,
    sections: list[SectionDraft],
    annotation: NoteAnnotation | None,
    *,
    domain: str,
    mark_source: str | None = None,
) -> DraftNote:
    """Детерминированно собирает DraftNote из готовых секций и аннотации (без LLM).

    Путь, action, папку и заголовок решает ПЛАН (как и раньше: модель их не
    выбирает). create: тело заметки, теги (+ domain/<домен>), links_out и
    abstract берутся из аннотации. update: только append_section из
    собранных секций; аннотатор для update не вызывается, поэтому теги,
    ссылки и резюме игнорируются. Пустое тело update даёт append_section=None —
    это поймает валидатор (empty_body), перезаписи файла не произойдёт.

    Исключения: ValueError — action="update" без existing_path.
    """
    frontmatter = {"created": datetime.now(timezone.utc).date().isoformat()}
    if mark_source:
        frontmatter["source"] = mark_source

    if note.action == "update":
        if not note.existing_path:
            raise ValueError(f"Для action='update' не указан existing_path: {note.title!r}")
        body, unverified = assemble_note_markdown(note, sections, domain=domain, for_update=True)
        return DraftNote(
            note_id=note.note_id,
            action=NoteAction.UPDATE,
            path=note.existing_path,
            title=note.title,
            folder=note.folder,
            frontmatter=frontmatter,
            append_section=body or None,
            unverified_sections=unverified,
        )

    body, unverified = assemble_note_markdown(
        note, sections, domain=domain,
        abstract=annotation.abstract if annotation else "",
    )
    return DraftNote(
        note_id=note.note_id,
        action=NoteAction.CREATE,
        path=build_note_path(note.folder, note.title),
        title=note.title,
        folder=note.folder,
        frontmatter=frontmatter,
        body_md=body,
        tags=add_domain_tag(list(annotation.tags) if annotation else [], domain),
        links_out=list(annotation.links_out) if annotation else [],
        unverified_sections=unverified,
    )