from __future__ import annotations

from gemini.schemas import ElaborationItem, ElaborationOutput
from roles.elaborator import MODEL_KNOWLEDGE_SOURCE_ID, elaborate_subtopics, elaborate_subtopics_sync
from storage.models import Plan, Subtopic, TaskStatus


class _FakeClient:
    """Мок LLMClient (см. llm/base.py::LLMClient Protocol) — возвращает
    заранее заданные ElaborationOutput по одному на каждый вызов батча."""

    def __init__(self, outputs: list[ElaborationOutput]):
        self._outputs = list(outputs)
        self.calls = 0

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        self.calls += 1
        return self._outputs.pop(0)


def _plan(n: int = 2) -> Plan:
    return Plan(
        task_id="t",
        topic_title="RAG",
        subtopics=[Subtopic(title=f"Подтема{i}", description=f"Описание {i}") for i in range(n)],
    )


def test_elaborate_subtopics_maps_subtopic_index_to_concept():
    output = ElaborationOutput(
        evidence=[
            ElaborationItem(concept="ignored", statement="Факт про подтему0", confidence=0.9, subtopic_index=0),
            ElaborationItem(concept="ignored", statement="Факт про подтему1", confidence=0.8, subtopic_index=1),
        ]
    )
    client = _FakeClient([output])
    status = TaskStatus(task_id="t1")

    evidence = elaborate_subtopics_sync(_plan(2), client, status)

    assert len(evidence) == 2
    assert evidence[0].concept == "Подтема0"
    assert evidence[1].concept == "Подтема1"
    assert all(e.source_id == MODEL_KNOWLEDGE_SOURCE_ID for e in evidence)
    assert all(e.verified is False for e in evidence)


def test_elaborate_subtopics_out_of_range_index_falls_back_to_first():
    output = ElaborationOutput(
        evidence=[
            ElaborationItem(
                concept="ignored", statement="Факт с плохим индексом", confidence=0.5, subtopic_index=99
            ),
        ]
    )
    client = _FakeClient([output])
    status = TaskStatus(task_id="t2")

    evidence = elaborate_subtopics_sync(_plan(2), client, status)

    assert len(evidence) == 1
    assert evidence[0].concept == "Подтема0"


def test_elaborate_subtopics_skips_already_done_on_resume():
    client = _FakeClient([])  # если бы был вызов — упало бы на pop из пустого списка
    status = TaskStatus(task_id="t3")

    plan = _plan(2)
    collected: list[list[str]] = []

    def _on_batch_done(titles: list[str], new_evidence: list) -> None:
        collected.append(titles)

    elaborate_subtopics(
        plan, client, status,
        already_done_subtopic_titles={"Подтема0", "Подтема1"},
        on_batch_done=_on_batch_done,
    )

    assert client.calls == 0
    assert collected == []


def test_elaborate_subtopics_partial_resume_only_processes_remaining():
    output = ElaborationOutput(
        evidence=[
            ElaborationItem(concept="ignored", statement="Факт про оставшуюся подтему", confidence=0.7, subtopic_index=0),
        ]
    )
    client = _FakeClient([output])
    status = TaskStatus(task_id="t4")

    plan = _plan(2)
    collected: list[list[str]] = []

    def _on_batch_done(titles: list[str], new_evidence: list) -> None:
        collected.append(titles)

    elaborate_subtopics(
        plan, client, status,
        already_done_subtopic_titles={"Подтема0"},
        on_batch_done=_on_batch_done,
    )

    assert client.calls == 1
    assert collected == [["Подтема1"]]