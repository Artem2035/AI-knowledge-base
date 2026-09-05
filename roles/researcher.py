from __future__ import annotations

import logging

from llm.base import LLMClient
from gemini.schemas import SourceSelectionOutput
from storage.models import Plan, SourceCandidate, TaskStatus
from tools.web_fetch import fetch_clean_text
from tools.web_search import deduplicate_by_url, search_web
from llm.chunking import split_items_into_batches

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
    """Отбор релевантных источников. Кандидаты делятся на батчи под
    доступный TPM-бюджет клиента (см. llm/chunking.py) — так мы не теряем
    хвост списка при авто-обрезке промпта, как это было раньше при
    большом количестве кандидатов. Каждый батч — отдельный вызов
    generate_structured; индекс в ответе локальный для батча и
    резолвится обратно через сам объект-кандидат (без глобального offset,
    т.к. каждый батч — это свой list[SourceCandidate])."""
    if not candidates:
        return []

    static_overhead = (
        "Список кандидатов-источников (индекс в квадратных скобках):\n\n\n\n"
        "Верни для каждого источника, который стоит оставить, его индекс, "
        "relevance_score и keep=true. Источники с keep=false можно не включать в ответ."
    )

    def _render(c: SourceCandidate) -> str:
        return f"[0] Подтема: {c.subtopic}\nЗаголовок: {c.title}\nСниппет: {c.snippet}\nURL: {c.url}\n"

    batches = split_items_into_batches(
        candidates,
        client=client,
        system_instruction=SYSTEM_INSTRUCTION,
        response_model=SourceSelectionOutput,
        render_item=_render,
        static_overhead_text=static_overhead,
    )

    kept: list[SourceCandidate] = []
    for batch in batches:
        listing = "\n".join(
            f"[{i}] Подтема: {c.subtopic}\nЗаголовок: {c.title}\nСниппет: {c.snippet}\nURL: {c.url}\n"
            for i, c in enumerate(batch)
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
        for item in output.items:
            if item.keep and 0 <= item.index < len(batch):
                cand = batch[item.index]
                cand.relevance_score = item.relevance_score
                kept.append(cand)

    selected: list[SourceCandidate] = []
    by_subtopic_count: dict[str, int] = {}
    for cand in sorted(kept, key=lambda c: c.relevance_score, reverse=True):
        count = by_subtopic_count.get(cand.subtopic, 0)
        if count >= max_per_subtopic:
            continue
        cand.selected = True
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
