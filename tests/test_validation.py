from __future__ import annotations

from pathlib import Path

from storage.models import DraftNote, NoteAction, OutlineNote, OutlineSubpoint, Plan, StagingChangeset
from tools.note_assembly import PLACEHOLDER_MARKDOWN
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

def test_english_body_gets_language_warning(tmp_path):
    db = _db(tmp_path)
    body = ("Retrieval-augmented generation combines a retriever with a generator. " * 6)
    draft = DraftNote(action=NoteAction.CREATE, path="Знания/En.md", title="En", body_md=body)
    report = run_validation(StagingChangeset(task_id="t12", creates=[draft]), db, allow_delete=False)
    assert report.ok  # warning не блокирует approve
    assert any(i.code == "note_language_mismatch" for i in report.warnings)
    db.close()


def test_russian_body_with_code_no_language_warning(tmp_path):
    db = _db(tmp_path)
    body = ("Эмбеддинг — числовое векторное представление текста, при котором близкие "
            "по смыслу объекты оказываются рядом в пространстве. " * 3
            + "\n\n```python\nimport numpy as np\nvec = np.zeros(384)\n```\n")
    draft = DraftNote(action=NoteAction.CREATE, path="Знания/Ru.md", title="Ru", body_md=body)
    report = run_validation(StagingChangeset(task_id="t13", creates=[draft]), db, allow_delete=False)
    assert not any(i.code == "note_language_mismatch" for i in report.issues)
    db.close()

def _structural(report):
    return [i for i in report.warnings if i.code == "note_too_short_structural"]


def test_note_of_code_list_and_table_has_no_structural_warning(tmp_path):
    db = _db(tmp_path)
    body = (
        "## Раздел\n\n- пункт один\n- пункт два\n\n"
        "```python\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    draft = DraftNote(action=NoteAction.CREATE, path="Знания/Блоки.md", title="Блоки", body_md=body)
    report = run_validation(StagingChangeset(task_id="v1", creates=[draft]), db, allow_delete=False)
    assert report.ok and not _structural(report)
    db.close()


def test_three_paragraphs_have_no_structural_warning(tmp_path):
    db = _db(tmp_path)
    para = "Достаточно длинный абзац, чтобы он считался содержательным блоком."
    draft = DraftNote(action=NoteAction.CREATE, path="Знания/Абзацы.md", title="Абзацы",
                      body_md=f"{para}\n\n{para}\n\n{para}")
    report = run_validation(StagingChangeset(task_id="v2", creates=[draft]), db, allow_delete=False)
    assert not _structural(report)
    db.close()


def test_callouts_and_placeholder_do_not_count_as_content(tmp_path):
    db = _db(tmp_path)
    body = (
        "> [!abstract] Кратко\n> Длинное резюме заметки, которое не должно считаться блоком.\n\n"
        "## A\n\n> [!warning] Требует проверки\n> Детали этого раздела могут быть неточными.\n\n"
        f"{PLACEHOLDER_MARKDOWN}\n\n"
        "## B\n\nЕдинственный настоящий абзац заметки, достаточно длинный для подсчёта."
    )
    draft = DraftNote(action=NoteAction.CREATE, path="Знания/Заглушки.md", title="Заглушки", body_md=body)
    report = run_validation(StagingChangeset(task_id="v3", creates=[draft]), db, allow_delete=False)
    issues = _structural(report)
    assert report.ok and len(issues) == 1 and "только 1" in issues[0].message
    db.close()


def test_unbalanced_fence_is_error(tmp_path):
    db = _db(tmp_path)
    draft = DraftNote(action=NoteAction.CREATE, path="Знания/Fence.md", title="Fence",
                      body_md="Текст заметки достаточно длинный для проверки.\n\n```python\nx = 1")
    report = run_validation(StagingChangeset(task_id="v4", creates=[draft]), db, allow_delete=False)
    assert not report.ok and any(i.code == "unbalanced_code_fence" for i in report.errors)
    db.close()


def test_inline_triple_backticks_are_not_flagged(tmp_path):
    db = _db(tmp_path)
    draft = DraftNote(action=NoteAction.CREATE, path="Знания/Inline.md", title="Inline",
                      body_md="Тройные кавычки ``` внутри строки это просто текст, а не ограждение блока кода.")
    report = run_validation(StagingChangeset(task_id="v5", creates=[draft]), db, allow_delete=False)
    assert not any(i.code == "unbalanced_code_fence" for i in report.issues)
    db.close()

def test_link_to_file_stem_of_draft_is_not_broken(tmp_path):
    """Ссылка по имени файла (а не по title) не должна давать broken_wikilink."""
    db = _db(tmp_path)
    target = DraftNote(action=NoteAction.CREATE, path="Знания/Файл.md", title="Другой заголовок",
                       body_md="Достаточно длинный текст заметки, чтобы пройти проверку длины.")
    source = DraftNote(action=NoteAction.CREATE, path="Знания/Источник.md", title="Источник",
                       body_md="Достаточно длинный текст заметки, чтобы пройти проверку длины.",
                       links_out=["Файл"])
    report = run_validation(StagingChangeset(task_id="t14", creates=[target, source]), db, allow_delete=False)
    assert not any(i.code == "broken_wikilink" for i in report.issues)
    db.close()

def test_moc_is_exempt_from_structural_warning(tmp_path):
    db = _db(tmp_path)
    moc = DraftNote(
        action=NoteAction.CREATE, path="Знания/Тема/Обзор.md", title="Обзор", is_moc=True,
        body_md="## Заметки\n\n- [[Первая заметка темы]]\n- [[Вторая заметка темы]]",
        links_out=["Первая заметка темы", "Вторая заметка темы"],
    )
    report = run_validation(StagingChangeset(task_id="m1", creates=[moc]), db, allow_delete=False)
    assert report.ok and not _structural(report)
    db.close()