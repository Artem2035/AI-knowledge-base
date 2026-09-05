from __future__ import annotations

from llm.base import LLMClient
from gemini.schemas import DedupDecisionOutput
from retrieval.search import RetrievalHit, VaultSearcher
from storage.models import Evidence, ExistingNote, Plan, TaskStatus
from tools.dedup import classify_similarity

SYSTEM_INSTRUCTION = (
    "Ты — Vault Analyst. Тебе даны концепция/факт из нового исследования и "
    "существующая заметка из личной базы знаний пользователя (её заголовок, "
    "теги и краткое содержание). Определи: это та же концепция (её стоит "
    "переиспользовать/дополнить), или это разные, самостоятельные концепции "
    "(нужна отдельная новая заметка). Главное правило: не плодить дубликаты — "
    "если сомневаешься между 'reuse' и 'distinct', выбирай 'extend' "
    "(дополнить существующую заметку новым материалом)."
)


def find_existing_notes_for_plan(
    plan: Plan,
    evidence: list[Evidence],
    searcher: VaultSearcher,
    client: LLMClient,
    status: TaskStatus,
    high_threshold: float,
    low_threshold: float,
    top_k: int = 3,
) -> list[ExistingNote]:
    """
    Для каждой концепции из плана — локальный поиск похожих существующих
    заметок. LLM вызывается только для кандидатов в "серой зоне" схожести.
    """
    results: dict[str, ExistingNote] = {}  # path -> ExistingNote (дедуп по пути)

    concepts = {s.title for s in plan.subtopics} | {e.concept for e in evidence}

    for concept in concepts:
        hits: list[RetrievalHit] = searcher.search(concept, top_k=top_k)
        for hit in hits:
            score = hit.combined_score
            verdict = classify_similarity(score, high_threshold, low_threshold)

            if verdict == "distinct":
                continue  # не заслуживает даже упоминания в отчёте

            if hit.path in results and results[hit.path].similarity_score >= score:
                continue  # уже есть более сильное совпадение с этой заметкой

            note = ExistingNote(
                path=hit.path,
                title=hit.title,
                frontmatter=hit.frontmatter,
                tags=hit.tags,
                summary=hit.summary,
                similarity_score=score,
                matched_concept=concept,
            )

            if verdict == "duplicate":
                note.decision = "reuse"
            else:  # ambiguous -> 1 маленький Gemini-вызов
                note.decision = _resolve_ambiguous(concept, note, client, status)

            results[hit.path] = note

    return list(results.values())


def _resolve_ambiguous(
    concept: str, note: ExistingNote, client: LLMClient, status: TaskStatus
) -> str:
    prompt = (
        f"Новая концепция из исследования: {concept!r}\n\n"
        f"Существующая заметка:\n"
        f"Заголовок: {note.title}\n"
        f"Теги: {', '.join(note.tags)}\n"
        f"Краткое содержание: {note.summary}\n\n"
        "Это та же концепция?"
    )
    output: DedupDecisionOutput = client.generate_structured(
        role="vault_dedup",
        prompt=prompt,
        response_model=DedupDecisionOutput,
        status=status,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    return output.decision
