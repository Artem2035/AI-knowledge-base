from __future__ import annotations

import logging

from llm.base import LLMClient
from gemini.schemas import SourceSelectionOutput
from storage.models import Plan, SourceCandidate, TaskStatus
from tools.web_fetch import fetch_clean_text
from tools.web_search import deduplicate_by_url, search_web

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = (
    "Ты — Researcher. Тебе даны подтема и список сырых результатов "
    "веб-поиска (заголовок + сниппет + URL). Отбери только релевантные, "
    "качественные источники: предпочитай официальную документацию, "
    "научные статьи, авторитетные технические ресурсы; исключай явный "
    "спам, рекламные страницы, дубликаты по содержанию, форумы низкого "
    "качества. Оцени relevance_score от 0 до 1."
)


def collect_raw_candidates(
    plan: Plan, max_results_per_query: int, max_sources_per_subtopic: int
) -> list[SourceCandidate]:
    """Чистый код, без LLM: сбор сырых результатов поиска по всем подтемам."""
    all_candidates: list[SourceCandidate] = []
    for subtopic in plan.subtopics:
        subtopic_candidates: list[SourceCandidate] = []
        for query in subtopic.search_queries:
            results = search_web(query, max_results=max_results_per_query, subtopic=subtopic.title)
            subtopic_candidates.extend(results)
        subtopic_candidates = deduplicate_by_url(subtopic_candidates)[:max_sources_per_subtopic * 2]
        all_candidates.extend(subtopic_candidates)
    return all_candidates


def select_relevant_sources(
    candidates: list[SourceCandidate],
    client: LLMClient,
    status: TaskStatus,
    max_per_subtopic: int,
) -> list[SourceCandidate]:
    """1 Gemini-вызов на отбор релевантных источников из уже собранных кандидатов."""
    if not candidates:
        return []

    listing = "\n".join(
        f"[{i}] Подтема: {c.subtopic}\nЗаголовок: {c.title}\nСниппет: {c.snippet}\nURL: {c.url}\n"
        for i, c in enumerate(candidates)
    )
    prompt = (
        f"Список кандидатов-источников (индекс в квадратных скобках):\n\n{listing}\n\n"
        "Верни для каждого источника, который стоит оставить, его индекс, "
        "relevance_score и keep=true. Источники с keep=false можно не включать в ответ."
    )
    output: SourceSelectionOutput = client.generate_structured(
        role="researcher_selection",
        prompt=prompt,
        response_model=SourceSelectionOutput,
        status=status,
        system_instruction=SYSTEM_INSTRUCTION,
    )

    selected: list[SourceCandidate] = []
    by_subtopic_count: dict[str, int] = {}
    kept_items = sorted(
        (item for item in output.items if item.keep and 0 <= item.index < len(candidates)),
        key=lambda it: it.relevance_score,
        reverse=True,
    )
    for item in kept_items:
        cand = candidates[item.index]
        count = by_subtopic_count.get(cand.subtopic, 0)
        if count >= max_per_subtopic:
            continue
        cand.selected = True
        cand.relevance_score = item.relevance_score
        selected.append(cand)
        by_subtopic_count[cand.subtopic] = count + 1

    return selected


def fetch_selected_sources(selected: list[SourceCandidate]) -> list[SourceCandidate]:
    """Чистый код: скачивание и очистка текста источников."""
    for cand in selected:
        text, error = fetch_clean_text(cand.url)
        cand.fetched_text = text
        cand.fetch_error = error
        if error:
            logger.info("Не удалось получить контент %s: %s", cand.url, error)
    return [c for c in selected if c.fetched_text]
