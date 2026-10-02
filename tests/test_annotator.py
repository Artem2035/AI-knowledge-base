from __future__ import annotations

from llm.common import LLMSchemaError
from llm.schemas import AnnotationBatchOutput, AnnotationItem
from roles.annotator import (
    annotate_notes, annotate_notes_sync, build_digest, normalize_tags,
)
from roles.synthesizer_writer import prepare_linking_context
from storage.models import OutlineNote, OutlineSubpoint, Plan, SectionDraft, TaskStatus


class _FakeClient:
    """Мок LLMClient: handler(номер_вызова, prompt) -> AnnotationBatchOutput или raise."""

    def __init__(self, handler):
        self.handler = handler
        self.prompts: list[str] = []

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        assert role == "annotator"
        self.prompts.append(prompt)
        return self.handler(len(self.prompts), prompt)


def _note(title: str, n_sub: int = 2, action: str = "create") -> OutlineNote:
    return OutlineNote(
        title=title, action=action,
        existing_path="Старая.md" if action == "update" else "",
        subpoints=[OutlineSubpoint(heading=f"Р{i}", covers="c") for i in range(n_sub)],
    )


def _sections(note: OutlineNote, text: str = "Текст раздела.") -> list[SectionDraft]:
    return [
        SectionDraft(note_id=note.note_id, subpoint_id=sp.subpoint_id, markdown=text)
        for sp in note.subpoints
    ]


def _plan(*notes: OutlineNote) -> Plan:
    return Plan(task_id="t", topic_title="Тема", notes=list(notes))


def _status() -> TaskStatus:
    return TaskStatus(task_id="a")


def _all_items(prompt: str) -> AnnotationBatchOutput:
    n = prompt.count("=== Заметка [")
    return AnnotationBatchOutput(items=[AnnotationItem(index=i, tags=["т"]) for i in range(n)])


def test_digest_strips_code_tables_headings_and_unclosed_fence():
    note = _note("N", 1)
    md = ("Первое предложение.\n\n```python\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
          "### Подзаголовок\n\nВторой абзац.\n\n```python\ny = 2")
    digest = build_digest(note, _sections(note, md))
    assert "Первое предложение." in digest and "Второй абзац." in digest
    for banned in ("x = 1", "y = 2", "|", "Подзаголовок", "```"):
        assert banned not in digest


def test_annotate_maps_index_cleans_tags_and_links():
    a, b = _note("A"), _note("B")
    plan = _plan(a, b)
    _, title_map = prepare_linking_context(plan.notes)
    out = AnnotationBatchOutput(items=[
        AnnotationItem(index=0, tags=["#RAG", "Векторный поиск", "rag", "  "],
                       links_out=["[[B]]", "A", "https://x.com", "Неизвестная"], abstract="Резюме"),
        AnnotationItem(index=1, links_out=["A"]),
    ])
    result = annotate_notes_sync(plan, _sections(a) + _sections(b), title_map, _FakeClient(lambda n, p: out), _status())

    assert [r.note_id for r in result] == [a.note_id, b.note_id]
    assert result[0].tags == ["rag", "векторный-поиск"]
    assert result[0].links_out == ["B"]          # своя, URL и неизвестная отброшены
    assert result[0].abstract == ""              # <8 секций: резюме не запрашивалось
    assert result[1].links_out == ["A"]


def test_abstract_requested_and_kept_only_for_big_notes():
    big, small = _note("Большая", 8), _note("Малая", 2)
    plan = _plan(big, small)
    _, title_map = prepare_linking_context(plan.notes)
    out = AnnotationBatchOutput(items=[
        AnnotationItem(index=0, abstract="Резюме"), AnnotationItem(index=1, abstract="Резюме"),
    ])
    client = _FakeClient(lambda n, p: out)
    result = annotate_notes_sync(plan, [], title_map, client, _status())

    assert client.prompts[0].count("abstract: required") == 1
    assert result[0].abstract == "Резюме" and result[1].abstract == ""


def test_update_notes_are_skipped():
    c, u = _note("C"), _note("U", action="update")
    plan = _plan(c, u)
    _, title_map = prepare_linking_context(plan.notes)
    client = _FakeClient(lambda n, p: _all_items(p))
    result = annotate_notes_sync(plan, [], title_map, client, _status())

    assert len(client.prompts) == 1
    assert "=== Заметка [1] ===" not in client.prompts[0]
    assert [r.note_id for r in result] == [c.note_id]


def test_resume_skips_done_notes():
    a = _note("A")
    plan = _plan(a)
    client = _FakeClient(lambda n, p: _all_items(p))
    got: list = []
    annotate_notes(plan, [], {}, client, _status(), {a.note_id}, lambda ids, an: got.append(ids))
    assert client.prompts == [] and got == []


def test_schema_error_splits_batch_in_half():
    a, b = _note("A"), _note("B")
    plan = _plan(a, b)

    def handler(n, prompt):
        if "=== Заметка [1] ===" in prompt:
            raise LLMSchemaError("битый JSON")
        return _all_items(prompt)

    client = _FakeClient(handler)
    result = annotate_notes_sync(plan, [], {}, client, _status())
    assert len(client.prompts) == 3
    assert [r.tags for r in result] == [["т"], ["т"]]


def test_persistent_schema_error_gives_empty_annotations_without_raising():
    plan = _plan(_note("A"), _note("B"))

    def handler(n, prompt):
        raise LLMSchemaError("битый JSON")

    result = annotate_notes_sync(plan, [], {}, _FakeClient(handler), _status())
    assert len(result) == 2 and all(not r.tags and not r.links_out and not r.abstract for r in result)


def test_bad_or_missing_index_gives_empty_annotation():
    a, b = _note("A"), _note("B")
    out = AnnotationBatchOutput(items=[AnnotationItem(index=99, tags=["мусор"])])
    result = annotate_notes_sync(_plan(a, b), [], {}, _FakeClient(lambda n, p: out), _status())
    assert [r.note_id for r in result] == [a.note_id, b.note_id]
    assert all(not r.tags for r in result)


def test_batches_capped_by_quality():
    plan = _plan(*[_note(f"N{i}") for i in range(7)])
    sizes: list[int] = []
    client = _FakeClient(lambda n, p: _all_items(p))
    annotate_notes(plan, [], {}, client, _status(), set(), lambda ids, an: sizes.append(len(ids)))
    assert sizes == [6, 1] and len(client.prompts) == 2


def test_normalize_tags():
    assert normalize_tags(["#Python", "python", "Data Science", "2024", "a/b", ""]) == [
        "python", "data-science", "a/b",
    ]
    assert len(normalize_tags([f"t{i}x" for i in range(20)])) == 6