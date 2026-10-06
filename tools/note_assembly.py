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

from storage.models import DraftNote, NoteAction, NoteAnnotation, OutlineNote, SectionDraft, Plan
from tools.markdown_tools import build_note_path, normalize_link_title, insert_wikilinks, PROTECTED_SPAN_RE

# Порог вставки резюме-callout: короткой заметке резюме не нужно.
ABSTRACT_MIN_SECTIONS = 8

MIN_HEADING_LEVEL = 2
MAX_HEADING_LEVEL = 4

PLACEHOLDER_MARKDOWN = "_Раздел не удалось сгенерировать автоматически — заполните его вручную._"

# Латинские буквы, визуально неотличимые от кириллических.
# Латинские буквы, визуально неотличимые от кириллических.
_HOMOGLYPH_MAP = {
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х",
}
_HOMOGLYPHS = str.maketrans(_HOMOGLYPH_MAP)
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_LATIN_RE = re.compile(r"[A-Za-z]")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_NON_BREAKING_HYPHEN = "\u2011"

_CODE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)  # в т.ч. незакрытый fence
_WIKILINK_TEXT_RE = re.compile(r"\[\[([^\]|\n]+)(?:\|([^\]\n]*))?\]\]")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁ])")
MOC_DESCRIPTION_MAX_CHARS = 150

_FENCE_LINE_RE = re.compile(r"^\s{0,3}```")
_HEADING_LINE_RE = re.compile(r"^(#{1,6})(\s+.*)$")

HUMANITIES_CALLOUT = (
    "> [!warning] Проверьте факты\n"
    "> Даты, цитаты и имена в этой заметке сгенерированы без источников — "
    "сверьте их перед использованием."
)
# Для update блок дописывается в конец существующей заметки: callout
# должен говорить только о добавленных разделах.
HUMANITIES_UPDATE_CALLOUT = (
    "> [!warning] Проверьте факты\n"
    "> Даты, цитаты и имена в добавленных ниже разделах сгенерированы без "
    "источников — сверьте их перед использованием."
)

MOC_TAG = "moc"

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
    text = fix_text_glitches(markdown.strip())  # до сравнения заголовка
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
        blocks.append(HUMANITIES_UPDATE_CALLOUT if for_update else HUMANITIES_CALLOUT)
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

def apply_inline_links(draft: DraftNote) -> DraftNote:
    """Проставляет inline-[[ссылки]] в тело create-заметки по её links_out
    (только первое вхождение, без кода/заголовков/таблиц, см.
    markdown_tools.insert_wikilinks). Не применяется к update (там
    append_section дописывается в чужой файл), к MOC и к пустому телу.
    Ссылка заметки на саму себя не ставится."""
    if draft.action != NoteAction.CREATE or draft.is_moc or not draft.links_out or not draft.body_md:
        return draft
    own = normalize_link_title(draft.title)
    targets = [t for t in draft.links_out if normalize_link_title(t) != own]
    new_body = insert_wikilinks(draft.body_md, targets)
    return draft if new_body == draft.body_md else draft.model_copy(update={"body_md": new_body})


def build_moc(
    plan: Plan,
    drafts: list[DraftNote],
    annotations_by_note: dict[str, NoteAnnotation],
    *,
    domain: str,
    default_folder: str,
    existing_paths: set[str],
) -> DraftNote | None:
    """Детерминированно собирает MOC (оглавление темы) без LLM.

    Возвращает None, если create-заметок меньше двух. Тело: непустой
    plan.summary как callout «Кратко» и список [[заголовок]] с описанием
    (abstract аннотации либо первое предложение заметки). frontmatter.source
    у MOC не ставится: это оглавление, а не конспект по знаниям модели.
    Защита от коллизии пути/заголовка: суффикс « (2)», « (3)»…"""
    creates = [d for d in drafts if d.action == NoteAction.CREATE and not d.is_moc]
    if len(creates) < 2:
        return None

    taken_paths = set(existing_paths) | {d.path for d in drafts}
    taken_titles = {d.title for d in drafts}
    base = f"{plan.topic_title.strip()} — обзор"
    title, n = base, 1
    path = build_note_path(default_folder, title)
    while path in taken_paths or title in taken_titles:
        n += 1
        title = f"{base} ({n})"
        path = build_note_path(default_folder, title)

    items: list[str] = []
    for d in creates:
        ann = annotations_by_note.get(d.note_id) if d.note_id else None
        description = _moc_description(d, ann)
        items.append(f"- [[{d.title}]] — {description}" if description else f"- [[{d.title}]]")

    parts: list[str] = []
    if plan.summary.strip():
        parts.append(_callout("abstract", "Кратко", plan.summary.strip()))
    parts.append("## Заметки\n\n" + "\n".join(items))

    return DraftNote(
        note_id="",
        action=NoteAction.CREATE,
        path=path,
        title=title,
        folder=default_folder,
        frontmatter={"created": datetime.now(timezone.utc).date().isoformat()},
        body_md="\n\n".join(parts),
        tags=add_domain_tag([MOC_TAG], domain),
        links_out=[d.title for d in creates],
        is_moc=True,
    )

