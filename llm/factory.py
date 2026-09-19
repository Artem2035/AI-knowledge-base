"""
Единственная точка выбора активного LLM-провайдера. Orchestrator вызывает
create_llm_client(...) один раз при старте — дальше вся система работает с
объектом, реализующим generate_structured(...), не зная, какой конкретно
провайдер используется.

В MVP реализован только Groq. Чтобы добавить третьего провайдера (например,
Together, Fireworks, локальную модель) — нужно реализовать тот же интерфейс
(llm/base.py::LLMClient Protocol) в llm/<provider>_client.py по образцу
llm/groq_client.py и добавить один elif сюда. roles/*, orchestrator/state_machine.py
трогать не нужно.
"""
from __future__ import annotations

from config.settings import Settings
from orchestrator.budget import LLMBudget


def create_llm_client(settings: Settings, budget: LLMBudget):
    if settings.llm_provider == "groq":
        from llm.groq_client import GroqClient

        return GroqClient(settings=settings, budget=budget)

    raise ValueError(
        f"Неизвестный LLM_PROVIDER: {settings.llm_provider!r}. "
        "Допустимое значение в MVP: 'groq'. Чтобы добавить провайдера, "
        "реализуйте generate_structured(...) в llm/<provider>_client.py "
        "(см. llm/groq_client.py как образец) и добавьте один elif сюда."
    )


def create_extraction_llm_client(settings: Settings, budget: LLMBudget):
    """Отдельный клиент для роли extractor_critic/elaborator. На Groq
    использует другую модель (settings.groq_extraction_model, по умолчанию
    openai/gpt-oss-120b, TPM=8000) вместо settings.groq_model — единственной
    роли, где регулярно не хватает TPM обычной модели из-за объёма текста
    источников/подтем. Настройки клонируются через model_copy(), чтобы не
    задевать остальные роли, использующие обычный create_llm_client().

    Для будущего провайдера без отдельного TPM-профиля extraction-модели
    эта функция должна просто возвращать тот же клиент, что и для
    остальных ролей — см. ветку if settings.llm_provider != "groq" ниже,
    которая уже это делает по умолчанию."""
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
    raise ValueError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider!r}.")


def extraction_budget_limits(settings: Settings) -> tuple[int, int]:
    """То же самое, но для extraction-клиента — на Groq у него другой RPD
    (см. create_extraction_llm_client): gpt-oss-120b как extraction-модель
    считается API отдельно от основной модели, поэтому лимиты не совпадают
    с budget_limits_for_provider()."""
    if settings.llm_provider == "groq":
        return settings.groq_rpm_soft_limit, settings.groq_extraction_rpd_soft_limit
    raise ValueError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider!r}.")