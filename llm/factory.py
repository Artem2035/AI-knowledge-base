"""
Единственная точка выбора активного LLM-провайдера. Orchestrator вызывает
create_llm_client(...) один раз при старте — дальше вся система работает с
объектом, реализующим generate_structured(...), не зная, Gemini это или Groq.

Чтобы добавить третьего провайдера (например, GitHub Models) — нужно
реализовать тот же интерфейс в llm/<provider>_client.py и добавить один
elif сюда. roles/*, orchestrator/state_machine.py трогать не нужно.
"""
from __future__ import annotations

from config.settings import Settings
from orchestrator.budget import GeminiBudget


def create_llm_client(settings: Settings, budget: GeminiBudget):
    if settings.llm_provider == "gemini":
        from gemini.client import GeminiClient

        return GeminiClient(settings=settings, budget=budget)

    if settings.llm_provider == "groq":
        from llm.groq_client import GroqClient

        return GroqClient(settings=settings, budget=budget)

    raise ValueError(
        f"Неизвестный LLM_PROVIDER: {settings.llm_provider!r}. "
        "Допустимые значения: 'gemini', 'groq'."
    )


def budget_limits_for_provider(settings: Settings) -> tuple[int, int]:
    """Возвращает (rpm_soft_limit, rpd_soft_limit) для активного провайдера."""
    if settings.llm_provider == "groq":
        return settings.groq_rpm_soft_limit, settings.groq_rpd_soft_limit
    return settings.gemini_rpm_soft_limit, settings.gemini_rpd_soft_limit
