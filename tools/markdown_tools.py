"""
Skill "Markdown generation" — принадлежит роли Writer, но реализован как
чистая, детерминированная функция без LLM (генерация текста YAML/Markdown
не требует reasoning, только форматирование уже готовых структурированных
данных из DraftNote).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

import yaml

from storage.models import DraftNote

_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')
_NESTED_WIKILINK_RE = re.compile(r'\[{2,}([^\[\]]+)\]{2,}')

_DASH_VARIANTS = {
    "\u2010": "-", "\u2011": "-", "\u2012": "-",
    "\u2013": "-", "\u2014": "-", "\u2015": "-",
}
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


# Единственный источник истины по составу frontmatter — сознательно
# ограничен тремя ключами по требованию продукта (не плодить произвольные
# YAML-свойства, которые может насочинять LLM через frontmatter_extra).
# Контроль на уровне кода, а не промпта: даже если Writer-роль или её
# промпт в будущем изменятся и снова начнут предлагать другие ключи,
# лишнее сюда не попадёт.
_ALLOWED_FRONTMATTER_KEYS = ("title", "tags", "created")


def render_frontmatter(frontmatter: dict) -> str:
    # sort_keys=False — сохраняем порядок ключей: title/tags/created.
    ordered = {
        k: frontmatter[k]
        for k in _ALLOWED_FRONTMATTER_KEYS
        if k in frontmatter and frontmatter[k]
    }
    ordered.setdefault("created", datetime.now(timezone.utc).date().isoformat())
    yaml_text = yaml.safe_dump(
        ordered, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return f"---\n{yaml_text}---\n"


def render_sources_block(source_refs: list[str]) -> str:
    """Источники — простой список ссылок в НАЧАЛЕ тела заметки (сразу
    после frontmatter), а не свойство YAML: см. render_frontmatter, которая
    сознательно не пропускает 'sources' в frontmatter."""
    if not source_refs:
        return ""
    lines = "\n".join(f"- {url}" for url in source_refs)
    return f"## Источники\n\n{lines}\n\n"


def render_markdown(draft: DraftNote) -> str:
    fm = dict(draft.frontmatter)
    fm["title"] = draft.title
    if draft.tags:
        fm["tags"] = draft.tags

    body = sanitize_wikilinks(draft.body_md.strip())
    parts = [
        render_frontmatter(fm),
        "\n",
        render_sources_block(draft.source_refs),
        body,
        "\n",
    ]

    if draft.links_out:
        related = "\n".join(f"- [[{strip_wikilink_brackets(t)}]]" for t in draft.links_out)
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

def sanitize_wikilinks(text: str) -> str:
    """Убирает случайное дублирование скобок ([[[[X]]]] -> [[X]]),
    которое иногда генерирует LLM при вложенной подстановке шаблона ссылки."""
    if not text:
        return text
    previous = None
    result = text
    # применяем повторно на случай тройной/четверной вложенности
    while previous != result:
        previous = result
        result = _NESTED_WIKILINK_RE.sub(r'[[\1]]', result)
    return result


def strip_wikilink_brackets(text: str) -> str:
    """Полностью убирает обрамляющие [[ ]] у заголовка ссылки.

    Отличие от sanitize_wikilinks: та схлопывает [[[[X]]]] к ОДНОЙ паре
    скобок [[X]] (полезно для тела заметки, где [[wikilink]] и так должен
    остаться ссылкой). Но в местах, где скобки добавляются программно
    (render_markdown при рендере links_out, синхронизация ссылок в
    roles/synthesizer_writer.py), применение sanitize_wikilinks к уже
    добавленным скобкам с последующим повторным оборачиванием в [[...]]
    как раз и давало баг [[[[Title]]]] — LLM иногда кладёт в links_out
    строку вида '[[Title]]' вместо чистого 'Title', sanitize_wikilinks
    оставляет одну пару скобок как валидную, а внешний код оборачивает
    её снова. Эта функция убирает скобки полностью, поэтому обёртка
    происходит ровно один раз."""
    if not text:
        return text
    t = text.strip()
    while len(t) >= 2 and t.startswith("[") and t.endswith("]"):
        t = t[1:-1].strip()
    return t


def normalize_link_title(title: str) -> str:
    """Нормализует заголовок для СРАВНЕНИЯ ссылок с реальными файлами:
    NFC-нормализация + унификация вариантов дефиса/тире. НЕ используется
    для отображения — только чтобы понять, ссылается ли LLM на уже
    существующую заметку под слегка другим написанием дефиса."""
    normalized = unicodedata.normalize("NFC", title).strip()
    for variant, replacement in _DASH_VARIANTS.items():
        normalized = normalized.replace(variant, replacement)
    return normalized