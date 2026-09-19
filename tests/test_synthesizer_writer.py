from __future__ import annotations

import pytest

from llm.schemas import DraftNoteOutput
from roles.synthesizer_writer import build_relationships, prepare_linking_context, write_note
from storage.models import DraftNote, Evidence, NoteAction, OutlineNote, OutlineSubpoint, TaskStatus


class _FakeClient:
    """Мок LLMClient — возвращает заранее заданные DraftNoteOutput по
    порядку и сохраняет последний prompt для инспекции содержимого."""

    def __init__(self, outputs: list[DraftNoteOutput]):
        self._outputs = list(outputs)
        self.last_prompt: str | None = None
        self.calls = 0

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        assert role == "synthesizer_write"
        self.calls += 1
        self.last_prompt = prompt
        return self._outputs.pop(0)


def _note(action: str = "create", existing_path: str = "") -> OutlineNote:
    return OutlineNote(
        title="Reranking",
        folder="Знания/RAG",
        action=action,
        existing_path=existing_path,
        subpoints=[
            OutlineSubpoint(heading="Определение", covers="Что такое reranking"),
            OutlineSubpoint(heading="Применение", covers="Где используется"),
        ],
    )


def test_prepare_linking_context_builds_sorted_titles_and_normalized_map():
    notes = [
        OutlineNote(title="Векторные базы данных"),
        OutlineNote(title="Эмбеддинги"),
    ]
    known_titles, title_map = prepare_linking_context(notes)

    assert known_titles == ["Векторные базы данных", "Эмбеддинги"]
    assert title_map["Эмбеддинги"] == "Эмбеддинги"


def test_write_note_groups_evidence_by_subpoint_and_filters_foreign_note():
    """Регрессия: в промпт должны попадать только факты, назначенные
    ЭТОЙ заметке (по note_id) — не весь список evidence задачи."""
    note = _note()
    sp0, sp1 = note.subpoints
    evidence = [
        Evidence(note_id=note.note_id, subpoint_id=sp0.subpoint_id, statement="Reranking переупорядочивает результаты."),
        Evidence(note_id=note.note_id, subpoint_id=sp1.subpoint_id, statement="Используется после retrieval, перед generation."),
        Evidence(note_id="другая-заметка", subpoint_id="чужой-раздел", statement="Не должно попасть в промпт."),
    ]
    output = DraftNoteOutput(action="create", title="Reranking", body_md="Текст.", tags=["rag"], links_out=[])
    client = _FakeClient([output])
    status = TaskStatus(task_id="s1")

    write_note(note, evidence, known_titles=[], title_map={}, client=client, status=status)

    assert "Reranking переупорядочивает результаты." in client.last_prompt
    assert "Используется после retrieval" in client.last_prompt
    assert "Не должно попасть в промпт." not in client.last_prompt
    assert "Определение" in client.last_prompt
    assert "Применение" in client.last_prompt


def test_write_note_create_builds_path_from_plan_folder_and_title():
    note = _note(action="create")
    output = DraftNoteOutput(action="create", title="Reranking", body_md="Текст заметки.", tags=[], links_out=[])
    client = _FakeClient([output])
    status = TaskStatus(task_id="s2")

    draft = write_note(note, [], known_titles=[], title_map={}, client=client, status=status)

    assert draft.action == NoteAction.CREATE
    assert draft.path == "Знания/RAG/Reranking.md"
    assert draft.note_id == note.note_id


def test_write_note_update_uses_existing_path_from_plan_not_from_model():
    """Регрессия: путь/action для update решает план (уже определённый
    Vault Analyst-ом), модель не может переопределить их через
    DraftNoteOutput.action — это foreign key, которым управляет код."""
    note = _note(action="update", existing_path="Знания/Retrieval.md")
    output = DraftNoteOutput(
        action="create",  # модель ошибочно вернула create — должно игнорироваться
        title="Reranking", append_section="## Reranking\n\nНовый материал.", tags=[], links_out=[],
    )
    client = _FakeClient([output])
    status = TaskStatus(task_id="s3")

    draft = write_note(note, [], known_titles=[], title_map={}, client=client, status=status)

    assert draft.action == NoteAction.UPDATE
    assert draft.path == "Знания/Retrieval.md"
    assert draft.append_section == "## Reranking\n\nНовый материал."


