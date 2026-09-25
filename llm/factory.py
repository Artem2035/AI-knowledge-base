"""
Единственная точка выбора активного LLM-провайдера. Orchestrator вызывает
create_llm_client(...) один раз при старте — дальше вся система работает с
объектом, реализующим generate_structured(...), не зная, какой конкретно
провайдер используется.

v4-A1 (см. docs/groq_token_budget.md §4, вариант A1): create_extraction_llm_client
теперь принимает опциональный primary_client — уже созданный основной
GroqClient (self.llm в Orchestrator). Если groq_extraction_model совпадает
с groq_model (дефолт проекта — оба "openai/gpt-oss-120b") и флаг
settings.groq_share_limiter_when_same_model включён (дефолт True) —
extraction-клиент переиспользует TokenRateLimiter/TokenEstimateCalibrator
основного клиента вместо создания собственных. Без этого два клиента,
физически делящих один TPM Groq, независимо резервировали бы токены "не
зная" друг о друге — риск превышения реального лимита суммой двух
локальных резервов, невидимый в логах ни одного из клиентов по отдельности.
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
        # используется — роутер работает с моделями-кандидатами, у
        # каждой свой LLMBudget (см. _create_openrouter_router), т.к. у
        # разных моделей OpenRouter разные free-tier лимиты. Тот же
        # паттерн (игнорировать переданный budget ради модель-специфичного)
        # уже применяется ниже в create_extraction_llm_client для Groq.
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
    Строит RoleRoutingLLMClient поверх ДВУХ групп моделей-кандидатов
    OpenRouter (planning/writing) — см. llm/router.py про то, как роутер
    сам разворачивает список в failover-цепочку по
    settings.openrouter_selection_mode ("auto"/"manual").

    У каждой модели-кандидата свой LLMBudget. В MVP один и тот же soft-лимит
    (settings.openrouter_planning_rpm_soft_limit/rpd_soft_limit) применяется
    ко всем кандидатам группы — упрощение: раздельная настройка per-модель
    добавила бы конфигурационный шум, непропорциональный MVP. Все бюджеты
    пишут в общий TaskStatus.llm_calls_used, так что MAX_LLM_CALLS_PER_TASK
    остаётся единым потолком на задачу.
    """
    from llm.openrouter_client import OpenRouterClient
    from llm.router import RoleRoutingLLMClient

    def _build_group(models: list[str], rpm: int, rpd: int) -> list[OpenRouterClient]:
        return [
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
        selection_mode=settings.openrouter_selection_mode,
    )


def create_extraction_llm_client(settings: Settings, budget: LLMBudget, primary_client=None):
    """Отдельный клиент для роли extractor_critic/elaborator.

    primary_client (НОВОЕ, v4-A1): основной LLM-клиент задачи (обычно
    Orchestrator.self.llm), ЕСЛИ он уже создан. Используется только для
    провайдера 'groq' и только когда:
      1) settings.groq_share_limiter_when_same_model=True (дефолт),
      2) settings.groq_extraction_model == settings.groq_model,
      3) primary_client — реальный экземпляр GroqClient (не роутер
         OpenRouter и не заглушка).
    Если хотя бы одно условие не выполнено — поведение НЕ отличается от
    прежнего: создаётся полностью независимый GroqClient со своим
    TokenRateLimiter/TokenEstimateCalibrator (как и было до v4-A1)."""
    if settings.llm_provider != "groq":
        return create_llm_client(settings, budget)

    from llm.groq_client import GroqClient

    extraction_settings = settings.model_copy(
        update={
            "groq_model": settings.groq_extraction_model,
            "groq_tpm_limit": settings.groq_extraction_tpm_limit,
        }
    )

    shared_limiter = None
    shared_calibrator = None
    if (
        getattr(settings, "groq_share_limiter_when_same_model", True)
        and settings.groq_extraction_model == settings.groq_model
        and isinstance(primary_client, GroqClient)
    ):
        shared_limiter = primary_client._limiter
        shared_calibrator = primary_client._calibrator
        import logging

        logging.getLogger(__name__).info(
            "create_extraction_llm_client: модель extraction-клиента (%s) "
            "совпадает с основной — переиспользуем TokenRateLimiter/"
            "TokenEstimateCalibrator основного клиента (groq_share_limiter_"
            "when_same_model=True).",
            settings.groq_extraction_model,
        )

    return GroqClient(
        settings=extraction_settings,
        budget=budget,
        shared_limiter=shared_limiter,
        shared_calibrator=shared_calibrator,
    )


def budget_limits_for_provider(settings: Settings) -> tuple[int, int]:
    """Возвращает (rpm_soft_limit, rpd_soft_limit) для Orchestrator.__init__
    (см. state_machine.py::self.budget). Для 'openrouter' этот объект
    НЕ передаётся реальным клиентам (см. _create_openrouter_router,
    строящий свои собственные LLMBudget на кандидата) — значение здесь
    используется только как разумный дефолт для этого неиспользуемого
    напрямую экземпляра."""
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