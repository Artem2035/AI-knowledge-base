"""
Единственная точка выбора активного LLM-провайдера. Orchestrator вызывает
create_llm_client(...) один раз при старте — дальше вся система работает с
объектом, реализующим generate_structured(...), не зная, какой конкретно
провайдер (и, для 'openrouter', какая конкретно из двух моделей под
капотом) используется.

Провайдеры в MVP:
- 'groq' (прежний, по умолчанию) — один клиент, одна модель на все роли
  (плюс отдельная extraction-модель, см. create_extraction_llm_client).
- 'openrouter' (новый) — РОЛЬ-BASED РОУТИНГ через llm/router.py::
  RoleRoutingLLMClient между двумя бесплатными моделями OpenRouter:
    * Nemotron 3 Super — дефолт: outline_planner, elaborator, critic,
      vault_dedup, folder_assignment, researcher_selection — большой
      контекст и тренировка на multi-step planning/reasoning.
    * Gemma 4 26B A4B — ТОЛЬКО synthesizer_write (Writer) — заявленная
      нативная поддержка structured output/function calling снижает риск
      невалидного JSON именно там, где схема (DraftNoteOutput) самая
      объёмная и от неё напрямую зависит запись в Vault.

Чтобы добавить ещё одного провайдера в будущем — реализовать тот же
интерфейс (llm/base.py::LLMClient Protocol) в llm/<provider>_client.py по
образцу llm/groq_client.py / llm/openrouter_client.py и добавить один elif
сюда. roles/*, orchestrator/state_machine.py трогать не нужно.
"""
from __future__ import annotations

from config.settings import Settings
from orchestrator.budget import LLMBudget


def create_llm_client(settings: Settings, budget: LLMBudget):
    if settings.llm_provider == "groq":
        from llm.groq_client import GroqClient

        return GroqClient(settings=settings, budget=budget)

    if settings.llm_provider == "openrouter":
        # budget, переданный сюда извне (см. Orchestrator.__init__), НЕ
        # используется — роутер строит СВОИ собственные LLMBudget, по
        # одному на модель (см. _create_openrouter_router), т.к. у Nemotron
        # и Gemma разные free-tier лимиты на OpenRouter. Тот же паттерн
        # (игнорировать переданный budget ради модель-специфичного) уже
        # применяется ниже в create_extraction_llm_client для Groq.
        return _create_openrouter_router(settings)

    raise ValueError(
        f"Неизвестный LLM_PROVIDER: {settings.llm_provider!r}. "
        "Допустимые значения в MVP: 'groq', 'openrouter'. Чтобы добавить "
        "провайдера, реализуйте generate_structured(...) в "
        "llm/<provider>_client.py (см. llm/groq_client.py, "
        "llm/openrouter_client.py как образцы) и добавьте одну ветку сюда."
    )


def _create_openrouter_router(settings: Settings):
    """
    RoleRoutingLLMClient поверх MultiModelOpenRouterClient на группу ролей
    (planning/writing) — см. llm/multi_model_client.py про auto/manual
    (settings.openrouter_selection_mode).

    У каждой модели-кандидата свой LLMBudget. В MVP один и тот же soft-лимит
    (settings.openrouter_planning_rpm_soft_limit/rpd_soft_limit) применяется
    ко всем кандидатам группы — упрощение: раздельная настройка per-модель
    добавила бы конфигурационный шум, непропорциональный MVP. Все бюджеты
    пишут в общий TaskStatus.llm_calls_used, так что MAX_LLM_CALLS_PER_TASK
    остаётся единым потолком на задачу.
    """
    from llm.openrouter_client import OpenRouterClient
    from llm.multi_model_client import MultiModelOpenRouterClient
    from llm.router import RoleRoutingLLMClient

    def _build_group(models: list[str], rpm: int, rpd: int) -> MultiModelOpenRouterClient:
        candidates = [
            OpenRouterClient(
                settings=settings,
                budget=LLMBudget(
                    max_calls_per_task=settings.max_llm_calls_per_task,
                    rpm_soft_limit=rpm, rpd_soft_limit=rpd,
                ),
                model=model,
            )
            for model in models
        ]
        return MultiModelOpenRouterClient(candidates, selection_mode=settings.openrouter_selection_mode)

    planning_group = _build_group(
        settings.openrouter_planning_models,
        settings.openrouter_planning_rpm_soft_limit,
        settings.openrouter_planning_rpd_soft_limit,
    )
    writing_group = _build_group(
        settings.openrouter_writing_models,
        settings.openrouter_writing_rpm_soft_limit,
        settings.openrouter_writing_rpd_soft_limit,
    )

    return RoleRoutingLLMClient(
        default_client=planning_group,
        role_map={"synthesizer_write": writing_group},
    )


def create_extraction_llm_client(settings: Settings, budget: LLMBudget):
    """Отдельный клиент для роли extractor_critic/elaborator — самая
    частая по числу вызовов роль (см. docs/architecture.md §5.3).

    На Groq использует другую модель (settings.groq_extraction_model) с
    отдельным TPM-профилем — единственной роли, где регулярно не хватает
    TPM обычной модели.

    На OpenRouter (и для любого другого будущего провайдера без отдельного
    TPM-профиля extraction-модели) elaborator и так уходит на Nemotron
    (planning-модель, дефолт роутера, см. _create_openrouter_router) —
    отдельной МОДЕЛИ не требуется, но отдельный БЮДЖЕТ (throttle) всё
    равно полезен, поэтому просто строится ещё один роутер со своим
    независимым LLMBudget внутри."""
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
    """Возвращает (rpm_soft_limit, rpd_soft_limit) для Orchestrator.__init__
    (см. state_machine.py::self.budget). Для 'openrouter' этот объект
    НЕ передаётся реальным клиентам (см. _create_openrouter_router,
    строящий свои собственные LLMBudget) — значение здесь используется
    только как разумный дефолт для этого неиспользуемого напрямую
    экземпляра."""
    if settings.llm_provider == "groq":
        return settings.groq_rpm_soft_limit, settings.groq_rpd_soft_limit
    if settings.llm_provider == "openrouter":
        return settings.openrouter_planning_rpm_soft_limit, settings.openrouter_planning_rpd_soft_limit
    raise ValueError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider!r}.")


def extraction_budget_limits(settings: Settings) -> tuple[int, int]:
    """То же самое, но для extraction-клиента (см. create_extraction_llm_client)."""
    if settings.llm_provider == "groq":
        return settings.groq_rpm_soft_limit, settings.groq_extraction_rpd_soft_limit
    if settings.llm_provider == "openrouter":
        return settings.openrouter_planning_rpm_soft_limit, settings.openrouter_planning_rpd_soft_limit
    raise ValueError(f"Неизвестный LLM_PROVIDER: {settings.llm_provider!r}.")