def _fix_word(match: re.Match) -> str:
    """Заменяет латинские омоглифы в слове, которое в основном кириллическое.
    Слово не трогаем, если в нём есть латинская буква без кириллического
    двойника (это идентификатор или термин, а не опечатка)."""
    word = match.group(0)
    latin = _LATIN_RE.findall(word)
    if not latin:
        return word
    if len(_CYRILLIC_RE.findall(word)) <= len(latin):
        return word
    if any(ch not in _HOMOGLYPH_MAP for ch in latin):
        return word
    return word.translate(_HOMOGLYPHS)


def _fix_plain_fragment(text: str) -> str:
    return _WORD_RE.sub(_fix_word, text.replace(_NON_BREAKING_HYPHEN, "-"))


def fix_text_glitches(text: str) -> str:
    """Чинит латинские омоглифы в кириллических словах и U+2011 -> '-'.
    Не трогает fenced-код, блочные формулы, inline-код, формулы, ссылки и URL."""
    out: list[str] = []
    in_fence = in_math = False
    for line in text.split("\n"):
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and line.strip() == "$$":
            in_math = not in_math
        elif not in_fence and not in_math:
            parts = PROTECTED_SPAN_RE.split(line)
            for j in range(0, len(parts), 2):  # чётные элементы — обычный текст
                parts[j] = _fix_plain_fragment(parts[j])
            line = "".join(parts)
        out.append(line)
    return "\n".join(out)


def plain_text(markdown: str, *, skip_callouts: bool = False) -> str:
    """Текст без кода, таблиц и заголовков; [[X|алиас]] -> текст ссылки.
    Шапки callout'ов пропускаются всегда; при skip_callouts=True пропускается
    и всё содержимое blockquote (служебные предупреждения сборки)."""
    text = _CODE_RE.sub(" ", markdown)
    lines: list[str] = []
    for ln in text.splitlines():
        stripped = ln.strip()
        if not stripped or stripped.startswith(("|", "#")):
            continue
        if stripped.startswith(">"):
            if skip_callouts or stripped.startswith("> [!"):
                continue
            stripped = stripped.lstrip("> ").strip()
        lines.append(stripped)
    text = _WIKILINK_TEXT_RE.sub(lambda m: m.group(2) or m.group(1), " ".join(lines))
    return re.sub(r"[*`]", "", text)


def first_sentence(text: str, limit: int = MOC_DESCRIPTION_MAX_CHARS) -> str:
    """Первое предложение (граница: .!? + пробел + заглавная буква, поэтому
    «т.е. значение» не рвётся), не длиннее limit символов по границе слова."""
    text = " ".join(text.split())
    if not text:
        return ""
    m = _SENTENCE_END_RE.search(text)
    sentence = text[:m.start()] if m else text
    if len(sentence) <= limit:
        return sentence
    return sentence[:limit].rsplit(" ", 1)[0] + "…"


def _moc_description(draft: DraftNote, annotation: NoteAnnotation | None) -> str:
    """abstract аннотации; если пуст — первое предложение первой секции из
    body_md (работает и для объединённых черновиков с note_id='')."""
    if annotation and annotation.abstract.strip():
        return " ".join(annotation.abstract.split())
    body = draft.body_md.replace(PLACEHOLDER_MARKDOWN, "")
    return first_sentence(plain_text(body, skip_callouts=True))