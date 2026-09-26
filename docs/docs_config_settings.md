# Документация: `config/settings.py`

> Единая точка конфигурации системы (докстринг модуля: "Все настройки читаются
> из переменных окружения / `.env` файла и НИКОГДА не хардкодятся в коде ролей/
> инструментов. Это единственный модуль, который знает про имена env-переменных.").
> Реализован через `pydantic_settings.BaseSettings` — каждое поле автоматически
> читается из одноимённой (в верхнем регистре) переменной окружения или `.env`,
> без ручного парсинга.

---

## 0. `class Settings(BaseSettings)`

**Описание.** Единственный класс модуля. Все поля ниже сгруппированы по
смысловым блокам (как в самом файле), для каждого указано: тип, дефолт,
откуда/кем читается/используется, назначение. `model_config` задаёт
`env_file=".env"`, `env_file_encoding="utf-8"`, `extra="ignore"` (лишние
переменные окружения не вызывают ошибку валидации).

---

## 1. Провайдер LLM и режим исследования

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `llm_provider` | `str` | `"groq"` | Единственная точка переключения провайдера (см. `llm/factory.py::create_llm_client`). Намеренно НЕ `Literal` — чтобы добавление нового провайдера в будущем не требовало менять тип поля, только добавить ветку в `factory.py`. Допустимые значения в MVP: `"groq"`, `"openrouter"`. |
| `research_mode` | `Literal["web", "knowledge"]` | `"knowledge"` | `"web"` — прежний пайплайн (DuckDuckGo-поиск + fetch + Extractor/Critic извлекает evidence из реального текста, даёт проверяемые `source_refs`, но зависит от сети и тратит больше вызовов). `"knowledge"` (дефолт) — без веб-поиска: `roles/elaborator.py` генерирует evidence из знаний модели, быстрее и без сетевого I/O, но заметки помечаются `frontmatter.source="model-knowledge"` и должны восприниматься как черновой конспект. **Важно:** `orchestrator/state_machine.py::run()` СЕЙЧАС явно блокирует `research_mode="web"` (`OrchestratorStopped`) — миграция на новую структуру плана не завершена. |

---

## 2. OpenRouter (второй провайдер, роль-based роутинг с failover)

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `openrouter_api_key` | `str` | `""` | Ключ API OpenRouter. |
| `openrouter_base_url` | `str` | `"https://openrouter.ai/api/v1"` | Базовый URL (OpenAI-совместимый). |
| `openrouter_timeout_seconds` | `int` (`ge=1`) | `60` | HTTP-таймаут одного запроса `OpenRouterClient`. |
| `openrouter_selection_mode` | `Literal["auto", "manual"]` | `"auto"` | `"auto"` — при сбое текущей модели (перегрузка, rate limit, исчерпанный дневной лимит, невалидный JSON) клиент сам пробует следующую модель из списка по порядку приоритета (см. `llm/router.py::RoleRoutingLLMClient`). `"manual"` — используется ТОЛЬКО первая модель списка; при её сбое — контролируемая остановка с подсказкой о резервных моделях (переключение — явное решение пользователя). |
| `openrouter_planning_models` | `list[str]` | `["nvidia/nemotron-3-ultra-550b-a55b:free", "z-ai/glm-5.2:free", "thinkingmachines/inkling-small:free"]` | Список моделей-кандидатов для planning-ролей (`outline_planner`, `elaborator`, `critic`, `vault_dedup`, `folder_assignment` — всё, кроме `synthesizer_write`), в порядке приоритета. Парсится из CSV-строки через `_split_csv_models` (см. §9), если задано строкой в `.env`. |
| `openrouter_planning_rpm_soft_limit` | `int` (`ge=1`) | `15` | Soft-лимит запросов/мин для planning-группы. |
| `openrouter_planning_rpd_soft_limit` | `int` (`ge=1`) | `150` | Soft-лимит запросов/день для planning-группы. |
| `openrouter_writing_models` | `list[str]` | `["google/gemma-4-26b-a4b-it:free", "google/gemma-4-31b:free"]` | Список моделей-кандидатов ТОЛЬКО для роли `synthesizer_write` (см. `llm/factory.py::_create_openrouter_router`, `role_map={"synthesizer_write": writing_group}`). |
| `openrouter_writing_rpm_soft_limit` | `int` (`ge=1`) | `15` | Soft-лимит RPM для writing-группы. |
| `openrouter_writing_rpd_soft_limit` | `int` (`ge=1`) | `150` | Soft-лимит RPD для writing-группы. |
| `openrouter_check_key_before_call` | `bool` | `True` | Опционально проверять остаток free-tier лимита ключа перед группой вызовов (`GET /api/v1/key`, поле `free_model_daily_requests`) — только для информативности лога, не блокирует вызов (сервер — источник истины). |
| `openrouter_key_check_cache_seconds` | `float` (`ge=0.0`) | `60.0` | На сколько секунд кэшируется результат проверки ключа. |

### 2.1. `_split_csv_models(cls, v)` — `field_validator`

**Описание.** `mode="before"`-валидатор для `openrouter_planning_models` и
`openrouter_writing_models`: если значение пришло СТРОКОЙ (типично из
`.env`, например `OPENROUTER_PLANNING_MODELS=model-a:free,model-b:free`) —
разбивает по запятой, обрезает пробелы, отбрасывает пустые элементы.
Если значение УЖЕ список (например, передано программно в тестах) —
возвращает как есть.

**Параметры:** `v` — сырое значение поля до валидации.
**Возвращаемое значение:** `list[str]` (если было строкой) либо исходное значение.
**Исключения:** не поднимает.

---

## 3. Groq (основной провайдер)

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_api_key` | `str` | `""` | Ключ Groq Free Tier. |
| `groq_model` | `str` | `"openai/gpt-oss-120b"` | Основная модель (planning/critic/vault_dedup/folder_assignment/synthesizer_write). |
| `groq_timeout_seconds` | `int` (`ge=1`) | `60` | HTTP-таймаут одного запроса. |
| `groq_rpm_soft_limit` | `int` (`ge=1`) | `25` | Клиентский soft-throttle RPM — реальный лимит free tier выше (~30 RPM), берётся с запасом, чтобы не упираться в TPM на длинных промптах. |
| `groq_rpd_soft_limit` | `int` (`ge=1`) | `10000` | Ориентировочный дневной soft-лимит. |
| `groq_tpm_limit` | `int` (`ge=1`) | `8000` | Явный TPM реальной модели — вынесено явным полем (а не только class-level fallback `GroqClient.DEFAULT_TPM_LIMIT`), чтобы изменение реального лимита Groq (уже случалось для других моделей проекта, см. комментарий про `groq/compound-mini` ниже) было видно прямо в конфиге при ревью, сверяемо с документом `Current Limits for AI models`. |

### 3.1. Groq: extraction-модель

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_extraction_model` | `str` | `"openai/gpt-oss-120b"` | Модель для `roles/elaborator.py`/`roles/extractor_critic.py` (самая частая по числу вызовов роль). Ранее стоял `groq/compound-mini` (заявленный TPM=70000), но на практике он маршрутизирует запросы на `llama-3.3-70b-versatile` со СКРЫТЫМ от клиента отдельным лимитом (наблюдалось `Limit=12000` в реальном логе 429) — заявленные 70K не отражали реальный бюджет, `TokenRateLimiter` калибровался неверно, массовые 429 (12 из 13 запросов). `openai/gpt-oss-120b` имеет скромный, но ЧЕСТНЫЙ TPM=8000 (свой, прямой, без скрытой прослойки) и официально поддерживает strict `json_schema`. |
| `groq_extraction_tpm_limit` | `int` (`ge=1`) | `8000` | TPM-лимит extraction-модели. |
| `groq_extraction_rpd_soft_limit` | `int` (`ge=1`) | `900` | Дневной soft-лимит extraction-клиента — запас от реального лимита 1000. |
| `groq_account_for_prompt_cache` | `bool` | `False` | Учитывать ли закэшированные Groq токены при расчёте эффективного расхода TPM-бюджета (см. `docs_llm_cycle_part3_groq_client.md §6.8`). |

### 3.2. Groq: adaptive rate limiting (`TokenRateLimiter`)

Значения по умолчанию СОВПАДАЮТ с тем, что раньше было захардкожено в
конструкторах `TokenRateLimiter`/`TokenEstimateCalibrator` — вынесение в
конфиг само по себе НЕ меняет поведение по умолчанию, только даёт
возможность потюнить без правки кода.

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_limiter_safety_margin` | `float` (`gt=0.0, le=1.0`) | `0.85` | Множитель запаса от реального TPM-лимита (используем не более 85%) — компенсирует неточность оценки токенов. |
| `groq_margin_penalty_factor` | `float` (`gt=0.0, lt=1.0`) | `0.8` | На какую долю ужимается эффективный лимит за ОДНО срабатывание реального 429 (`0.8` = минус 20%). |
| `groq_margin_min_penalty` | `float` (`gt=0.0, le=1.0`) | `0.5` | Множитель штрафа не уходит ниже этой доли от базового лимита даже при серии 429 подряд в рамках одной задачи. |
| `groq_margin_recovery_seconds` | `float` (`ge=0.0`) | `300.0` | Через сколько секунд без новых 429 лимит полностью восстанавливается. |
| `groq_margin_recovery_mode` | `Literal["step", "linear"]` | `"linear"` | `"step"` — margin_penalty остаётся ПОЛНОСТЬЮ ужатым все `groq_margin_recovery_seconds`, затем мгновенно скачет на 100% (прежнее поведение — давало наблюдаемый эффект "система берёт 2000-3000 из 8000" до 5 минут после ОДНОЙ 429). `"linear"` (НОВЫЙ ДЕФОЛТ) — margin_penalty линейно растёт от значения на момент штрафа до 1.0 в течение того же окна — устраняет ступеньку без изменения общего "консервативного окна". |
| `groq_share_limiter_when_same_model` | `bool` | `True` | Если модель extraction-клиента реально совпадает с основной — extraction-клиент переиспользует `TokenRateLimiter`/`TokenEstimateCalibrator` ОСНОВНОГО клиента (см. `llm/factory.py::create_extraction_llm_client`), т.к. оба физически делят ОДИН TPM Groq API. Дефолт `True` — раздельные лимитеры при совпадающей модели ВСЕГДА источник риска голодания, а не осознанный trade-off; сценарий, где стоило бы выключить флаг, не просматривается (разве что изолированная отладка одного из двух клиентов). |

### 3.3. Groq: калибровка output-резерва (по роли)

Раньше был единственный хардкод `GroqClient.RESERVED_OUTPUT_TOKENS=1500` для
ВСЕХ ролей одинаково — но роли резко отличаются по длине ответа (`critic`/
`vault_dedup`/`folder_assignment` отвечают компактным JSON, `synthesizer_write`
пишет целую заметку).

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_reserved_output_tokens_default` | `int` (`ge=1`) | `1500` | Используется, пока по роли ЕЩЁ НЕТ ни одного наблюдения (холодный старт). Значение сознательно совпадает со старым хардкодом — первый вызов КАЖДОЙ роли в новой задаче должен быть не менее безопасным, чем раньше. |
| `groq_reserved_output_min_tokens` | `int` (`ge=1`) | `300` | Нижняя граница калиброванного резерва — даже если роль ЗА ВСЮ ИСТОРИЮ задачи отвечала коротко (например, `critic` почти всегда `verdict='ok'` без feedback), резерв не опускается ниже этого пола: единичная короткая серия ответов не гарантирует, что следующий вызов той же роли не окажется длиннее. Выбрано по порядку величины реальных коротких JSON-ответов ролей проекта (`CriticVerdictOutput`, `DedupDecisionOutput`, `FolderAssignmentOutput`), не по формальному расчёту. |
| `groq_output_calibration_ema_alpha` | `float` (`gt=0.0, le=1.0`) | `0.3` | EMA-коэффициент калибровки OUTPUT-резерва по роли — ОТДЕЛЬНЫЙ параметр от `groq_calibration_ema_alpha` (тот калибрует ВХОДНОЙ `prompt_tokens` в виде СООТНОШЕНИЯ, этот — саму величину `completion_tokens` в ТОКЕНАХ, разная природа величин, разная ожидаемая дисперсия — смешивание в одном alpha было бы случайным совпадением). Значение `0.3` совпадает с `groq_calibration_ema_alpha` только потому, что пока нет данных, чтобы обосновать другое число специально для output. |

### 3.4. Groq: калибровка оценки токенов по роли (prompt-ratio)

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_calibration_ema_alpha` | `float` (`gt=0.0, le=1.0`) | `0.3` | Вес нового наблюдения в EMA — больше значит быстрее адаптация, но чувствительнее к шуму отдельных вызовов. |
| `groq_calibration_min_ratio` | `float` (`gt=0.0`) | `0.05` | Нижняя граница калиброванного коэффициента. |
| `groq_calibration_max_ratio` | `float` (`gt=0.0`) | `1.5` | Верхняя граница — защита от того, чтобы ОДИН нетипичный вызов (напр. первый вызов роли без кэша) не увёл коэффициент в крайность на весь остаток задачи. |

#### `_max_ratio_above_min(cls, v, info)` — `field_validator`

**Описание.** Валидатор поля `groq_calibration_max_ratio`: проверяет, что оно
СТРОГО больше уже провалидированного `groq_calibration_min_ratio`
(`info.data.get("groq_calibration_min_ratio")`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `v` | `float` | Значение `groq_calibration_max_ratio` до финальной проверки. |
| `info` | Pydantic `ValidationInfo` | Доступ к уже провалидированным полям (`info.data`). |

**Возвращаемое значение:** `float` — `v` без изменений, если проверка пройдена.
**Исключения:** `ValueError` — если `min_ratio is not None and v <= min_ratio`.

### 3.5. Groq: точность "наивной" оценки токенов по символам

Раньше — хардкоженные константы прямо в `llm/common.py` (`2.3`/`4.0`/порог
доли кириллицы `0.3`), не настраиваемые и не протестированные на реальных
промптах проекта. Значения по умолчанию НЕ изменены относительно старого
хардкода — сама по себе эта правка добавляет только настраиваемость, но не
меняет поведение системы, пока кто-то явно не подберёт более точные числа
по собранным данным.

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_chars_per_token_cyrillic` | `float` (`gt=0.0`) | `2.3` | Символов на токен для кириллического текста. |
| `groq_chars_per_token_latin` | `float` (`gt=0.0`) | `4.0` | Символов на токен для латинского/смешанного текста. |
| `groq_cyrillic_ratio_threshold` | `float` (`ge=0.0, le=1.0`) | `0.3` | Порог доли кириллицы, выше которого текст считается "кириллическим". |

---

## 4. Объединение заметок в конце workflow

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `enable_draft_merging` | `bool` | `True` | См. `staging/draft_merge.py` — ноль LLM-вызовов, чистая пересборка уже написанного текста ПОСЛЕ Writer+Critic, ПЕРЕД validation/staging. |
| `draft_merge_mode` | `Literal["all", "select"]` | `"all"` | `"all"` — ВСЕ написанные заметки (`action=create`) сливаются в одну итоговую АВТОМАТИЧЕСКИ, без интерактивного выбора (предсказуемо: включил флаг — получил одну большую заметку по теме задачи). `"select"` — пользователь сам выбирает, какие заметки объединить (можно несколькими независимыми группами, см. `staging/draft_merge.py::apply_merges`), остальные остаются отдельными файлами. |

---

## 5. Общий бюджет и FREE_ONLY

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `free_only` | `bool` | `True` | Жёсткий флаг: только бесплатные провайдеры. Проверяется `validate_free_only()` (см. §10). |
| `max_llm_calls_per_task` | `int` (`ge=1`) | `40` | Общий бюджет вызовов на задачу — НЕ зависит от того, какой провайдер активен (используется в `orchestrator/budget.py::LLMBudget`). |
| `max_llm_retries` | `int` (`ge=0`) | `3` | Зарезервированное поле — общий предел retry-попыток (фактические retry настроены на уровне `tenacity`-декораторов конкретных клиентов, см. `GroqClient._call_with_retry`, `OpenRouterClient._call_with_retry`). |

---

## 6. Vault

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `vault_path` | `Path` | `Path("./vault_placeholder")` | Абсолютный путь к реальному Obsidian Vault пользователя. |
| `vault_name` | `str` | `"MyVault"` | Название Vault (используется в CLI-выводе/логах, не влияет на логику). |
| `default_notes_folder` | `str` | `"Знания"` | Базовая папка по умолчанию для новых тем — реальная папка задачи строится как `f"{default_notes_folder}/{slugify(plan.topic_title)}"` в `Orchestrator.run()`. |

---

## 7. Рабочие директории (строго вне Vault)

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `workdir` | `Path` | `Path("./.obsidian_ai_kb")` | Корневая рабочая директория проекта. |
| `staging_dir` | `Path` | `Path("./.obsidian_ai_kb/staging")` | Директория staging-changeset'ов (см. `docs_staging.md §1`). |
| `db_path` | `Path` | `Path("./.obsidian_ai_kb/vault_index.sqlite3")` | Файл SQLite-индекса Vault (`vault/db.py::VaultDB`). |
| `checkpoint_dir` | `Path` | `Path("./.obsidian_ai_kb/checkpoints")` | Директория чекпоинтов задач (см. `docs_staging.md §2`). |

### 7.1. `_expand(cls, v)` — `field_validator`

**Описание.** `mode="before"`-валидатор для полей `vault_path`, `workdir`,
`staging_dir`, `db_path`: разворачивает `~` в домашнюю директорию пользователя
(`Path(v).expanduser()`), чтобы пути вида `~/ObsidianVault` из `.env`
работали корректно на любой ОС.

**Параметры:** `v: str | Path`.
**Возвращаемое значение:** `Path` — с раскрытым `~`.
**Исключения:** не поднимает.

---

## 8. Retrieval / dedup

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `use_local_embeddings` | `bool` | `True` | Включает попытку создания `tools/dedup.py::LocalEmbedder` (см. `try_create_embedder`). |
| `embedding_model` | `str` | `"sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"` | Имя модели `sentence-transformers` для локальных эмбеддингов. |
| `dedup_high_threshold` | `float` (`ge=0.0, le=1.0`) | `0.85` | Порог "точно дубликат" для `tools/dedup.py::classify_similarity`. |
| `dedup_low_threshold` | `float` (`ge=0.0, le=1.0`) | `0.55` | Порог "точно разные". |

---

## 9. Synthesizer / Writer

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `max_subpoints_per_generation_batch` | `int` (`ge=1`) | `6` | Потолок подпунктов заметки на ОДИН вызов Elaborator ПО КАЧЕСТВУ, НЕ по токен-бюджету — при большем числе подпунктов в одном вызове модель даёт поверхностные однострочные ответы (см. `llm/chunking.py::batch_for_quality_and_budget`). |

---

## 10. Critic

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `max_critic_rounds` | `int` (`ge=0`) | `1` | Сколько раз Writer имеет право переписать заметку по замечаниям Critic-а в рамках ОДНОЙ задачи — жёсткий bounded retry (см. `roles/critic.py::run_critic_cycle`, `docs_roles_part2.md §4.2`). После исчерпания заметка идёт в staging как есть, с пометкой `needs_review=true`. `0` — критик полностью выключен. |

---

## 11. Прочее

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `language` | `str` | `"ru"` | Язык интерфейса/заметок по умолчанию. |
| `allow_delete` | `bool` | `False` | Разрешает ли `vault/writer.py::VaultWriter.delete_note` реально удалять файлы. |
| `git_enabled` | `bool` | `False` | Включает `staging/commit.py::_git_commit` после каждого `commit_changeset`. |
| `max_sources_per_subtopic` | `int` (`ge=1`) | `4` | Потолок отобранных источников на подтему (только `RESEARCH_MODE=web`, `roles/researcher.py`). |
| `max_search_results_per_query` | `int` (`ge=1`) | `6` | Сколько результатов запрашивать у `tools/web_search.py::search_web` на один запрос (только web-режим). |
| `max_chunks_per_source` | `int` (`ge=1`) | `3` | Потолок единиц (чанков) на ОДИН источник в `roles/extractor_critic.py` — источники, требующие больше, обрезаются с предупреждением (только web-режим). |

---

## 12. Методы класса

### 12.1. `ensure_dirs(self) -> None`

**Описание.** Создаёт (если не существуют) все рабочие директории:
`workdir`, `staging_dir`, `checkpoint_dir`, а также родительскую директорию
`db_path` (`self.db_path.parent.mkdir(...)` — не сам файл, только папку).
Все вызовы `mkdir(parents=True, exist_ok=True)`.

**Параметры:** нет.
**Возвращаемое значение:** `None`.
**Исключения:** `OSError` при проблемах ФС (права доступа) — не перехватывается.

**Кто вызывает:** `Orchestrator.__init__`, `cli/main.py::approve`/`index`.

### 12.2. `validate_free_only(self) -> None`

**Описание.** Жёсткая проверка режима FREE ONLY. Вызывается при старте
`Orchestrator` (и напрямую в конструкторах `GroqClient`/`OpenRouterClient`).
НИЧЕГО не "чинит" автоматически — если `free_only=False`, явно требует
подтверждения через отдельный флаг (которого в текущей реализации попросту
НЕТ), чтобы платный режим НИКОГДА не включался случайно/по умолчанию.

**Параметры:** нет.
**Возвращаемое значение:** `None` (если `self.free_only=True`).
**Исключения:** `RuntimeError` — если `self.free_only=False` ("FREE_ONLY=false
запрещено в текущей версии MVP. Система спроектирована работать
исключительно на бесплатном API. Платные провайдеры сознательно не
реализованы.").

---

## 13. Модульные функции (вне класса)

### 13.1. `get_settings() -> Settings`

**Описание.** Синглтон-фабрика — при первом вызове создаёт
`Settings()` (читает `.env`/переменные окружения) и кэширует в модульной
переменной `_settings`; при последующих вызовах возвращает уже созданный
объект БЕЗ пересоздания (важно: если переменные окружения изменились ПОСЛЕ
первого вызова в рамках одного процесса — это НЕ будет учтено без явного
сброса кэша).

**Параметры:** нет.
**Возвращаемое значение:** `Settings`.
**Исключения:** `pydantic.ValidationError`, если какие-то поля не проходят
валидацию (напр. `groq_calibration_max_ratio <= groq_calibration_min_ratio`).

### 13.2. `reset_settings_cache() -> None`

**Описание.** Сбрасывает закэшированный `_settings` в `None` — ТОЛЬКО для
тестов (чтобы каждый тест мог создать свежий `Settings(...)` с нужными
переопределениями через `get_settings()` заново, либо чтобы тесты,
создающие `Settings(...)` напрямую, не путались с синглтоном других тестов).

**Параметры:** нет.
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

---

## Сводная схема: кто читает какие поля `Settings`

```
Orchestrator.__init__          → free_only, workdir/staging_dir/checkpoint_dir/db_path (ensure_dirs),
                                  embedding_model, use_local_embeddings, llm_provider,
                                  groq_rpm_soft_limit/rpd_soft_limit (через budget_limits_for_provider),
                                  max_llm_calls_per_task, groq_extraction_model (через extraction_budget_limits)

llm/factory.py::create_llm_client         → llm_provider, groq_api_key (внутри GroqClient),
                                              openrouter_* (внутри _create_openrouter_router)
llm/factory.py::create_extraction_llm_client → groq_extraction_model/_tpm_limit, groq_share_limiter_when_same_model

llm/groq_client.py::GroqClient.__init__   → ВСЕ поля groq_* (см. docs_llm_cycle_part3 §7 — сводная таблица)

roles/vault_analyst.py       → dedup_high_threshold, dedup_low_threshold, default_notes_folder (косвенно, через Orchestrator)
roles/elaborator.py          → max_subpoints_per_generation_batch (через orchestrator/state_machine.py)
roles/critic.py              → max_critic_rounds (через orchestrator/state_machine.py)
roles/researcher.py          → max_search_results_per_query, max_sources_per_subtopic (только web-режим)
roles/extractor_critic.py    → max_chunks_per_source (только web-режим)

staging/commit.py            → allow_delete, git_enabled (через cli/main.py::approve)
vault/writer.py::VaultWriter → allow_delete

cli/main.py                  → все пути (staging_dir, checkpoint_dir, db_path, vault_path),
                                 draft_merge_mode, max_llm_calls_per_task (для вывода лимитов)

orchestrator/state_machine.py → research_mode, enable_draft_merging, language,
                                  KNOWLEDGE_MODE_FRONTMATTER_SOURCE (константа модуля, не Settings)
```

Документация по `config/settings.py` завершена.
