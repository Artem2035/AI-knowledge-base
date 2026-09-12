from __future__ import annotations

from storage.models import StagingChangeset, ValidationIssue, ValidationReport
from vault.db import VaultDB

from .link_validator import validate_links, validate_no_path_collisions
from .markdown_validator import validate_markdown_body, validate_headings_coverage
from .yaml_validator import validate_yaml_frontmatter


def validate_no_unauthorized_deletes(
    changeset: StagingChangeset, allow_delete: bool
) -> list[ValidationIssue]:
    if changeset.deletes and not allow_delete:
        return [
            ValidationIssue(
                level="error",
                code="delete_not_allowed",
                message=(
                    f"Changeset содержит {len(changeset.deletes)} операций удаления, "
                    "но ALLOW_DELETE=false. Удаление заблокировано по умолчанию."
                ),
            )
        ]
    return []


def run_validation(
    changeset: StagingChangeset, db: VaultDB, allow_delete: bool, plan: "Plan | None" = None
) -> ValidationReport:
    all_drafts = changeset.creates + changeset.updates
    issues: list[ValidationIssue] = []

    issues += validate_no_unauthorized_deletes(changeset, allow_delete)
    issues += validate_no_path_collisions(all_drafts, db)
    issues += validate_links(all_drafts, db)

    notes_by_id = {n.note_id: n for n in plan.notes} if plan else {}
    for draft in all_drafts:
        issues += validate_yaml_frontmatter(draft)
        issues += validate_markdown_body(draft)
        issues += validate_headings_coverage(draft, notes_by_id.get(draft.note_id))

    ok = not any(i.level == "error" for i in issues)
    return ValidationReport(ok=ok, issues=issues)

__all__ = ["run_validation", "validate_no_unauthorized_deletes"]
