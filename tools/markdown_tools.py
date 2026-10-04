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
import logging

logger = logging.getLogger(__name__)


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
    normalized = re.sub(r"[\\/]+", " ", normalized)
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
# ограничен четырьмя ключами по требованию продукта (не плодить произвольные
# YAML-свойства, которые может насочинять LLM через frontmatter_extra).
# Контроль на уровне кода, а не промпта: даже если Writer-роль или её
# промпт в будущем изменятся и снова начнут предлагать другие ключи,
# лишнее сюда не попадёт.
#
# "source" добавлен для RESEARCH_MODE=knowledge (см. config/settings.py) —
# roles/synthesizer_writer.py::_to_draft_note проставляет
# frontmatter["source"] = "model-knowledge", когда заметка написана без
# внешних источников, чтобы это было видно прямо в самой заметке в
# Obsidian, а не только в diff при approve (см. staging/diff.py).
_ALLOWED_FRONTMATTER_KEYS = ("title", "tags", "created", "source")


def render_frontmatter(frontmatter: dict) -> str:
    # sort_keys=False — сохраняем порядок ключей: title/tags/created/source.
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
    сознательно не пропускает произвольные ключи, кроме
    title/tags/created/source, в frontmatter. В RESEARCH_MODE=knowledge
    source_refs всегда пуст (нет внешних источников) — тогда этот блок
    просто не рендерится, см. вызов ниже."""
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

    if draft.links_out and not draft.is_moc:
        related = "\n".join(f"- [[{strip_wikilink_brackets(t)}]]" for t in draft.links_out)
        parts.append(f"\n## Связанные заметки\n\n{related}\n")

    return "".join(parts)


_FENCE_LINE_RE = re.compile(r"^\s{0,3}```")
# Строки, в которых ссылки не ставим: заголовки, строки таблиц, шапки callout'ов.
_NO_LINK_LINE_RE = re.compile(r"^(?:\s{0,3}#{1,6}(?:\s|$)|\s*\||\s*>\s*\[!)")
# Фрагменты внутри строки, которые не трогаем: inline-код, формулы, [[ссылки]],
# markdown-ссылки, URL. Группа ОДНА (захватывающая): re.split возвращает
# чередование «обычный текст / защищённый фрагмент».
_PROTECTED_SPAN_RE = re.compile(
    r"(`[^`\n]*`|\$\$[^$\n]*\$\$|\$[^$\n]+\$|\[\[[^\]\n]*\]\]"
    r"|\[[^\]\n]*\]\([^)\n]*\)|https?://\S+|www\.\S+)"
)


def _link_first_occurrence(lines: list[str], pattern: re.Pattern, title: str) -> list[str]:
    """Заменяет первое допустимое вхождение pattern на [[title]] (мутирует lines)."""
    in_fence = in_math = False
    for i, line in enumerate(lines):
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.strip() == "$$":
            in_math = not in_math
            continue
        if in_math or _NO_LINK_LINE_RE.match(line):
            continue
        parts = _PROTECTED_SPAN_RE.split(line)
        for j in range(0, len(parts), 2):  # чётные элементы — обычный текст
            new, n = pattern.subn(lambda _m: f"[[{title}]]", parts[j], count=1)
            if n:
                parts[j] = new
                lines[i] = "".join(parts)
                return lines
    return lines


def insert_wikilinks(body_md: str, titles_to_link: list[str]) -> str:
    """Проставляет [[wikilink]] на ПЕРВОЕ вхождение каждого заголовка
    (точное совпадение, с границами слова, без учёта падежей).

    Не трогает: fenced-код (в т.ч. незакрытый — до конца текста), inline-код,
    формулы, строки заголовков, строки таблиц, шапки callout'ов, URL,
    markdown-ссылки и уже существующие [[ссылки]]. Если на заголовок уже
    есть [[ссылка]] (в т.ч. [[Заголовок|алиас]]), повторно не ставится.
    Длинные заголовки обрабатываются первыми. Идемпотентна."""
    if not body_md:
        return body_md
    lines = body_md.split("\n")
    titles = sorted({t.strip() for t in titles_to_link if t and t.strip()}, key=len, reverse=True)
    for title in titles:
        if re.search(rf"\[\[{re.escape(title)}[|#\]]", "\n".join(lines)):
            continue
        pattern = re.compile(rf"(?<!\w){re.escape(title)}(?!\w)")
        lines = _link_first_occurrence(lines, pattern, title)
    return "\n".join(lines)

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

def snap_link(raw: str, title_map: dict[str, str]) -> str | None:
    """Приводит ссылку, предложенную моделью, к каноническому заголовку:
    снимает обрамляющие [[ ]], отбрасывает URL-подобные значения (в links_out
    должны быть только заголовки заметок), снаппит к точному написанию
    через title_map (normalize_link_title(title) -> title). Если заголовка
    нет в карте — возвращает как есть (решение, допустима ли такая ссылка,
    принимает вызывающий код). None — ссылку нужно выбросить."""
    link = strip_wikilink_brackets(raw).strip()
    if not link:
        return None
    if "://" in link or link.startswith("www."):
        logger.warning(
            "Игнорируем URL-подобное значение в links_out (это не "
            "заголовок заметки): %r", link,
        )
        return None
    return title_map.get(normalize_link_title(link), link)