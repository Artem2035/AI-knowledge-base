from __future__ import annotations

from pydantic import BaseModel

from llm.router import RoleRoutingLLMClient
from storage.models import TaskStatus


class _Out(BaseModel):
    value: str


class _FakeClient:
    """Мок LLMClient (см. llm/base.py::LLMClient Protocol) — просто
    помечает, каким тегом он был вызван, и по каким ролям."""

    def __init__(self, tag: str):
        self.tag = tag
        self.calls: list[str] = []

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        self.calls.append(role)
        return response_model(value=self.tag)


def test_router_sends_mapped_role_to_specific_client():
    """Регрессия для основного сценария: synthesizer_write должен уходить
    именно на writing-клиент (Gemma), а не на дефолтный (Nemotron)."""
    planning = _FakeClient("nemotron")
    writing = _FakeClient("gemma")
    router = RoleRoutingLLMClient(planning, {"synthesizer_write": writing})
    status = TaskStatus(task_id="t1")

    out = router.generate_structured(
        role="synthesizer_write", prompt="p", response_model=_Out, status=status,
    )

    assert out.value == "gemma"
    assert writing.calls == ["synthesizer_write"]
    assert planning.calls == []


def test_router_falls_back_to_default_for_unmapped_role():
    """outline_planner/elaborator/critic/vault_dedup/folder_assignment —
    всё, что не 'synthesizer_write', должно уходить на дефолтный
    (planning) клиент."""
    planning = _FakeClient("nemotron")
    writing = _FakeClient("gemma")
    router = RoleRoutingLLMClient(planning, {"synthesizer_write": writing})
    status = TaskStatus(task_id="t2")

    for role in ("outline_planner", "elaborator", "critic", "vault_dedup", "folder_assignment"):
        out = router.generate_structured(
            role=role, prompt="p", response_model=_Out, status=status,
        )
        assert out.value == "nemotron"

    assert planning.calls == ["outline_planner", "elaborator", "critic", "vault_dedup", "folder_assignment"]
    assert writing.calls == []


def test_router_with_empty_role_map_always_uses_default():
    planning = _FakeClient("nemotron")
    router = RoleRoutingLLMClient(planning)
    status = TaskStatus(task_id="t3")

    router.generate_structured(role="critic", prompt="p", response_model=_Out, status=status)

    assert planning.calls == ["critic"]


def test_router_available_prompt_budget_tokens_delegates_to_default_when_present():
    class _WithBudget(_FakeClient):
        def available_prompt_budget_tokens(self, system_instruction, response_model):
            return 4242

    router = RoleRoutingLLMClient(_WithBudget("nemotron"))
    assert router.available_prompt_budget_tokens("sys", _Out) == 4242


def test_router_available_prompt_budget_tokens_returns_none_when_default_lacks_it():
    router = RoleRoutingLLMClient(_FakeClient("nemotron"))
    assert router.available_prompt_budget_tokens("sys", _Out) is None