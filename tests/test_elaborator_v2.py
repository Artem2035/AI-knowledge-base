from __future__ import annotations

from llm.common import LLMSchemaError
from llm.prompts.elaborator_v2 import get_system_instruction
from llm.schemas import SectionBatchOutput, SectionItem
from roles.elaborator_v2 import elaborate_outline, elaborate_outline_sync
from storage.models import OutlineNote, OutlineSubpoint, Plan, TaskStatus
from tools.note_assembly import PLACEHOLDER_MARKDOWN


class _FakeClient:
    """Мок LLMClient: handler(номер_вызова, prompt) -> SectionBatchOutput или raise."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, str | None]] = []

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        assert role == "elaborator"
        self.calls.append((prompt, system_instruction))
        return self.handler(len(self.calls), prompt)


def _out(*items) -> SectionBatchOutput:
    return SectionBatchOutput(sections=[
        SectionItem(unit_index=i, markdown=m, needs_check=nc) for i, m, nc in items
    ])


def _plan(n: int = 2, domain: str = "technical") -> Plan:
    note = OutlineNote(title="Заметка", subpoints=[
        OutlineSubpoint(heading=f"Р{i}", covers=f"Раскрыть {i}", kind="definition") for i in range(n)
    ])
    return Plan(task_id="t", topic_title="Тема", domain=domain, notes=[note])


def _status() -> TaskStatus:
    return TaskStatus(task_id="e")


def test_maps_unit_index_and_passes_domain_instruction():
    plan = _plan(2)
    client = _FakeClient(lambda n, p: _out((0, "Текст A", False), (1, "Текст B", True)))
    sections = elaborate_outline_sync(plan, client, _status())

    sp = plan.notes[0].subpoints
    assert [s.subpoint_id for s in sections] == [sp[0].subpoint_id, sp[1].subpoint_id]
    assert sections[0].markdown == "Текст A" and sections[1].needs_check is True
    prompt, system = client.calls[0]
    assert system == get_system_instruction("technical")
    assert "Р1" in prompt and "не повторяй" in prompt


def test_out_of_range_index_dropped_and_missing_retried_once():
    def handler(n, prompt):
        return _out((99, "мусор", False), (0, "A", False)) if n == 1 else _out((0, "B", False))

    client = _FakeClient(handler)
    sections = elaborate_outline_sync(_plan(2), client, _status())
    assert [s.markdown for s in sections] == ["A", "B"]
    assert len(client.calls) == 2


def test_missing_after_retry_becomes_placeholder_with_needs_check():
    client = _FakeClient(lambda n, p: _out((0, "A", False)) if n == 1 else _out())
    plan = _plan(2)
    ids: list[list[str]] = []
    elaborate_outline(plan, client, _status(), set(), lambda i, s: ids.append(i), 3)

    sections = elaborate_outline_sync(plan, _FakeClient(lambda n, p: _out((0, "A", False)) if n == 1 else _out()), _status())
    assert sections[1].markdown == PLACEHOLDER_MARKDOWN and sections[1].needs_check
    assert len(ids[0]) == 2  # в чекпоинт уходят ВСЕ подпункты батча, включая placeholder


def test_unbalanced_fence_retried_then_closed_with_needs_check():
    def handler(n, prompt):
        if n == 1:
            return _out((0, "```python\nx = 1", False), (1, "ok", False))
        return _out((0, "```python\ny = 2", False))

    client = _FakeClient(handler)
    sections = elaborate_outline_sync(_plan(2), client, _status())
    assert sections[0].markdown.endswith("```") and sections[0].needs_check is True
    assert sections[1].markdown == "ok" and sections[1].needs_check is False
    assert len(client.calls) == 2


def test_schema_error_splits_batch_in_half():
    def handler(n, prompt):
        if "Раздел [1]" in prompt:
            raise LLMSchemaError("битый JSON")
        return _out((0, "ok", False))

    client = _FakeClient(handler)
    sections = elaborate_outline_sync(_plan(2), client, _status())
    assert [s.markdown for s in sections] == ["ok", "ok"]
    assert len(client.calls) == 3  # один неудачный на 2 + два по одному


def test_single_unit_schema_error_gives_placeholder():
    def handler(n, prompt):
        raise LLMSchemaError("битый JSON")

    sections = elaborate_outline_sync(_plan(1), _FakeClient(handler), _status())
    assert sections[0].markdown == PLACEHOLDER_MARKDOWN and sections[0].needs_check


def test_resume_skips_done_units():
    plan = _plan(2)
    client = _FakeClient(lambda n, p: _out((0, "B", False)))
    done = {plan.notes[0].subpoints[0].subpoint_id}
    got: list[list[str]] = []
    elaborate_outline(plan, client, _status(), done, lambda ids, s: got.append(ids), 3)
    assert got == [[plan.notes[0].subpoints[1].subpoint_id]]
    assert len(client.calls) == 1


def test_batch_split_by_quality_cap():
    client = _FakeClient(lambda n, p: _out(*[(i, f"t{i}", False) for i in range(3 if n == 1 else 1)]))
    elaborate_outline_sync(_plan(4), client, _status(), max_subpoints_per_batch=3)
    assert len(client.calls) == 2  # 3 + 1


def test_system_instruction_static_and_domain_specific():
    assert get_system_instruction("technical") is get_system_instruction("technical")
    assert "needs_check" in get_system_instruction("technical")
    assert "direct quotes" in get_system_instruction("humanities")
    assert "direct quotes" not in get_system_instruction("technical")
    assert "- [ ] item" in get_system_instruction("life_management")
    assert get_system_instruction("неизвестный") == get_system_instruction("technical")