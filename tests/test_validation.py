from __future__ import annotations

from pathlib import Path

from storage.models import DraftNote, NoteAction, OutlineNote, OutlineSubpoint, Plan, StagingChangeset
from validation import run_validation
from vault.db import VaultDB
from vault.index import VaultIndexer

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "test_vault"


def _db(tmp_path):
    db = VaultDB(tmp_path / "index.sqlite3")
    VaultIndexer(db=db, vault_path=FIXTURE_VAULT, embedder=None).sync()
    return db


def test_valid_create_note_passes(tmp_path):
    db = _db(tmp_path)
    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/Reranking.md",
        title="Reranking",
        frontmatter={"title": "Reranking", "tags": ["rag"]},
        body_md="Reranking — это переупорядочивание результатов retrieval по релевантности.",
        tags=["rag"],
        links_out=["Эмбеддинги"],
    )
    changeset = StagingChangeset(task_id="t1", creates=[draft])
    report = run_validation(changeset, db, allow_delete=False)
    assert report.ok, [i.message for i in report.errors]
    db.close()


def test_create_colliding_with_existing_note_is_error(tmp_path):
    db = _db(tmp_path)
    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/Эмбеддинги.md",  # уже существует!
        title="Эмбеддинги",
        body_md="Дублирующий контент.",
    )
    changeset = StagingChangeset(task_id="t2", creates=[draft])
    report = run_validation(changeset, db, allow_delete=False)
    assert not report.ok
    assert any(i.code == "create_collides_with_existing" for i in report.errors)
    db.close()


def test_update_missing_target_is_error(tmp_path):
    db = _db(tmp_path)
    draft = DraftNote(
        action=NoteAction.UPDATE,
        path="Знания/Не существует.md",
        title="Не существует",
        body_md="...",
    )
    changeset = StagingChangeset(task_id="t3", updates=[draft])
    report = run_validation(changeset, db, allow_delete=False)
    assert not report.ok
    assert any(i.code == "update_missing_target" for i in report.errors)
    db.close()


def test_empty_body_is_error(tmp_path):
    db = _db(tmp_path)
    draft = DraftNote(action=NoteAction.CREATE, path="Знания/Пусто.md", title="Пусто", body_md="")
    changeset = StagingChangeset(task_id="t4", creates=[draft])
    report = run_validation(changeset, db, allow_delete=False)
    assert not report.ok
    assert any(i.code == "empty_body" for i in report.errors)
    db.close()


def test_deletes_blocked_by_default(tmp_path):
    db = _db(tmp_path)
    changeset = StagingChangeset(task_id="t5", deletes=["Знания/Python основы.md"])
    report = run_validation(changeset, db, allow_delete=False)
    assert not report.ok
    assert any(i.code == "delete_not_allowed" for i in report.errors)
    db.close()


def test_deletes_allowed_when_flag_set(tmp_path):
    db = _db(tmp_path)
    changeset = StagingChangeset(task_id="t6", deletes=["Знания/Python основы.md"])
    report = run_validation(changeset, db, allow_delete=True)
    assert not any(i.code == "delete_not_allowed" for i in report.issues)
    db.close()


def test_broken_wikilink_is_warning_not_error(tmp_path):
    db = _db(tmp_path)
    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/Новое.md",
        title="Новое",
        body_md="Содержимое достаточно длинное, чтобы пройти проверку минимальной длины текста.",
        links_out=["Заметка которой точно нет"],
    )
    changeset = StagingChangeset(task_id="t7", creates=[draft])
    report = run_validation(changeset, db, allow_delete=False)
    assert report.ok  # это warning, не error
    assert any(i.code == "broken_wikilink" for i in report.warnings)
    db.close()


def test_duplicate_path_in_same_changeset_is_error(tmp_path):
    db = _db(tmp_path)
    d1 = DraftNote(action=NoteAction.CREATE, path="Знания/X.md", title="X", body_md="Текст первой заметки X.")
    d2 = DraftNote(action=NoteAction.CREATE, path="Знания/X.md", title="X2", body_md="Текст второй заметки X.")
    changeset = StagingChangeset(task_id="t8", creates=[d1, d2])
    report = run_validation(changeset, db, allow_delete=False)
    assert not report.ok
    assert any(i.code == "duplicate_path_in_changeset" for i in report.errors)
    db.close()


def test_run_validation_without_plan_skips_headings_coverage(tmp_path):
    """Регрессия: run_validation без plan (напр. старые вызовы без него)
    не должен падать — validate_headings_coverage просто ничего не
    добавляет, т.к. notes_by_id пуст."""
    db = _db(tmp_path)
    draft = DraftNote(
        action=NoteAction.CREATE, path="Знания/Без плана.md", title="Без плана",
        body_md="Текст заметки без привязки к плану.",
    )
    changeset = StagingChangeset(task_id="t9", creates=[draft])
    report = run_validation(changeset, db, allow_delete=False)  # plan не передан
    assert report.ok
    assert not any(i.code == "missing_outline_heading" for i in report.issues)
    db.close()


def test_missing_outline_heading_is_warning(tmp_path):
    db = _db(tmp_path)
    note = OutlineNote(
        title="Chunking",
        subpoints=[
            OutlineSubpoint(heading="Определение", covers="Что такое chunking"),
            OutlineSubpoint(heading="Стратегии", covers="Какие бывают стратегии"),
        ],
    )
    plan = Plan(task_id="p1", topic_title="RAG", notes=[note])

    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/Chunking.md",
        title="Chunking",
        note_id=note.note_id,
        body_md=(
            "## Определение\n\nChunking — разбиение текста на фрагменты.\n\n"
            "Раздел «Стратегии» отсутствует, хотя заявлен планом."
        ),
    )
    changeset = StagingChangeset(task_id="t10", creates=[draft])
    report = run_validation(changeset, db, allow_delete=False, plan=plan)

    assert report.ok  # это warning, не error — не блокирует approve
    assert any(
        i.code == "missing_outline_heading" and "Стратегии" in i.message
        for i in report.warnings
    )
    db.close()


def test_all_outline_headings_present_no_warning(tmp_path):
    db = _db(tmp_path)
    note = OutlineNote(
        title="Chunking",
        subpoints=[OutlineSubpoint(heading="Определение", covers="Что такое chunking")],
    )
    plan = Plan(task_id="p2", topic_title="RAG", notes=[note])

    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/Chunking.md",
        title="Chunking",
        note_id=note.note_id,
        body_md="## Определение\n\nChunking — разбиение текста на фрагменты перед индексацией.",
    )
    changeset = StagingChangeset(task_id="t11", creates=[draft])
    report = run_validation(changeset, db, allow_delete=False, plan=plan)

    assert not any(i.code == "missing_outline_heading" for i in report.warnings)
    db.close()