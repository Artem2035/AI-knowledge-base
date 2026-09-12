from __future__ import annotations

import logging

from llm.base import LLMClient
from gemini.prompts.critic import SYSTEM_INSTRUCTION
from gemini.schemas import CriticVerdictOutput, NotePlanItem
from roles import synthesizer_writer
from storage.models import DraftNote, Evidence, SourceCandidate, TaskStatus

logger = logging.getLogger(__name__)


def review_draft(
    draft: DraftNote,
    assigned_evidence: list[Evidence],
    client: LLMClient,
    status: TaskStatus,
) -> CriticVerdictOutput:
    """Один вызов ревью уже написанной заметки. Работает на итоговом тексте
    (body_md для create, append_section для update), а не на сыром evidence
    — см. gemini/prompts/critic.py про то, чему критик может и не может
    доверять без внешних источников."""
    text = draft.append_section or draft.body_md
    evidence_listing = "\n".join(
        f"- [{e.concept}] {e.statement}" for e in assigned_evidence
    ) or "(факты не были назначены явно — заметка опиралась на заголовок и общий контекст)"

    prompt = (
        f"Заголовок заметки: {draft.title}\n\n"
        f"Текст заметки:\n{text}\n\n"
        f"Факты, которые должны быть отражены в заметке:\n{evidence_listing}\n\n"
        "Оцени заметку и вынеси вердикт согласно роли."
    )

    return client.generate_structured(
        role="critic",
        prompt=prompt,
        response_model=CriticVerdictOutput,
        status=status,
        system_instruction=SYSTEM_INSTRUCTION,
    )


def run_critic_cycle(
    item: NotePlanItem,
    evidence: list[Evidence],
    known_titles: list[str],
    title_map: dict[str, str],
    sources: list[SourceCandidate],
    client: LLMClient,
    status: TaskStatus,
    default_folder: str,
    max_rounds: int,
    mark_source: str | None = None,
) -> DraftNote:
    """
    Пишет заметку (synthesizer_writer.write_note), затем прогоняет её через
    Critic и, если тот просит переписать, переписывает — НЕ БОЛЕЕ
    max_rounds раз (bounded retry, см. config/settings.py::max_critic_rounds
    и ТЗ п.31 — 'bounded retry loops': LLM не должен проверять то, что
    можно надёжно проверить программно, а там, где LLM всё же нужен для
    проверки, цикл должен иметь жёсткий потолок, а не крутиться до 'ok').

    Если max_rounds <= 0 — критик вообще не вызывается (эквивалентно
    прежнему поведению без критика).

    Если после исчерпания max_rounds последний вердикт всё ещё 'rewrite' —
    заметка возвращается КАК ЕСТЬ (без дополнительного ре-ревью — это
    сознательно, чтобы не тратить лишний вызов на подтверждение), но с
    draft.needs_review=True, чтобы пользователь обратил на неё внимание в
    diff (staging/diff.py) перед approve.
    """
    draft = synthesizer_writer.write_note(
        item, evidence, known_titles, title_map, sources, client, status,
        default_folder, mark_source=mark_source,
    )

    if max_rounds <= 0:
        return draft

    assigned_evidence = [evidence[i] for i in item.evidence_indices if 0 <= i < len(evidence)]

    rounds = 0
    verdict: CriticVerdictOutput | None = None
    while rounds < max_rounds:
        verdict = review_draft(draft, assigned_evidence, client, status)
        if verdict.verdict == "ok":
            break
        rounds += 1
        logger.info(
            "Critic попросил переписать заметку «%s» (попытка %d/%d): %s",
            item.title, rounds, max_rounds, verdict.feedback,
        )
        draft = synthesizer_writer.write_note(
            item, evidence, known_titles, title_map, sources, client, status,
            default_folder,
            extra_instructions=verdict.feedback,
            mark_source=mark_source,
        )

    draft.critic_rounds = rounds
    draft.needs_review = rounds >= max_rounds and verdict is not None and verdict.verdict == "rewrite"
    return draft