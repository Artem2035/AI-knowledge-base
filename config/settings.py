"""
Единая точка конфигурации системы.

Все настройки читаются из переменных окружения / .env файла и НИКОГДА
не хардкодятся в коде ролей/инструментов. Это единственный модуль,
который знает про имена env-переменных.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Провайдер LLM ----
    # В MVP допустимо только значение "groq". Поле намеренно типизировано
    # как обычная строка (не Literal), а не привязано жёстко к одному
    # значению — это единственная точка переключения провайдера
    # (см. llm/factory.py::create_llm_client). roles/*, orchestrator
    # работают с любым провайдером одинаково, т.к. каждый клиент
    # реализует один и тот же метод generate_structured() (см.
    # llm/base.py::LLMClient Protocol).
    # Чтобы добавить второго провайдера в будущем — реализовать llm/<provider>_client.py по образцу
    # llm/groq_client.py и добавить одну ветку в llm/factory.py; менять
    # тип этого поля не требуется.
    llm_provider: str = Field(default="groq")

    # ---- Режим исследования ----
    # "web" — прежний пайплайн: DuckDuckGo-поиск + fetch страниц +
    #   Extractor/Critic извлекает evidence из реального текста источников.
    #   Даёт проверяемые source_refs, но зависит от нестабильной сети и
    #   тратит больше вызовов LLM.
    # "knowledge" (дефолт) — без веб-поиска: Elaborator (roles/elaborator.py)
    #   генерирует evidence по каждой подтеме плана из знаний модели.
    #   Быстрее и надёжнее (нет сетевого I/O к внешним сайтам), но заметки
    #   не имеют проверяемых источников — помечаются
    #   frontmatter.source="model-knowledge" (см. tools/markdown_tools.py) и
    #   должны рассматриваться как черновой конспект, требующий вашей
    #   проверки, а не как исследование с цитируемыми источниками.
    research_mode: Literal["web", "knowledge"] = Field(default="knowledge")

    # ---- OpenRouter (второй провайдер, роль-based роутинг с failover) ----
    openrouter_api_key: str = Field(default="")
    openrouter_base_url: str = Field(default="https://openrouter.ai/api/v1")
    openrouter_timeout_seconds: int = Field(default=60, ge=1)

    # "auto" (дефолт) — при сбое текущей модели (перегрузка апстрима,
    #   rate limit, исчерпанный дневной бесплатный лимит именно у неё,
    #   невалидный JSON после repair) клиент сам пробует следующую модель
    #   из списка *_models ниже, по порядку приоритета.
    # "manual" — используется только ПЕРВАЯ модель списка; при её сбое —
    #   контролируемая остановка с подсказкой, какие резервные модели
    #   есть в конфиге (переключение — ваше явное решение).
    openrouter_selection_mode: Literal["auto", "manual"] = Field(default="auto")

    # Списки моделей-кандидатов НА РОЛЬ, в порядке приоритета. В .env можно
    # задать comma-separated строкой:
    # OPENROUTER_PLANNING_MODELS=nvidia/nemotron-3-ultra-550b-a55b:free,z-ai/glm-5.2:free
    openrouter_planning_models: list[str] = Field(
        default_factory=lambda: [
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "z-ai/glm-5.2:free",
            "thinkingmachines/inkling-small:free",
        ]
    )
    openrouter_planning_rpm_soft_limit: int = Field(default=15, ge=1)
    openrouter_planning_rpd_soft_limit: int = Field(default=150, ge=1)

    openrouter_writing_models: list[str] = Field(
        default_factory=lambda: [
            "google/gemma-4-26b-a4b-it:free",
            "google/gemma-4-31b:free",
        ]
    )
    openrouter_writing_rpm_soft_limit: int = Field(default=15, ge=1)
    openrouter_writing_rpd_soft_limit: int = Field(default=150, ge=1)

    @field_validator("openrouter_planning_models", "openrouter_writing_models", mode="before")
    @classmethod
    def _split_csv_models(cls, v):
        if isinstance(v, str):
            return [m.strip() for m in v.split(",") if m.strip()]
        return v

    # ---- OpenRouter: проактивная проверка остатка free-tier лимита ----
    # См. GET /api/v1/key (docs: Open router ai limits) — поле
    # free_model_daily_requests.{used,limit,remaining} показывает дневной
    # счётчик запросов к :free-моделям ДЛЯ ВСЕГО КЛЮЧА (не по модели).
    # Если True — перед КАЖДОЙ группой вызовов (planning/writing)
    # MultiModelOpenRouterClient опционально запрашивает этот эндпоинт
    # (см. llm/openrouter_client.py::fetch_key_info) и кэширует результат
    # на openrouter_key_check_cache_seconds — это позволяет узнать, что
    # дневной бесплатный лимit близок к исчерпанию, ДО того как реальный
    # запрос упадёт с 429, и залогировать явное предупреждение. Само по
    # себе НЕ блокирует вызов (сервер — источник истины), просто даёт
    # более информативное сообщение при остановке.
    openrouter_check_key_before_call: bool = Field(default=True)
    openrouter_key_check_cache_seconds: float = Field(default=60.0, ge=0.0)

    # ---- Groq ----
    groq_api_key: str = Field(default="", description="Ключ Groq API (бесплатный тир)")
    groq_model: str = Field(default="openai/gpt-oss-120b")
    groq_timeout_seconds: int = Field(default=60, ge=1)
    # Реальный лимит free tier у Groq выше (~30 RPM / ~14400 RPD), но
    # берём с запасом, чтобы не упираться в TPM-лимит на длинных промптах.
    groq_rpm_soft_limit: int = Field(default=25, ge=1)
    groq_rpd_soft_limit: int = Field(default=10000, ge=1)

    # Явный TPM реальной модели. Раньше это значение подхватывалось только
    # неявным дефолтом GroqClient.DEFAULT_TPM_LIMIT=8000 через getattr —
    # если Groq изменит лимит (уже случалось однажды, см. комментарий про
    # groq/compound-mini ниже), поле в Settings не менялось бы автоматически
    # и рассинхрон был бы невидим до первой волны 429. Явное поле здесь —
    # чтобы значение было видно в конфиге и легко сверялось с
    # `Current Limits for AI models` при ревью, а не пряталось в коде клиента.
    groq_tpm_limit: int = Field(default=8000, ge=1)

    # ---- Groq: модель для extraction ----
    # ИЗМЕНЕНО: раньше здесь стоял groq/compound-mini (заявленный TPM=70000),
    # но на практике compound-mini маршрутизирует запросы на llama-3.3-70b
    # -versatile с отдельным, скрытым от клиента лимитом (наблюдалось
    # Limit=12000 в реальном логе 429) — заявленные 70K не отражают реальный
    # бюджет, из-за чего TokenRateLimiter калибровался неверно и давал
    # массовые 429 (12 из 13 запросов). openai/gpt-oss-120b имеет более
    # скромный, но ЧЕСТНЫЙ TPM=8000 — свой, прямой, без скрытой прослойки —
    # и официально поддерживает strict json_schema (constrained decoding),
    # что даёт гарантированно валидный JSON вместо best-effort JSON mode.
    groq_extraction_model: str = Field(default="openai/gpt-oss-120b")
    groq_extraction_tpm_limit: int = Field(default=8000, ge=1)
    groq_extraction_rpd_soft_limit: int = Field(default=900, ge=1)  # запас от реального лимита 1000
    groq_account_for_prompt_cache: bool = Field(default=False)

    # ---- Groq: adaptive rate limiting (llm/groq_client.py::TokenRateLimiter) ----
    # Вынесено в конфиг по итогам обсуждения простоя из-за TPM rate limit
    # (см. комментарии в заголовке llm/groq_client.py, варианты 1/3/4).
    # Значения по умолчанию совпадают с тем, что раньше было захардкожено
    # прямо в классах TokenRateLimiter/TokenEstimateCalibrator — изменение
    # этого файла НЕ меняет поведение системы по умолчанию, только даёт
    # возможность потюнить без правки кода.

    # Множитель к реальному TPM-лимиту модели — держим запас, чтобы неточная
    # оценка токенов сама по себе не провоцировала 429 (было захардкожено
    # как safety_margin=0.85 в конструкторе TokenRateLimiter).
    groq_limiter_safety_margin: float = Field(default=0.85, gt=0.0, le=1.0)

    # Adaptive safety margin (вариант 3): что происходит с лимитом сразу
    # после РЕАЛЬНОГО 429 от API.
    # penalty_factor — на какую долю ужимается эффективный лимит за одно
    # срабатывание (0.8 = минус 20%).
    groq_margin_penalty_factor: float = Field(default=0.8, gt=0.0, lt=1.0)
    # min_penalty — не даём итоговому множителю уйти ниже этой доли от
    # базового лимита, даже при нескольких 429 подряд в рамках одной задачи
    # (иначе можно почти полностью парализовать вызовы одним неудачным стартом).
    groq_margin_min_penalty: float = Field(default=0.5, gt=0.0, le=1.0)
    # recovery_seconds — через сколько секунд без новых 429 лимит плавно
    # восстанавливается до базового значения.
    groq_margin_recovery_seconds: float = Field(default=300.0, ge=0.0)

    # "step" — прежнее поведение: margin_penalty остаётся ужатым ПОЛНОСТЬЮ
    #   все groq_margin_recovery_seconds секунд, затем мгновенно скачет на
    #   100%. Проблема этого режима: пока действует штраф, бюджет занижен
    #   целиком, даже если реальный риск повторной 429 давно рассеялся —
    #   именно это давало наблюдаемый эффект "система берёт 2000-3000 из
    #   8000" в течение до 5 минут после одной единственной 429.
    # "linear" (НОВЫЙ ДЕФОЛТ) — margin_penalty линейно растёт от значения
    #   на момент штрафа до 1.0 в течение groq_margin_recovery_seconds.
    #   Выбран дефолтом, а не "step", потому что устраняет ступеньку без
    #   изменения общего "консервативного окна" в groq_margin_recovery_seconds
    #   (которое остаётся тем же, что и раньше, — риск повторной 429 не
    #   растёт, просто бюджет перестаёт быть искусственно постоянным все
    #   5 минут).
    groq_margin_recovery_mode: Literal["step", "linear"] = Field(default="linear")
    # GroqClient — основной (self.llm) и extraction-клиент
    # (self.extraction_client, см. orchestrator/state_machine.py). По
    # умолчанию groq_extraction_model == groq_model ("openai/gpt-oss-120b"
    # для обоих) — то есть оба клиента физически делят ОДИН TPM-лимит Groq
    # API, но раньше каждый вёл собственный TokenRateLimiter/
    # TokenEstimateCalibrator, не зная о расходе другого. Сумма двух
    # независимых резервов могла превысить реальный лимит без единого
    # видимого предупреждения ни в одном из логов по отдельности.
    # При True (дефолт) — если модели действительно совпадают,
    # extraction-клиент переиспользует лимитер/калибратор основного клиента
    # (см. llm/factory.py::create_extraction_llm_client). Дефолт True, а не
    # False, потому что раздельные лимитеры при совпадающей модели — это
    # ВСЕГДА источник риска голодания, а не осознанный trade-off; ситуация,
    # где стоило бы выключить флаг, не просматривается (разве что для
    # изолированной отладки одного из двух клиентов).
    groq_share_limiter_when_same_model: bool = Field(default=True)
    # НОВОЕ (B1 из документации): резерв под OUTPUT-токены модели.
    # Раньше был единственным хардкодом GroqClient.RESERVED_OUTPUT_TOKENS=1500
    # для ВСЕХ ролей одинаково — но роли резко отличаются по длине ответа:
    # critic/vault_dedup/folder_assignment отвечают компактным JSON на
    # несколько полей, а synthesizer_write пишет целую заметку. Единый
    # резерв "на самый длинный случай" отнимал бюджет под prompt там, где
    # он объективно не нужен — одна из причин "недоиспользования" TPM.
    #
    # groq_reserved_output_tokens_default — используется, пока по роли ЕЩЁ
    # НЕТ ни одного наблюдения (холодный старт калибровки). Значение
    # СОЗНАТЕЛЬНО совпадает со старым хардкодом 1500: первый вызов КАЖДОЙ
    # роли в новой задаче должен быть не менее безопасным, чем раньше —
    # лучше один раз недоиспользовать бюджет на первом вызове, чем
    # обрезать реальный ответ модели или спровоцировать 429.
    groq_reserved_output_tokens_default: int = Field(default=1500, ge=1)
    # Нижняя граница калиброванного резерва (см.
    # llm/groq_client.py::TokenEstimateCalibrator.reserved_output_tokens).
    # Даже если РОЛЬ ЗА ВСЮ ИСТОРИЮ задачи отвечала коротко (например,
    # critic почти всегда verdict='ok' без feedback), не даём резерву упасть
    # ниже этого пола — единичная короткая серия ответов не гарантирует,
    # что следующий вызов той же роли не окажется длиннее (например, critic
    # с развёрнутым feedback на rewrite). 300 токенов — с запасом покрывает
    # короткий JSON-вердикт с парой предложений комментария; выбрано по
    # порядку величины реальных коротких JSON-ответов ролей проекта
    # (CriticVerdictOutput, DedupDecisionOutput, FolderAssignmentOutput), а
    # не по формальному расчёту — при появлении реальных данных калибровки
    # это значение стоит сверить и скорректировать (см. чек-лист
    # тестирования в groq_token_budget.md §7).
    groq_reserved_output_min_tokens: int = Field(default=300, ge=1)


    # EMA-коэффициент калибровки OUTPUT-резерва по роли. Отдельный параметр
    # от groq_calibration_ema_alpha (см. ниже) — тот калибрует оценку ВХОДНОГО
    # prompt_tokens (в единицах "во сколько раз наивная оценка отличается от
    # факта"), а этот — саму величину completion_tokens (в токенах, не в
    # соотношении). Это разные по природе величины с разной ожидаемой
    # дисперсией между вызовами одной роли, поэтому смешивать их в одном
    # alpha было бы случайным совпадением, а не осознанным решением.
    # 0.3 выбрано ТЕМ ЖЕ значением, что и groq_calibration_ema_alpha —
    # единственная причина именно этого числа: нет пока данных, чтобы
    # обосновать другое значение специально для output; при появлении
    # реальной статистики (см. чек-лист в groq_token_budget.md) эти два
    # параметра стоит развести по фактической волатильности.
    groq_output_calibration_ema_alpha: float = Field(default=0.3, gt=0.0, le=1.0)
    # Калибровка оценки токенов по роли (вариант 1 + учёт Groq prompt
    # caching, см. TokenEstimateCalibrator).
    # ema_alpha — вес нового наблюдения в экспоненциальном скользящем
    # среднем; больше — быстрее адаптация, но чувствительнее к шуму
    # отдельных вызовов.
    groq_calibration_ema_alpha: float = Field(default=0.3, gt=0.0, le=1.0)
    # min/max_ratio — ограничивают, во сколько раз калиброванная оценка
    # может отличаться от "наивной" по одному наблюдению — защита от того,
    # чтобы один нетипичный вызов (например, первый вызов роли без кэша)
    # не увёл коэффициент в крайность на весь остаток задачи.
    groq_calibration_min_ratio: float = Field(default=0.05, gt=0.0)
    groq_calibration_max_ratio: float = Field(default=1.5, gt=0.0)

    # ---- Groq: точность "наивной" оценки токенов по символам ----
    # Раньше это были хардкоженные константы прямо в llm/common.py
    # (2.3 / 4.0 / порог доли кириллицы 0.3), не настраиваемые и не
    # протестированные на реальных промптах проекта (см. B4 в документации
    # бюджета токенов). Вынесены сюда, чтобы их можно было пересчитать по
    # факту (собрать реальные (prompt, usage.prompt_tokens) пары и
    # подобрать точные коэффициенты) БЕЗ правки кода — только конфиг.
    # Значения по умолчанию НЕ изменены относительно старого хардкода —
    # это сознательно: сама по себе эта правка добавляет только
    # настраиваемость, но не меняет поведение системы, пока кто-то явно
    # не подберёт более точные числа по собранным данным.
    groq_chars_per_token_cyrillic: float = Field(default=2.3, gt=0.0)
    groq_chars_per_token_latin: float = Field(default=4.0, gt=0.0)
    groq_cyrillic_ratio_threshold: float = Field(default=0.3, ge=0.0, le=1.0)

    # ---- Объединение заметок в конце workflow ----
    # См. staging/draft_merge.py — ноль LLM-вызовов, чистая пересборка уже
    # написанного текста ПОСЛЕ Writer+Critic, ПЕРЕД validation/staging.
    enable_draft_merging: bool = Field(default=True)

    # Как именно объединять, когда enable_draft_merging=True:
    # "all" (дефолт) — ВСЕ написанные заметки (action=create) сливаются в
    #   одну итоговую заметку автоматически, без интерактивного выбора —
    #   поведение однозначно и предсказуемо: включил флаг -> получил одну
    #   большую заметку по теме задачи.
    # "select" — пользователь сам выбирает, какие заметки объединить (можно
    #   несколькими независимыми группами, см. staging/draft_merge.py::
    #   apply_merges), остальные остаются отдельными файлами как обычно.
    draft_merge_mode: Literal["all", "select"] = Field(default="all")

    @field_validator("groq_calibration_max_ratio")
    @classmethod
    def _max_ratio_above_min(cls, v: float, info) -> float:
        min_ratio = info.data.get("groq_calibration_min_ratio")
        if min_ratio is not None and v <= min_ratio:
            raise ValueError(
                "groq_calibration_max_ratio должен быть больше "
                "groq_calibration_min_ratio"
            )
        return v

    free_only: bool = Field(default=True, description="Жёсткий флаг: только бесплатные провайдеры")

    # Общий бюджет вызовов на задачу — не зависит от того, какой провайдер активен
    max_llm_calls_per_task: int = Field(default=40, ge=1)
    max_llm_retries: int = Field(default=3, ge=0)

    # ---- Vault ----
    vault_path: Path = Field(default=Path("./vault_placeholder"))
    vault_name: str = Field(default="MyVault")
    default_notes_folder: str = Field(default="Знания")

    # ---- Рабочие директории (строго вне Vault) ----
    workdir: Path = Field(default=Path("./.obsidian_ai_kb"))
    staging_dir: Path = Field(default=Path("./.obsidian_ai_kb/staging"))
    db_path: Path = Field(default=Path("./.obsidian_ai_kb/vault_index.sqlite3"))

    # ---- Retrieval / dedup ----
    use_local_embeddings: bool = Field(default=True)
    embedding_model: str = Field(
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    dedup_high_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    dedup_low_threshold: float = Field(default=0.55, ge=0.0, le=1.0)

    # ---- Synthesizer / Writer ----
    max_subpoints_per_generation_batch: int = Field(
        default=6, ge=1,
        description=(
            "Потолок подпунктов заметки на один вызов Elaborator по КАЧЕСТВУ, "
            "не по токен-бюджету — при большем числе подпунктов в одном вызове "
            "модель даёт поверхностные однострочные ответы."
        ),
    )

    # ---- Critic ----
    # Сколько раз Writer имеет право переписать заметку по замечаниям
    # Critic-а в рамках одной задачи. Жёсткий bounded retry — не цикл до
    # "ok", а фиксированный потолок попыток (см. roles/critic.py): после
    # исчерпания заметка идёт в staging как есть, с пометкой
    # needs_review=true, а не блокирует всю задачу и не тратит вызовы
    # бесконечно.
    max_critic_rounds: int = Field(default=1, ge=0)

    # ---- Прочее ----
    language: str = Field(default="ru")
    allow_delete: bool = Field(default=False)
    git_enabled: bool = Field(default=False)
    max_sources_per_subtopic: int = Field(default=4, ge=1)
    max_search_results_per_query: int = Field(default=6, ge=1)
    max_chunks_per_source: int = Field(
        default=3, ge=1,
        description="Потолок единиц (чанков) на один источник в extractor_critic — источники, требующие больше, обрезаются с предупреждением",
    )
    checkpoint_dir: Path = Field(default=Path("./.obsidian_ai_kb/checkpoints"))

    @field_validator("vault_path", "workdir", "staging_dir", "db_path", mode="before")
    @classmethod
    def _expand(cls, v: str | Path) -> Path:
        return Path(v).expanduser()

    def ensure_dirs(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def validate_free_only(self) -> None:
        """
        Жёсткая проверка режима FREE ONLY. Вызывается при старте Orchestrator.
        Ничего не "чинит" автоматически — если free_only=False, явно требуем
        подтверждения через отдельный флаг, чтобы платный режим никогда не
        включался случайно/по умолчанию.
        """
        if not self.free_only:
            raise RuntimeError(
                "FREE_ONLY=false запрещено в текущей версии MVP. "
                "Система спроектирована работать исключительно на бесплатном "
                "Groq API. Платные провайдеры сознательно не реализованы."
            )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Только для тестов — сбросить закэшированные настройки."""
    global _settings
    _settings = None