from __future__ import annotations

from llm.schemas import ElaborationItem, ElaborationOutput
from roles.elaborator import (
    MODEL_KNOWLEDGE_SOURCE_ID,
    build_elaboration_units,
    elaborate_outline,
    elaborate_outline_sync,
)
from storage.models import OutlineNote, OutlineSubpoint, Plan, TaskStatus


class _FakeClient:
    """Мок LLMClient (см. llm/base.py::LLMClient Protocol) — возвращает
    заранее заданные ElaborationOutput по одному на каждый вызов батча."""

    def __init__(self, outputs: list[ElaborationOutput]):
        self._outputs = list(outputs)
        self.calls = 0

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        self.calls += 1
        return self._outputs.pop(0)


def _plan(subpoints_per_note: list[int]) -> Plan:
    """Строит Plan с заданным числом заметок и подпунктов в каждой."""
    notes = []
    for note_idx, n_subpoints in enumerate(subpoints_per_note):
        subpoints = [
            OutlineSubpoint(heading=f"Раздел{note_idx}.{i}", covers=f"Раскрыть аспект {i}")
            for i in range(n_subpoints)
        ]
        notes.append(OutlineNote(title=f"Заметка{note_idx}", subpoints=subpoints))
    return Plan(task_id="t", topic_title="RAG", notes=notes)


def test_build_elaboration_units_flattens_notes_and_subpoints():
    plan = _plan([2, 3])
    units = build_elaboration_units(plan, already_done_subpoint_ids=set())

    assert len(units) == 5
    assert units[0].note.title == "Заметка0"
    assert units[2].note.title == "Заметка1"


def test_build_elaboration_units_skips_already_done():
    plan = _plan([2])
    done_id = plan.notes[0].subpoints[0].subpoint_id
    units = build_elaboration_units(plan, already_done_subpoint_ids={done_id})

    assert len(units) == 1
    assert units[0].subpoint.subpoint_id != done_id


def test_elaborate_outline_maps_unit_index_to_note_and_subpoint():
    plan = _plan([2])
    sp0, sp1 = plan.notes[0].subpoints[0], plan.notes[0].subpoints[1]

    output = ElaborationOutput(
        evidence=[
            ElaborationItem(statement="Факт про раздел0", confidence=0.9, unit_index=0),
            ElaborationItem(statement="Факт про раздел1", confidence=0.8, unit_index=1),
        ]
    )
    client = _FakeClient([output])
    status = TaskStatus(task_id="t1")

    evidence = elaborate_outline_sync(plan, client, status, max_subpoints_per_batch=6)

    assert len(evidence) == 2
    assert evidence[0].note_id == plan.notes[0].note_id
    assert evidence[0].subpoint_id == sp0.subpoint_id
    assert evidence[1].subpoint_id == sp1.subpoint_id
    assert all(e.source_id == MODEL_KNOWLEDGE_SOURCE_ID for e in evidence)
    assert all(e.verified is False for e in evidence)


def test_elaborate_outline_out_of_range_index_falls_back_to_first_unit_of_batch():
    plan = _plan([2])
    output = ElaborationOutput(
        evidence=[
            ElaborationItem(statement="Факт с плохим индексом", confidence=0.5, unit_index=99),
        ]
    )
    client = _FakeClient([output])
    status = TaskStatus(task_id="t2")

    evidence = elaborate_outline_sync(plan, client, status, max_subpoints_per_batch=6)

    assert len(evidence) == 1
    assert evidence[0].subpoint_id == plan.notes[0].subpoints[0].subpoint_id


def test_elaborate_outline_skips_already_done_on_resume():
    client = _FakeClient([])  # если бы был вызов — упало бы на pop из пустого списка
    status = TaskStatus(task_id="t3")

    plan = _plan([1, 1])
    all_ids = {sp.subpoint_id for note in plan.notes for sp in note.subpoints}
    collected: list[list[str]] = []

    def _on_batch_done(ids, new_evidence):
        collected.append(ids)

    elaborate_outline(
        plan, client, status,
        already_done_subpoint_ids=all_ids,
        on_batch_done=_on_batch_done,
        max_subpoints_per_batch=6,
    )

    assert client.calls == 0
    assert collected == []


def test_elaborate_outline_partial_resume_only_processes_remaining():
    plan = _plan([2])
    remaining_sp = plan.notes[0].subpoints[1]
    done_id = plan.notes[0].subpoints[0].subpoint_id

    output = ElaborationOutput(
        evidence=[
            ElaborationItem(statement="Факт про оставшийся раздел", confidence=0.7, unit_index=0),
        ]
    )
    client = _FakeClient([output])
    status = TaskStatus(task_id="t4")

    collected: list[list[str]] = []

    def _on_batch_done(ids, new_evidence):
        collected.append(ids)

    elaborate_outline(
        plan, client, status,
        already_done_subpoint_ids={done_id},
        on_batch_done=_on_batch_done,
        max_subpoints_per_batch=6,
    )

    assert client.calls == 1
    assert collected == [[remaining_sp.subpoint_id]]


def test_elaborate_outline_splits_large_note_by_quality_cap():
    """Регрессия для батчинга по качеству (llm/chunking.py::
    batch_for_quality_and_budget): заметка с 8 подпунктами при
    max_subpoints_per_batch=3 должна уйти в 3 отдельных вызова
    (3+3+2), а не в один вызов со всеми 8 сразу — иначе модель даёт
    поверхностные однострочные ответы на каждый подпункт."""
    plan = _plan([8])
    outputs = [
        ElaborationOutput(
            evidence=[
                ElaborationItem(statement=f"Факт {i}", confidence=0.5, unit_index=i)
                for i in range(n)
            ]
        )
        for n in (3, 3, 2)
    ]
    client = _FakeClient(outputs)
    status = TaskStatus(task_id="t5")

    batch_sizes: list[int] = []

    def _on_batch_done(ids, new_evidence):
        batch_sizes.append(len(ids))

    elaborate_outline(
        plan, client, status,
        already_done_subpoint_ids=set(),
        on_batch_done=_on_batch_done,
        max_subpoints_per_batch=3,
    )

    assert client.calls == 3
    assert batch_sizes == [3, 3, 2]


def test_elaborate_outline_two_notes_not_merged_across_boundary():
    """Заметка-1 (5 подпунктов) при max_subpoints_per_batch=6 не должна
    сливаться с заметкой-2 в одном батче, если после округления по
    заметке-1 не остаётся места — но если остаётся (5 < 6), проверяем,
    что порядок сохраняется и превышение потолка per-батч не происходит."""
    plan = _plan([5, 3])
    outputs = [
        ElaborationOutput(evidence=[ElaborationItem(statement=f"Ф{i}", confidence=0.5, unit_index=i) for i in range(n)])
        for n in (6, 2)
    ]
    client = _FakeClient(outputs)
    status = TaskStatus(task_id="t6")

    batch_sizes: list[int] = []

    def _on_batch_done(ids, new_evidence):
        batch_sizes.append(len(ids))

    elaborate_outline(
        plan, client, status,
        already_done_subpoint_ids=set(),
        on_batch_done=_on_batch_done,
        max_subpoints_per_batch=6,
    )

    assert client.calls == 2
    assert batch_sizes == [6, 2]  # 5+3=8 элементов -> [0:6], [6:8]