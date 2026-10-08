# Документация: `config/settings.py`

> Reference-док. Обзор пакета — `_index.md`. Единая точка конфигурации, реализована через `pydantic_settings.BaseSettings`: каждое поле читается из одноимённой (в верхнем регистре) переменной окружения или `.env`. Практическое руководство (минимальный набор, шаблон, типичные ошибки) — `env.md`.

## 0. `class Settings(BaseSettings)`

`model_config`: `env_file=".env"`, `env_file_encoding="utf-8"`, `extra="ignore"` (лишние переменные не вызывают ошибку, но и не действуют; так же игнорируются ключи удалённых полей из старых `.env`).

**Поля сложных типов** (`dict`, `list`) из окружения разбираются как JSON.

---

## 1. Провайдер LLM и режим исследования

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `llm_provider` | `str` | `"groq"` | Единственная точка переключения провайдера (`../llm/core.md §3.1`). Намеренно не `Literal`, чтобы новый провайдер не требовал менять тип. Допустимо: `"groq"`, `"openrouter"`. |
| `research_mode` | `Literal["web","knowledge"]` | `"knowledge"` | `knowledge` — Elaborator пишет разделы из знаний модели, без сети; заметки помечаются `frontmatter.source="model-knowledge"`. `web` — прежний путь; `Orchestrator.run()` его **блокирует** (`OrchestratorStopped`). |

---

## 2. OpenRouter (второй провайдер, подробно не документируется)

Поля есть в коде (`../CONTRIBUTING.md`): `openrouter_api_key`, `openrouter_base_url`, `openrouter_timeout_seconds` (60), `openrouter_selection_mode` (`auto`/`manual`), `openrouter_planning_models` и `openrouter_writing_models` (списки), `openrouter_planning_rpm_soft_limit`/`_rpd_soft_limit` (15/150), `openrouter_writing_rpm_soft_limit`/`_rpd_soft_limit` (15/150), `openrouter_check_key_before_call` (True), `openrouter_key_check_cache_seconds` (60.0; два последних зарезервированы). Валидатор `_split_csv_models` принимает строку через запятую, но `pydantic-settings` разбирает списки из окружения как JSON раньше валидатора, поэтому в `.env` надёжен JSON-массив. Группа writing обслуживает роль `synthesizer_write`, которая в текущем конвейере не вызывается (Writer удалён).

---

## 3. Groq (основной провайдер)

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_api_key` | `str` | `""` | Ключ Groq Free Tier. |
| `groq_model` | `str` | `"openai/gpt-oss-120b"` | Основная модель (planner, vault_dedup, folder_assignment, annotator). |
| `groq_timeout_seconds` | `int` (`ge=1`) | `60` | HTTP-таймаут запроса. |
| `groq_rpm_soft_limit` | `int` (`ge=1`) | `25` | Клиентский soft-throttle RPM (реальный ~30). |
| `groq_rpd_soft_limit` | `int` (`ge=1`) | `10000` | Ориентировочный дневной soft-лимит основного клиента. |
| `groq_tpm_limit` | `int` (`ge=1`) | `8000` | Явный TPM модели. Вынесен в поле, чтобы изменение реального лимита было видно в конфиге. |

### 3.1. Extraction-модель

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_extraction_model` | `str` | `"openai/gpt-oss-120b"` | Модель Elaborator. Прежний `groq/compound-mini` (заявленные 70K TPM) на практике маршрутизировался на модель со скрытым лимитом ≈12000 и давал массовые 429; `gpt-oss-120b` имеет честные 8000 TPM и strict `json_schema`. |
| `groq_extraction_tpm_limit` | `int` (`ge=1`) | `8000` | TPM extraction-модели. |
| `groq_extraction_rpd_soft_limit` | `int` (`ge=1`) | `900` | Дневной soft-лимит extraction-клиента (запас от 1000). |
| `groq_account_for_prompt_cache` | `bool` | `False` | Вычитать ли закэшированные токены из расхода бюджета. |

### 3.2. Adaptive rate limiting (`TokenRateLimiter`)

