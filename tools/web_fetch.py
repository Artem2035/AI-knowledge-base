"""
Skill "URL fetching + page extraction" (Researcher). Бесплатно: httpx + trafilatura.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; ObsidianAIKB/0.1; personal-use-research-bot)"
MAX_CHARS_PER_SOURCE = 12000  # ограничиваем, чтобы не раздувать контекст для Gemini


def fetch_clean_text(url: str, timeout: float = 15.0) -> tuple[str | None, str | None]:
    """Возвращает (text, error). Если text is None — fetch_error непусто."""
    try:
        with httpx.Client(
            follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        return None, f"fetch_error: {exc}"

    try:
        import trafilatura

        extracted = trafilatura.extract(html, include_comments=False, include_tables=False)
    except Exception as exc:
        return None, f"extract_error: {exc}"

    if not extracted:
        return None, "extract_error: empty content"

    if len(extracted) > MAX_CHARS_PER_SOURCE:
        extracted = extracted[:MAX_CHARS_PER_SOURCE]

    return extracted, None
