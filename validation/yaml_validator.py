from __future__ import annotations

import yaml

from storage.models import DraftNote, ValidationIssue

_ALLOWED_SCALAR_TYPES = (str, int, float, bool, type(None))


def validate_yaml_frontmatter(draft: DraftNote) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    def _check_value(key: str, value) -> None:
        if isinstance(value, list):
            for v in value:
                if not isinstance(v, _ALLOWED_SCALAR_TYPES):
                    issues.append(
                        ValidationIssue(
                            level="error",
                            code="yaml_unsupported_type",
                            message=f"Frontmatter key '{key}' содержит неподдерживаемый тип в списке: {type(v)}",
                            draft_id=draft.draft_id,
                        )
                    )
        elif not isinstance(value, _ALLOWED_SCALAR_TYPES):
            issues.append(
                ValidationIssue(
                    level="error",
                    code="yaml_unsupported_type",
                    message=f"Frontmatter key '{key}' имеет неподдерживаемый тип: {type(value)}",
                    draft_id=draft.draft_id,
                )
            )

    for key, value in draft.frontmatter.items():
        _check_value(key, value)

    # финальная проверка — YAML действительно сериализуется и парсится обратно
    try:
        dumped = yaml.safe_dump(draft.frontmatter, allow_unicode=True)
        yaml.safe_load(dumped)
    except Exception as exc:
        issues.append(
            ValidationIssue(
                level="error",
                code="yaml_roundtrip_failed",
                message=f"Frontmatter не проходит YAML round-trip: {exc}",
                draft_id=draft.draft_id,
            )
        )

    if not draft.title.strip():
        issues.append(
            ValidationIssue(
                level="error", code="empty_title", message="Пустой заголовок заметки", draft_id=draft.draft_id
            )
        )

    return issues
