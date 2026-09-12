from __future__ import annotations

import logging

from llm.base import LLMClient
from llm.chunking import split_items_into_batches
from gemini.prompts.elaborator import SYSTEM_INSTRUCTION
from gemini.schemas import ElaborationOutput
from storage.models import Evidence, Plan, Subtopic, TaskStatus

logger = logging.getLogger(__name__)

# Маркер source_id для Evidence, сгенерированного без внешнего источника —
# используется дальше (roles/synthesizer_writer.py, tools/markdown_tools.py)
# как сигнал пометить итоговую заметку source: model-knowledge во
# frontmatter, а не как ссылка на реальный SourceCandidate.
MODEL_KNOWLEDGE_SOURCE_ID = "model_knowledge"


def elaborate_subtopics(
    plan: Plan,
    client: LLMClient,
    status: TaskStatus,
    already_done_subtopic_titles: set[str],
    on_batch_done,
) -> None:
    """
    Аналог roles/extractor_critic.py::extract_evidence_from_sources, но БЕЗ
    входного текста источников: раскрывает подтемы плана из знаний модели.
    Батчинг подтем в один вызов — тем же механизмом (llm/chunking.py), что
    и батчинг единиц текста в extractor_critic, просто "единица" здесь —
    целая подтема (title+description), а не чанк чужого текста.

    on_batch_done(subtopic_titles: list[str], new_evidence: list[Evidence])
    — вызывается ПОСЛЕ КАЖДОГО батча; вызывающий код (orchestrator) обязан
    сохранить subtopic_titles в чекпоинт немедленно, иначе resume потеряет
    прогресс (тот же контракт, что у extract_evidence_from_sources).
    """
    remaining = [s for s in plan.subtopics if s.title not in already_done_subtopic_titles]
    if not remaining:
        return

    static_overhead = (
        f"Тема исследования: {plan.topic_title}\n\n"
        "Раскрой КАЖДУЮ из перечисленных подтем по отдельности. Для каждого "
        "факта укажи subtopic_index — номер подтемы в квадратных скобках, "
        "к которой он относится."
    )

    batches = split_items_into_batches(
        remaining,
        client=client,
        system_instruction=SYSTEM_INSTRUCTION,
        response_model=ElaborationOutput,
        render_item=lambda s: f"{s.title}\n{s.description}",
        static_overhead_text=static_overhead,
    )

    for batch in batches:
        evidence = _elaborate_batch(batch, plan, client, status)
        on_batch_done([s.title for s in batch], evidence)


def _elaborate_batch(
    subtopics: list[Subtopic], plan: Plan, client: LLMClient, status: TaskStatus
) -> list[Evidence]:
    if not subtopics:
        return []

    listing = "\n\n".join(
        f"=== Подтема [{i}]: {s.title} ===\n{s.description or '(описание не задано)'}"
        for i, s in enumerate(subtopics)
    )
    prompt = (
        f"Тема исследования: {plan.topic_title}\n\n"
        f"Подтемы для раскрытия:\n\n{listing}\n\n"
        "Раскрой КАЖДУЮ подтему выше своими знаниями. Обязательно укажи "
        "subtopic_index для каждого факта — номер подтемы в квадратных "
        "скобках."
    )

    output: ElaborationOutput = client.generate_structured(
        role="elaborator",
        prompt=prompt,
        response_model=ElaborationOutput,
        status=status,
        system_instruction=SYSTEM_INSTRUCTION,
    )

    evidence_list: list[Evidence] = []
    for item in output.evidence:
        if 0 <= item.subtopic_index < len(subtopics):
            concept = subtopics[item.subtopic_index].title
        else:
            logger.warning(
                "Elaborator вернул subtopic_index=%d вне диапазона [0, %d) — "
                "приписываем факт первой подтеме батча.",
                item.subtopic_index, len(subtopics),
            )
            concept = subtopics[0].title if subtopics else item.concept
        evidence_list.append(
            Evidence(
                concept=concept,
                statement=item.statement,
                source_id=MODEL_KNOWLEDGE_SOURCE_ID,
                confidence=item.confidence,
                is_definition=item.is_definition,
                critic_note=item.critic_note,
            )
        )
    return evidence_list


def elaborate_subtopics_sync(plan: Plan, client: LLMClient, status: TaskStatus) -> list[Evidence]:
    """Совместимость с прежним стилем вызова без чекпоинтинга (используется
    в тестах и там, где resume не нужен) — аналог
    roles/extractor_critic.py::extract_evidence_from_source."""
    collected: list[Evidence] = []

    def _collect(_titles: list[str], new_evidence: list[Evidence]) -> None:
        collected.extend(new_evidence)

    elaborate_subtopics(
        plan, client, status, already_done_subtopic_titles=set(), on_batch_done=_collect
    )
    return collected