from __future__ import annotations

from gemini.schemas import CriticVerdictOutput, DraftNoteOutput, NotePlanItem
from roles.critic import run_critic_cycle
from storage.models import Evidence, TaskStatus


class _FakeClient:
    """Мок LLMClient, различающий вызовы по role — synthesizer_write и
    critic должны получать разные заранее заданные ответы."""

    def __init__(self, write_outputs: list[DraftNoteOutput], critic_outputs: list[CriticVerdictOutput]):
        self.write_outputs = list(write_outputs)
        self.critic_outputs = list(critic_outputs)
        self.write_calls = 0
        self.critic_calls = 0

    def generate_structured(self, *, role, prompt, response_model, status, system_instruction=None):
        if role == "synthesizer_write":
            self.write_calls += 1
            return self.write_outputs.pop(0)
        if role == "critic":
            self.critic_calls += 1
            return self.critic_outputs.pop(0)
        raise AssertionError(f"неожиданная роль в вызове: {role!r}")


def _item() -> NotePlanItem:
    return NotePlanItem(title="RAG", action="create", folder="Знания", evidence_indices=[0])


def _evidence() -> list[Evidence]:
    return [Evidence(concept="RAG", statement="RAG объединяет retrieval и generation.", source_id="model_knowledge")]


def _draft_output(body: str = "Текст версии, достаточно длинный для теста ревью критика.") -> DraftNoteOutput:
    return DraftNoteOutput(action="create", title="RAG", body_md=body, tags=["rag"], links_out=[])


def test_critic_approves_on_first_try_no_rewrite():
    client = _FakeClient(
        write_outputs=[_draft_output()],
        critic_outputs=[CriticVerdictOutput(verdict="ok")],
    )
    status = TaskStatus(task_id="c1")

    draft = run_critic_cycle(
        _item(), _evidence(), known_titles=[], title_map={}, sources=[],
        client=client, status=status, default_folder="Знания", max_rounds=1,
    )

    assert client.write_calls == 1
    assert client.critic_calls == 1
    assert draft.critic_rounds == 0
    assert draft.needs_review is False


def test_critic_rewrite_once_within_bound_then_stops():
    client = _FakeClient(
        write_outputs=[_draft_output("Первая версия."), _draft_output("Вторая версия, переписанная.")],
        critic_outputs=[CriticVerdictOutput(verdict="rewrite", feedback="Слишком коротко, добавь примеры.")],
    )
    status = TaskStatus(task_id="c2")

    draft = run_critic_cycle(
        _item(), _evidence(), known_titles=[], title_map={}, sources=[],
        client=client, status=status, default_folder="Знания", max_rounds=1,
    )

    assert client.write_calls == 2  # исходная версия + одно переписывание
    # КРИТИЧНО для bounded retry: после исчерпания max_rounds система НЕ
    # делает финальное ре-ревью переписанной версии — иначе цикл не был бы
    # по-настоящему ограниченным по числу вызовов критика.
    assert client.critic_calls == 1
    assert draft.critic_rounds == 1
    assert draft.needs_review is True
    assert draft.body_md == "Вторая версия, переписанная."


def test_critic_feedback_is_passed_to_rewrite_prompt():
    client = _FakeClient(
        write_outputs=[_draft_output("Первая версия."), _draft_output("Вторая версия.")],
        critic_outputs=[CriticVerdictOutput(verdict="rewrite", feedback="УНИКАЛЬНАЯ_МЕТКА_ЗАМЕЧАНИЯ")],
    )
    status = TaskStatus(task_id="c3")

    original_generate = client.generate_structured
    captured_prompts: list[str] = []

    def _spy(*, role, prompt, response_model, status, system_instruction=None):
        if role == "synthesizer_write":
            captured_prompts.append(prompt)
        return original_generate(
            role=role, prompt=prompt, response_model=response_model,
            status=status, system_instruction=system_instruction,
        )

    client.generate_structured = _spy  # type: ignore[method-assign]

    run_critic_cycle(
        _item(), _evidence(), known_titles=[], title_map={}, sources=[],
        client=client, status=status, default_folder="Знания", max_rounds=1,
    )

    assert len(captured_prompts) == 2
    assert "УНИКАЛЬНАЯ_МЕТКА_ЗАМЕЧАНИЯ" not in captured_prompts[0]
    assert "УНИКАЛЬНАЯ_МЕТКА_ЗАМЕЧАНИЯ" in captured_prompts[1]


def test_critic_disabled_when_max_rounds_zero():
    client = _FakeClient(write_outputs=[_draft_output()], critic_outputs=[])
    status = TaskStatus(task_id="c4")

    draft = run_critic_cycle(
        _item(), _evidence(), known_titles=[], title_map={}, sources=[],
        client=client, status=status, default_folder="Знания", max_rounds=0,
    )

    assert client.write_calls == 1
    assert client.critic_calls == 0
    assert draft.critic_rounds == 0
    assert draft.needs_review is False


def test_critic_multi_round_bound_respected():
    """max_rounds=2: критик может попросить переписать дважды, но не
    больше — третьего вызова критика быть не должно, даже если бы он был
    доступен в списке ответов (здесь намеренно передан только 2, чтобы
    IndexError/pop-from-empty сам стал проверкой границы)."""
    client = _FakeClient(
        write_outputs=[_draft_output("v1"), _draft_output("v2"), _draft_output("v3")],
        critic_outputs=[
            CriticVerdictOutput(verdict="rewrite", feedback="замечание 1"),
            CriticVerdictOutput(verdict="rewrite", feedback="замечание 2"),
        ],
    )
    status = TaskStatus(task_id="c5")

    draft = run_critic_cycle(
        _item(), _evidence(), known_titles=[], title_map={}, sources=[],
        client=client, status=status, default_folder="Знания", max_rounds=2,
    )

    assert client.write_calls == 3
    assert client.critic_calls == 2
    assert draft.critic_rounds == 2
    assert draft.needs_review is True
    assert draft.body_md == "v3"