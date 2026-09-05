from __future__ import annotations

from markdown_it import MarkdownIt

from storage.models import DraftNote, ValidationIssue

_md = MarkdownIt("commonmark")


def validate_markdown_body(draft: DraftNote) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if not draft.body_md.strip() and not draft.append_section:
        issues.append(
            ValidationIssue(
                level="error",
                code="empty_body",
                message="Тело заметки пустое (и append_section тоже пуст)",
                draft_id=draft.draft_id,
            )
        )
        return issues

    text = draft.body_md or draft.append_section or ""

    if text.count("```") % 2 != 0:
        issues.append(
            ValidationIssue(
                level="error",
                code="unbalanced_code_fence",
                message="Незакрытый блок кода (нечётное число ```)",
                draft_id=draft.draft_id,
            )
        )

    try:
        _md.parse(text)
    except Exception as exc:
        issues.append(
            ValidationIssue(
                level="error",
                code="markdown_parse_error",
                message=f"Markdown не парсится: {exc}",
                draft_id=draft.draft_id,
            )
        )

    if len(text) < 40:
        issues.append(
            ValidationIssue(
                level="warning",
                code="very_short_note",
                message="Очень короткая заметка (<40 символов) — возможно, стоит объединить с другой",
                draft_id=draft.draft_id,
            )
        )

    return issues
