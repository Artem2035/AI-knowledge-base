"""
Skill "Markdown generation" — принадлежит роли Writer, но реализован как
чистая, детерминированная функция без LLM (генерация текста YAML/Markdown
не требует reasoning, только форматирование уже готовых структурированных
данных из DraftNote).
"""
from __future__ import annotations

import re
import unicodedata

import yaml

from storage.models import DraftNote

_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')


def slugify_filename(title: str) -> str:
    """
    Делает безопасное имя файла для Obsidian, сохраняя кириллицу
    (в отличие от типичных web-slugify, здесь НЕ транслитерируем русский —
    по ТЗ имена заметок по умолчанию на русском, если запрос на русском).
    """
    normalized = unicodedata.normalize("NFC", title).strip()
    normalized = _INVALID_FS_CHARS.sub("", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or "Без названия"


def build_note_path(folder: str, title: str) -> str:
    filename = slugify_filename(title)
    folder = folder.strip("/")
    if folder:
        return f"{folder}/{filename}.md"
    return f"{filename}.md"


def render_frontmatter(frontmatter: dict) -> str:
    # sort_keys=False — сохраняем порядок, заданный Writer-ролью (обычно
    # title/tags/created/aliases/source в начале — так удобнее читать в Obsidian)
    yaml_text = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return f"---\n{yaml_text}---\n"


def render_markdown(draft: DraftNote) -> str:
    fm = dict(draft.frontmatter)
    fm.setdefault("title", draft.title)
    if draft.tags:
        fm.setdefault("tags", draft.tags)
    if draft.source_refs:
        fm.setdefault("sources", draft.source_refs)

    parts = [render_frontmatter(fm), "\n", draft.body_md.strip(), "\n"]

    if draft.links_out:
        related = "\n".join(f"- [[{t}]]" for t in draft.links_out)
        parts.append(f"\n## Связанные заметки\n\n{related}\n")

    return "".join(parts)


def insert_wikilinks(body_md: str, titles_to_link: list[str]) -> str:
    """
    Простая детерминированная простановка [[wikilink]] для первого вхождения
    каждого заголовка из titles_to_link в тексте (без затрагивания уже
    существующих ссылок/кода).
    """
    result = body_md
    for title in sorted(titles_to_link, key=len, reverse=True):
        if not title or f"[[{title}]]" in result:
            continue
        pattern = re.compile(rf"(?<!\[)\b{re.escape(title)}\b(?!\])")
        result, n = pattern.subn(f"[[{title}]]", result, count=1)
    return result