Значения по умолчанию соответствуют прежнему хардкоду в конструкторах (вынесение само по себе поведение не меняет), кроме `groq_limiter_safety_margin`.

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_limiter_safety_margin` | `float` (`gt=0, le=1`) | `0.95` | Доля реального TPM, которую разрешено использовать (дефолт аргумента конструктора лимитера 0.85, но Settings всегда передаёт это значение). Фиксированное минутное окно и точный подсчёт токенов через `tiktoken` позволили поднять значение. |
| `groq_margin_penalty_factor` | `float` (`gt=0, lt=1`) | `0.8` | Ужатие лимита за одно срабатывание реального 429 (0.8 = минус 20%). |
| `groq_margin_min_penalty` | `float` (`gt=0, le=1`) | `0.5` | Множитель штрафа не опускается ниже этой доли базового лимита. |
| `groq_margin_recovery_seconds` | `float` (`ge=0`) | `300.0` | За сколько секунд без 429 лимит восстанавливается. |
| `groq_margin_recovery_mode` | `Literal["step","linear"]` | `"linear"` | `step` — штраф держится всё окно, затем мгновенный сброс (эффект «берёт 2000–3000 из 8000» до 5 минут после одной 429). `linear` — плавный рост до 1.0 за то же окно. |
| `groq_share_limiter_when_same_model` | `bool` | `True` | При совпадении extraction-модели с основной оба клиента делят один `TokenRateLimiter` и калибратор (`../llm/core.md §3.3`): они физически бьют в один TPM Groq. |

### 3.3. Резерв вывода по роли

Раньше был единый хардкод 1500 на все роли, хотя ответы резко различаются по длине.

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_reserved_output_tokens_default` | `int` (`ge=1`) | `1500` | Резерв для ролей без записи в словаре ниже, пока нет наблюдений. Совпадает со старым хардкодом (первый вызов не менее безопасен, чем раньше). |
| `groq_reserved_output_min_tokens` | `int` (`ge=1`) | `300` | Пол калиброванного резерва. |
| `groq_output_calibration_ema_alpha` | `float` (`gt=0, le=1`) | `0.3` | EMA резерва вывода по роли (отдельно от `groq_calibration_ema_alpha`: другая величина и другая волатильность). |
| `groq_reserved_output_by_role` | `dict[str, int]` | `{"elaborator": 2500, "outline_planner": 3000, "annotator": 2000}` | Стартовый резерв ролей с заведомо длинным ответом. Приоритет: EMA роли → этот словарь → общий дефолт. `outline_planner` вызывается раз за задачу, калибратор её не выучит. Значения провизорные: уточняются по строке `Groq usage [...]` (отношение факт/резерв). В `.env` — JSON-объект, **заменяющий словарь целиком**. |

### 3.4. Калибровка оценки токенов входа

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_calibration_ema_alpha` | `float` (`gt=0, le=1`) | `0.3` | Вес нового наблюдения. |
| `groq_calibration_min_ratio` | `float` (`gt=0`) | `0.05` | Нижняя граница коэффициента. |
| `groq_calibration_max_ratio` | `float` (`gt=0`) | `1.5` | Верхняя граница (защита от одного нетипичного вызова). |

**`_max_ratio_above_min(cls, v, info)`** — `field_validator` поля `groq_calibration_max_ratio`: значение должно быть строго больше уже проверенного `groq_calibration_min_ratio`. **Исключение:** `ValueError`.

### 3.5. Подсчёт токенов

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_use_tiktoken` | `bool` | `True` | Считать токены через `tiktoken` для `openai/gpt-oss-20b/120b` (`../llm/groq_client.md §6.2`). `False` или недоступный `tiktoken` → эвристика по символам. |
| `groq_chars_per_token_cyrillic` | `float` (`gt=0`) | `2.3` | Символов на токен для кириллицы (эвристика). |
| `groq_chars_per_token_latin` | `float` (`gt=0`) | `4.0` | Для латиницы (эвристика). |
| `groq_cyrillic_ratio_threshold` | `float` (`0..1`) | `0.3` | Порог доли кириллицы (эвристика). |

Эвристика используется, только если `tiktoken` отключён или недоступен: для `openai/gpt-oss-20b/120b` `GroqClient` считает токены через `tiktoken` (`o200k_harmony`, запасная `o200k_base`).

---

## 4. Объединение заметок

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `enable_draft_merging` | `bool` | `False` | См. `../staging/draft_merge.md`. Ноль LLM-вызовов: пересборка готового текста после сборки заметок и до валидации. По умолчанию выключено: шаг и колбэк `merge_confirm_cb` пропускаются. |
| `draft_merge_mode` | `Literal["all","select"]` | `"all"` | `all` — все create-заметки сливаются в одну автоматически; `select` — выбор групп пользователем. Действует только при `enable_draft_merging=True`. |

---

## 5. Бюджет и FREE_ONLY

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `free_only` | `bool` | `True` | Только бесплатные провайдеры; проверяется `validate_free_only()` (§12.2). |
| `max_llm_calls_per_task` | `int` (`ge=1`) | `40` | Потолок вызовов на сессию, не зависит от провайдера (`../orchestrator/budget.md`). Повторы при сетевых сбоях и 429 заданы декораторами `tenacity` в клиентах, отдельной настройки нет. |

---

## 6. Vault

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `vault_path` | `Path` | `./vault_placeholder` | Абсолютный путь к реальному Vault. |
| `vault_name` | `str` | `"MyVault"` | Название; на логику не влияет. |
| `default_notes_folder` | `str` | `"Знания"` | Папка темы: `f"{default_notes_folder}/{slugify_filename(plan.topic_title)}"` (для новых заметок и MOC). |

---