def test_write_note_update_without_existing_path_raises():
    note = _note(action="update", existing_path="")
    output = DraftNoteOutput(action="update", title="X", append_section="доп.", tags=[], links_out=[])
    client = _FakeClient([output])
    status = TaskStatus(task_id="s4")

    with pytest.raises(ValueError):
        write_note(note, [], known_titles=[], title_map={}, client=client, status=status)


def test_write_note_snaps_links_out_to_canonical_titles():
    note = _note()
    title_map = {"векторные базы данных": "Векторные базы данных"}
    output = DraftNoteOutput(
        action="create", title="Reranking", body_md="Текст.",
        tags=[], links_out=["векторные базы данных", "Совсем новая тема"],
    )
    client = _FakeClient([output])
    status = TaskStatus(task_id="s5")

    draft = write_note(note, [], known_titles=[], title_map=title_map, client=client, status=status)

    assert "Векторные базы данных" in draft.links_out
    assert "Совсем новая тема" in draft.links_out


def test_write_note_ignores_url_like_links_out():
    note = _note()
    output = DraftNoteOutput(
        action="create", title="Reranking", body_md="Текст.",
        tags=[], links_out=["https://example.com/article", "Эмбеддинги"],
    )
    client = _FakeClient([output])
    status = TaskStatus(task_id="s6")

    draft = write_note(note, [], known_titles=[], title_map={}, client=client, status=status)

    assert "https://example.com/article" not in draft.links_out
    assert "Эмбеддинги" in draft.links_out


def test_write_note_marks_source_in_frontmatter_when_requested():
    note = _note()
    output = DraftNoteOutput(action="create", title="Reranking", body_md="Текст.", tags=[], links_out=[])
    client = _FakeClient([output])
    status = TaskStatus(task_id="s7")

    draft = write_note(
        note, [], known_titles=[], title_map={}, client=client, status=status,
        mark_source="model-knowledge",
    )

    assert draft.frontmatter.get("source") == "model-knowledge"


def test_write_note_without_mark_source_omits_frontmatter_key():
    note = _note()
    output = DraftNoteOutput(action="create", title="Reranking", body_md="Текст.", tags=[], links_out=[])
    client = _FakeClient([output])
    status = TaskStatus(task_id="s7b")

    draft = write_note(note, [], known_titles=[], title_map={}, client=client, status=status)

    assert "source" not in draft.frontmatter


def test_write_note_extra_instructions_included_in_prompt():
    note = _note()
    output = DraftNoteOutput(action="create", title="Reranking", body_md="v2", tags=[], links_out=[])
    client = _FakeClient([output])
    status = TaskStatus(task_id="s8")

    write_note(
        note, [], known_titles=[], title_map={}, client=client, status=status,
        extra_instructions="Добавь больше примеров.",
    )

    assert "Добавь больше примеров." in client.last_prompt


def test_write_note_headings_listed_in_plan_order():
    """Регрессия: заголовки разделов должны идти в промпте в том же
    порядке, что заданы планом — Writer больше не выбирает структуру
    сам, только заполняет её (см. WRITE_SYSTEM_INSTRUCTION)."""
    note = _note()  # subpoints: Определение, Применение
    output = DraftNoteOutput(action="create", title="Reranking", body_md="Текст.", tags=[], links_out=[])
    client = _FakeClient([output])
    status = TaskStatus(task_id="s9")

    write_note(note, [], known_titles=[], title_map={}, client=client, status=status)

    idx_def = client.last_prompt.index("Определение")
    idx_prim = client.last_prompt.index("Применение")
    assert idx_def < idx_prim


def test_build_relationships_maps_titles_to_paths():
    d1 = DraftNote(action=NoteAction.CREATE, path="Знания/A.md", title="A", links_out=["B"])
    d2 = DraftNote(action=NoteAction.CREATE, path="Знания/B.md", title="B", links_out=[])

    rels = build_relationships([d1, d2])

    assert len(rels) == 1
    assert rels[0].from_note == "Знания/A.md"
    assert rels[0].to_note == "Знания/B.md"
    assert rels[0].link_type == "wikilink"


def test_build_relationships_unresolved_title_falls_back_to_title_itself():
    d1 = DraftNote(action=NoteAction.CREATE, path="Знания/A.md", title="A", links_out=["Неизвестная тема"])

    rels = build_relationships([d1])

    assert rels[0].to_note == "Неизвестная тема"