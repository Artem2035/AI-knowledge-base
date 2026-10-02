"""
Детерминированная сборка заметки из готовых секций Elaborator v2 — без LLM.

Заголовки внутри секции приводятся к диапазону ## … ####: # поднимается
до ##, всё глубже #### опускается до ####. Строки внутри fenced code
block (```) не трогаются, иначе '# comment' в примерах кода превратился бы
в заголовок.
"""
from __future__ import annotations

import re

from storage.models import OutlineNote, SectionDraft

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