## 7. Рабочие директории (вне Vault)

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `workdir` | `Path` | `./.obsidian_ai_kb` | Корень рабочих файлов. |
| `staging_dir` | `Path` | `./.obsidian_ai_kb/staging` | Staging-changeset'ы (`../staging/changeset.md`). |
| `db_path` | `Path` | `./.obsidian_ai_kb/vault_index.sqlite3` | SQLite-индекс (`../vault/db.md`). |
| `checkpoint_dir` | `Path` | `./.obsidian_ai_kb/checkpoints` | Чекпоинты v5 (`../staging/checkpoint.md`). Объявлен ниже, в блоке «Прочее». |

### 7.1. `_expand(cls, v)` — `field_validator`
`mode="before"` для `vault_path`, `workdir`, `staging_dir`, `db_path`: `Path(v).expanduser()`. **`checkpoint_dir` в список не входит**: `~` в нём не раскрывается.

---

## 8. Retrieval / dedup

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `use_local_embeddings` | `bool` | `True` | Попытка создать `LocalEmbedder` (`../tools/dedup.md`). |
| `embedding_model` | `str` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Модель эмбеддингов. |
| `dedup_high_threshold` | `float` (`0..1`) | `0.85` | Порог «точно дубликат». |
| `dedup_low_threshold` | `float` (`0..1`) | `0.55` | Порог «точно разные». |

---

## 9. Elaborator

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `max_subpoints_per_generation_batch` | `int` (`ge=1`) | `3` | Потолок подпунктов на один вызов Elaborator **по качеству**, не по токен-бюджету (`../llm/chunking.md §2`). Elaborator пишет полный markdown раздела, выходные токены растут пропорционально, поэтому 2–3. |

---

## 10. Critic (удалён)

Поле `max_critic_rounds` удалено вместе с ролью Critic. Ключ `MAX_CRITIC_ROUNDS` в старом `.env` игнорируется (`extra="ignore"`).

---

## 11. Прочее

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `language` | `str` | `"ru"` | Язык задач и заметок. |
| `allow_delete` | `bool` | `False` | Разрешает `VaultWriter.delete_note` реально удалять файлы. |
| `git_enabled` | `bool` | `False` | Включает `_git_commit` после `commit_changeset`. |
| `max_sources_per_subtopic` | `int` (`ge=1`) | `4` | Только web-режим. |
| `max_search_results_per_query` | `int` (`ge=1`) | `6` | Только web-режим. |
| `max_chunks_per_source` | `int` (`ge=1`) | `3` | Только web-режим. |
| `checkpoint_dir` | `Path` | см. §7 | Каталог чекпоинтов. |

---

## 12. Методы класса

### 12.1. `ensure_dirs(self) -> None`
Создаёт `workdir`, `staging_dir`, `checkpoint_dir` и родителя `db_path` (`mkdir(parents=True, exist_ok=True)`). **Исключения:** `OSError` не перехватывается. Вызывают `Orchestrator.__init__`, `cli/main.py::approve`/`index`.

### 12.2. `validate_free_only(self) -> None`
Жёсткая проверка FREE ONLY при старте `Orchestrator` и в конструкторах клиентов. Ничего не «чинит». **Исключения:** `RuntimeError`, если `free_only=False`.

---

## 13. Модульные функции

### 13.1. `get_settings() -> Settings`
Синглтон: при первом вызове создаёт `Settings()` и кэширует в `_settings`. **Исключения:** `pydantic.ValidationError` (например, `groq_calibration_max_ratio <= groq_calibration_min_ratio`).

### 13.2. `reset_settings_cache() -> None`
Сбрасывает кэш; только для тестов.

---

## Сводная схема: кто читает какие поля

```
Orchestrator.__init__         → free_only, пути (ensure_dirs), embedding_model, use_local_embeddings,
                                 llm_provider, max_llm_calls_per_task, groq_*_soft_limit, groq_extraction_*
Orchestrator.run              → research_mode, max_subpoints_per_generation_batch, default_notes_folder,
                                 dedup_high_threshold, dedup_low_threshold, enable_draft_merging,
                                 allow_delete, language

llm/factory.py                → llm_provider, groq_api_key (в GroqClient), groq_extraction_model/_tpm_limit,
                                 groq_share_limiter_when_same_model, openrouter_* (для openrouter)
llm/groq_client.py::GroqClient → все groq_* (в т.ч. groq_reserved_output_by_role и groq_use_tiktoken)

roles/vault_analyst.py        → пороги dedup и default_notes_folder (через Orchestrator)
roles/elaborator.py           → max_subpoints_per_generation_batch (через Orchestrator)
roles/annotator.py            → напрямую ничего (константы модуля)

staging/commit.py             → allow_delete, git_enabled (через cli/main.py::approve)
vault/writer.py               → allow_delete
cli/main.py                   → пути, vault_path, draft_merge_mode, max_llm_calls_per_task
```

Документация по `config/settings.py` завершена. Обзор пакета — `_index.md`.