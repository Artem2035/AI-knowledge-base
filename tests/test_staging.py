from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from staging.changeset import list_pending_tasks, load_changeset, save_changeset
from staging.commit import CommitError, commit_changeset
from storage.models import DraftNote, NoteAction, StagingChangeset, ValidationReport
from vault.db import VaultDB

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "test_vault"


def _copy_fixture_vault(tmp_path) -> Path:
    dest = tmp_path / "vault_copy"
    shutil.copytree(FIXTURE_VAULT, dest)
    return dest


def test_save_and_load_changeset_roundtrip(tmp_path):
    staging_dir = tmp_path / "staging"
    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/Test.md",
        title="Test",
        body_md="Содержимое тестовой заметки.",
    )
    changeset = StagingChangeset(task_id="task123", creates=[draft])
    save_changeset(staging_dir, changeset)

    loaded = load_changeset(staging_dir, "task123")
    assert loaded is not None
    assert loaded.task_id == "task123"
    assert loaded.creates[0].title == "Test"

    assert "task123" in list_pending_tasks(staging_dir)

    preview = staging_dir / "task123" / "notes_preview" / "Знания__Test.md"
    assert preview.exists()
    assert "Test" in preview.read_text(encoding="utf-8")


def test_commit_creates_file_in_vault(tmp_path):
    vault_path = _copy_fixture_vault(tmp_path)
    db = VaultDB(tmp_path / "index.sqlite3")

    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/Reranking.md",
        title="Reranking",
        frontmatter={"title": "Reranking"},
        body_md="Reranking — переупорядочивание результатов retrieval.",
    )
    changeset = StagingChangeset(task_id="commit1", creates=[draft])
    changeset.validation = ValidationReport(ok=True)

    results = commit_changeset(
        changeset=changeset,
        vault_path=vault_path,
        db=db,
        embedder=None,
        allow_delete=False,
        git_enabled=False,
        backup_dir=tmp_path / "backup",
    )

    assert len(results) == 1
    written = vault_path / "Знания" / "Reranking.md"
    assert written.exists()
    assert "Reranking" in written.read_text(encoding="utf-8")
    db.close()


def test_commit_rejects_unvalidated_changeset(tmp_path):
    vault_path = _copy_fixture_vault(tmp_path)
    db = VaultDB(tmp_path / "index.sqlite3")
    draft = DraftNote(action=NoteAction.CREATE, path="Знания/X.md", title="X", body_md="Текст.")
    changeset = StagingChangeset(task_id="commit2", creates=[draft])  # validation=None!

    with pytest.raises(CommitError):
        commit_changeset(
            changeset=changeset,
            vault_path=vault_path,
            db=db,
            embedder=None,
            allow_delete=False,
            git_enabled=False,
            backup_dir=tmp_path / "backup",
        )
    db.close()


def test_commit_rejects_deletes_without_flag(tmp_path):
    vault_path = _copy_fixture_vault(tmp_path)
    db = VaultDB(tmp_path / "index.sqlite3")
    changeset = StagingChangeset(
        task_id="commit3", deletes=["Знания/Python основы.md"], validation=ValidationReport(ok=True)
    )

    with pytest.raises(CommitError):
        commit_changeset(
            changeset=changeset,
            vault_path=vault_path,
            db=db,
            embedder=None,
            allow_delete=False,
            git_enabled=False,
            backup_dir=tmp_path / "backup",
        )
    db.close()


def test_commit_update_creates_backup(tmp_path):
    vault_path = _copy_fixture_vault(tmp_path)
    db = VaultDB(tmp_path / "index.sqlite3")

    original_path = vault_path / "Знания" / "Python основы.md"
    original_content = original_path.read_text(encoding="utf-8")

    draft = DraftNote(
        action=NoteAction.UPDATE,
        path="Знания/Python основы.md",
        title="Python основы",
        append_section="## Дополнение\n\nНовый материал.",
    )
    changeset = StagingChangeset(task_id="commit4", updates=[draft], validation=ValidationReport(ok=True))
    backup_dir = tmp_path / "backup"

    commit_changeset(
        changeset=changeset,
        vault_path=vault_path,
        db=db,
        embedder=None,
        allow_delete=False,
        git_enabled=False,
        backup_dir=backup_dir,
    )

    updated_content = original_path.read_text(encoding="utf-8")
    assert "Дополнение" in updated_content
    assert original_content in updated_content  # старое содержимое не потеряно

    backups = list(backup_dir.iterdir())
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original_content
    db.close()
