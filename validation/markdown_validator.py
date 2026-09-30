from __future__ import annotations

import re
from markdown_it import MarkdownIt
from storage.models import DraftNote, ValidationIssue, NoteAction

_md = MarkdownIt("commonmark")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_URL_RE = re.compile(r"https?://\S+")
_MATH_BLOCK_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_MATH_INLINE_RE = re.compile(r"\$[^$\n]+\$")

# Порог мягкий: русский текст с терминами даёт ~0.6-0.9, полностью
# английский — около 0. Слишком короткие тексты не проверяем (шум).
_MIN_CYRILLIC_SHARE = 0.3
_MIN_LETTERS_FOR_LANG_CHECK = 200


def _cyrillic_share(text: str) -> tuple[float, int]:
    """Доля кириллицы среди букв ПРОЗЫ (без кода, URL и формул).
    Возвращает (доля, число учтённых букв). Нет букв -> (1.0, 0)."""
    cleaned = _CODE_FENCE_RE.sub(" ", text)
    cleaned = _MATH_BLOCK_RE.sub(" ", cleaned)
    cleaned = _MATH_INLINE_RE.sub(" ", cleaned)
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)

    cyr = lat = 0
    for ch in cleaned.lower():
        if "а" <= ch <= "я" or ch == "ё":
            cyr += 1
        elif "a" <= ch <= "z":
            lat += 1
    total = cyr + lat
    return (cyr / total if total else 1.0), total

def _count_substantial_paragraphs(text: str, min_chars: int = 40) -> int:
    tokens = _md.parse(text)
    count = 0
    for i, tok in enumerate(tokens):
        if tok.type == "inline" and i > 0 and tokens[i - 1].type == "paragraph_open":
            if len(tok.content.strip()) >= min_chars:
                count += 1
    return count


def validate_markdown_body(draft: DraftNote) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if not draft.body_md.strip() and not draft.append_section:
        issues.append(ValidationIssue(
            level="error", code="empty_body",
            message="Тело заметки пустое (и append_section тоже пуст)",
            draft_id=draft.draft_id,
        ))
        return issues

    text = draft.body_md or draft.append_section or ""

    if text.count("```") % 2 != 0:
        issues.append(ValidationIssue(
            level="error", code="unbalanced_code_fence",
            message="Незакрытый блок кода (нечётное число ```)",
            draft_id=draft.draft_id,
        ))

    try:
        _md.parse(text)
    except Exception as exc:
        issues.append(ValidationIssue(
            level="error", code="markdown_parse_error",
            message=f"Markdown не парсится: {exc}", draft_id=draft.draft_id,
        ))

    if len(text) < 40:
        issues.append(ValidationIssue(
            level="warning", code="very_short_note",
            message="Очень короткая заметка (<40 символов) — возможно, стоит объединить с другой",
            draft_id=draft.draft_id,
        ))
    elif draft.action == NoteAction.CREATE:
        substantial = _count_substantial_paragraphs(text)
        if substantial < 3:
            issues.append(ValidationIssue(
                level="warning",
                code="note_too_short_structural",
                message=(
                    f"Новая заметка содержит только {substantial} содержательных "
                    "абзац(-а/-ев) — рекомендуемый минимум 3. Рассмотрите объединение "
                    "со смежной темой в одну структурированную заметку."
                ),
                draft_id=draft.draft_id,
            ))
    # Дешёвая защита от «съезда» body_md на английский после перевода
    # системных инструкций (см. LANGUAGE_RULE в llm/prompts/common.py).
    share, letters = _cyrillic_share(text)
    if letters >= _MIN_LETTERS_FOR_LANG_CHECK and share < _MIN_CYRILLIC_SHARE:
        issues.append(ValidationIssue(
            level="warning", code="note_language_mismatch",
            message=(
                f"Доля кириллицы в тексте заметки всего {share:.0%} — возможно, "
                "модель написала заметку не на русском. Проверьте перед approve."
            ),
            draft_id=draft.draft_id,
        ))

    return issues

def validate_headings_coverage(draft: DraftNote, note: "OutlineNote | None") -> list[ValidationIssue]:
    if note is None:
        return []
    text = draft.body_md or draft.append_section or ""
    return [
        ValidationIssue(
            level="warning", code="missing_outline_heading",
            message=f"Заголовок «{sp.heading}» из плана отсутствует в тексте заметки «{draft.title}»",
            draft_id=draft.draft_id,
        )
        for sp in note.subpoints if f"## {sp.heading}" not in text
    ]