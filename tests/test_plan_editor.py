from __future__ import annotations

from cli import plan_editor
from storage.models import OutlineNote, OutlineSubpoint, Plan


def _plan() -> Plan:
    note = OutlineNote(title="N", subpoints=[
        OutlineSubpoint(heading="A", covers="a", kind="definition"),
        OutlineSubpoint(heading="B", covers="b", kind="example"),
    ])
    return Plan(task_id="t", topic_title="T", domain="technical", notes=[note])


def test_change_domain_resets_incompatible_kinds(monkeypatch):
    plan = _plan()
    monkeypatch.setattr(plan_editor.typer, "prompt", lambda *a, **k: 2)  # humanities
    plan_editor._change_domain(plan)
    assert plan.domain == "humanities"
    assert [sp.kind for sp in plan.notes[0].subpoints] == ["other", "other"]


def test_change_domain_same_domain_keeps_kinds(monkeypatch):
    plan = _plan()
    monkeypatch.setattr(plan_editor.typer, "prompt", lambda *a, **k: 1)  # technical
    plan_editor._change_domain(plan)
    assert [sp.kind for sp in plan.notes[0].subpoints] == ["definition", "example"]


def test_prompt_kind_returns_selected(monkeypatch):
    monkeypatch.setattr(plan_editor.typer, "prompt", lambda *a, **k: 3)
    assert plan_editor._prompt_kind("technical") == "parameters"


def test_tree_shows_domain_and_kind():
    root = plan_editor.build_plan_tree(_plan())
    assert "technical" in str(root.label)
    assert "(definition)" in str(root.children[0].children[0].label)