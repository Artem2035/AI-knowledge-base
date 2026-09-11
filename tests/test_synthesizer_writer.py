from __future__ import annotations

from pydantic import BaseModel

from gemini.schemas import DraftNoteOutput, NotePlanItem
from roles.synthesizer_writer import _DEPTH_WORD_RANGES, write_note
from storage.models import Evidence, NoteAction, SourceCandidate, TaskStatus


class _FakeClient:
    """Мок LLMClient (см. llm/base.py::LLMClient Protocol) — не делает
    реальных вызовов, просто фиксирует последний prompt для проверки и
    возвращает заранее заданный output."""

    def __init__(self, output: BaseModel):
        self.output = output
        self.last_prompt: str | None = None
        self.last_system_instruction: str | None = None

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        self.last_prompt = prompt
        self.last_system_instruction = system_instruction
        return self.output


def _evidence() -> list[Evidence]:
    return [
        Evidence(concept="RAG", statement="RAG объединяет retrieval и generation.", source_id="s1"),
    ]


def _item(depth_hint: str) -> NotePlanItem:
    return NotePlanItem(
        title="RAG",
        action="create",
        folder="Знания",
        evidence_indices=[0],
        depth_hint=depth_hint,
    )


def _output() -> DraftNoteOutput:
    return DraftNoteOutput(
        action="create",
        title="RAG",
        body_md="Текст заметки про RAG, достаточно длинный для теста.",
        tags=["rag"],
        links_out=[],
    )


def test_write_note_includes_standard_target_words_in_prompt():
    client = _FakeClient(_output())
    status = TaskStatus(task_id="t1")

    write_note(
        _item("standard"), _evidence(), known_titles=[], title_map={},
        sources=[], client=client, status=status, default_folder="Знания",
    )

    assert _DEPTH_WORD_RANGES["standard"] in client.last_prompt
    assert _DEPTH_WORD_RANGES["long"] not in client.last_prompt


def test_write_note_includes_long_target_words_in_prompt():
    client = _FakeClient(_output())
    status = TaskStatus(task_id="t2")

    write_note(
        _item("long"), _evidence(), known_titles=[], title_map={},
        sources=[], client=client, status=status, default_folder="Знания",
    )

    assert _DEPTH_WORD_RANGES["long"] in client.last_prompt


def test_write_note_propagates_depth_hint_to_draft_note():
    client = _FakeClient(_output())
    status = TaskStatus(task_id="t3")

    draft = write_note(
        _item("long"), _evidence(), known_titles=[], title_map={},
        sources=[], client=client, status=status, default_folder="Знания",
    )

    assert draft.depth_hint == "long"
    assert draft.action == NoteAction.CREATE


def test_note_plan_item_depth_hint_defaults_to_standard():
    item = NotePlanItem(title="X", action="create")
    assert item.depth_hint == "standard"