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


def create_extraction_llm_client(settings: Settings, budget: GeminiBudget):
    """Отдельный клиент для роли extractor_critic. На Groq использует
    другую модель (settings.groq_extraction_model, по умолчанию
    groq/compound-mini, TPM=70000) вместо settings.groq_model — единственной
    роли, где регулярно не хватает TPM обычной модели из-за объёма текста
    источников. Настройки клонируются через model_copy(), чтобы не задевать
    остальные роли, использующие обычный create_llm_client().
    На Gemini отдельной "extraction"-модели с другим TPM нет смысла заводить
    — возвращается тот же клиент, что и для остальных ролей."""
    if settings.llm_provider != "groq":
        return create_llm_client(settings, budget)

    from llm.groq_client import GroqClient

    extraction_settings = settings.model_copy(
        update={
            "groq_model": settings.groq_extraction_model,
            "groq_tpm_limit": settings.groq_extraction_tpm_limit,
        }
    )
    return GroqClient(settings=extraction_settings, budget=budget)


def budget_limits_for_provider(settings: Settings) -> tuple[int, int]:
    """Возвращает (rpm_soft_limit, rpd_soft_limit) для активного провайдера
    (все роли КРОМЕ extraction)."""
    if settings.llm_provider == "groq":
        return settings.groq_rpm_soft_limit, settings.groq_rpd_soft_limit
    return settings.gemini_rpm_soft_limit, settings.gemini_rpd_soft_limit


def extraction_budget_limits(settings: Settings) -> tuple[int, int]:
    """То же самое, но для extraction-клиента — на Groq у него другой RPD
    (см. create_extraction_llm_client): compound-mini считается API отдельно
    от gpt-oss, поэтому лимиты не совпадают с budget_limits_for_provider()."""
    if settings.llm_provider == "groq":
        return settings.groq_rpm_soft_limit, settings.groq_extraction_rpd_soft_limit
    return settings.gemini_rpm_soft_limit, settings.gemini_rpd_soft_limit