"""
Skill "web search" (Researcher). Только бесплатный DuckDuckGo через ddgs,
без ключей, без платных провайдеров, никакого fallback на Tavily/SerpAPI/etc.
"""
from __future__ import annotations

import logging

from storage.models import SourceCandidate

logger = logging.getLogger(__name__)


def search_web(query: str, max_results: int = 6, subtopic: str = "") -> list[SourceCandidate]:
    """
    Возвращает сырые кандидаты источников. Ранжирование/отбор релевантности —
    отдельный шаг (роль Researcher, может использовать Gemini), здесь только
    сбор сырых результатов.
    """
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError(
            "Библиотека 'ddgs' не установлена. Установите: pip install ddgs"
        ) from exc

    results: list[SourceCandidate] = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(
                    SourceCandidate(
                        url=r.get("href", ""),
                        title=r.get("title", ""),
                        snippet=r.get("body", ""),
                        subtopic=subtopic,
                    )
                )
    except Exception as exc:
        logger.warning("Веб-поиск не удался для запроса '%s': %s", query, exc)
    return results


def deduplicate_by_url(candidates: list[SourceCandidate]) -> list[SourceCandidate]:
    seen: set[str] = set()
    unique = []
    for c in candidates:
        norm = c.url.rstrip("/").lower()
        if norm and norm not in seen:
            seen.add(norm)
            unique.append(c)
    return unique
