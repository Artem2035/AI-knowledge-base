from __future__ import annotations

from llm.schemas import OutlineNoteOutput, OutlinePlanOutput, OutlineSubpointOutput
from roles.outline_planner import build_plan
from storage.models import Task, TaskStatus, normalize_kind


class _FakeClient:
    def __init__(self, output: OutlinePlanOutput):
        self._output = output

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        assert role == "outline_planner"
        return self._output


def _output(domain: str, kinds: list[str]) -> OutlinePlanOutput:
    return OutlinePlanOutput(
        topic_title="Тема", domain=domain,
        notes=[OutlineNoteOutput(
            title="Заметка",
            subpoints=[OutlineSubpointOutput(heading=f"Р{i}", covers="...", kind=k) for i, k in enumerate(kinds)],
        )],
    )


def test_build_plan_keeps_domain_and_valid_kinds():
    client = _FakeClient(_output("technical", ["definition", "example"]))
    plan = build_plan(Task(raw_query="q"), client, TaskStatus(task_id="p1"))
    assert plan.domain == "technical"
    assert [sp.kind for sp in plan.notes[0].subpoints] == ["definition", "example"]


def test_build_plan_resets_kind_foreign_to_domain():
    client = _FakeClient(_output("humanities", ["context", "mechanism"]))  # mechanism — из technical
    plan = build_plan(Task(raw_query="q"), client, TaskStatus(task_id="p2"))
    assert [sp.kind for sp in plan.notes[0].subpoints] == ["context", "other"]


def test_normalize_kind_unknown_domain_gives_other():
    assert normalize_kind("definition", "unknown") == "other"


def test_old_subpoint_without_kind_loads_with_default():
    from storage.models import OutlineSubpoint
    assert OutlineSubpoint(heading="h", covers="c").kind == "other"