from __future__ import annotations

from storage.models import DraftNote, NoteAction, StagingChangeset, ValidationIssue
from validation import run_validation
from validation.autofix import autofix_drafts
from vault.db import VaultDB


def _create(title: str, path: str | None = None, **kw) -> DraftNote:
    return DraftNote(
        action=NoteAction.CREATE, path=path or f"Знания/{title}.md",
        title=title, body_md="Текст.", **kw,
    )


def test_duplicate_path_in_changeset_gets_suffix():
    result, issues = autofix_drafts([_create("X"), _create("X")], set())
    assert [d.title for d in result] == ["X", "X (2)"]
    assert result[1].path == "Знания/X (2).md"
    assert [i.code for i in issues] == ["path_autofixed"] and issues[0].level == "warning"


def test_collision_with_vault_renames_and_redirects_links():
    a = _create("A")
    b = _create("B", links_out=["A"])
    result, issues = autofix_drafts([a, b], {"Знания/A.md"})
    assert result[0].title == "A (2)" and result[0].path == "Знания/A (2).md"
    assert result[1].links_out == ["A (2)"]
    assert len(issues) == 1


def test_duplicate_does_not_redirect_links():
    result, _ = autofix_drafts([_create("X"), _create("X"), _create("B", links_out=["X"])], set())
    assert result[2].links_out == ["X"]


def test_update_and_clean_drafts_untouched():
    upd = DraftNote(action=NoteAction.UPDATE, path="Знания/Старая.md", title="Старая", append_section="доп.")
    clean = _create("Чистая")
    result, issues = autofix_drafts([upd, clean], {"Знания/Старая.md"})
    assert result == [upd, clean] and issues == []


def test_run_validation_appends_extra_issues(tmp_path):
    db = VaultDB(tmp_path / "i.sqlite3")
    extra = [ValidationIssue(level="warning", code="path_autofixed", message="m")]
    report = run_validation(StagingChangeset(task_id="x"), db, allow_delete=False, extra_issues=extra)
    assert report.ok and [i.code for i in report.warnings] == ["path_autofixed"]
    db.close()