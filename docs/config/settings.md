# Документация: `config/settings.py`

> Reference-док. Обзор пакета — `_index.md`. Единая точка конфигурации
> системы, реализована через `pydantic_settings.BaseSettings` — каждое
> поле автоматически читается из одноимённой (в верхнем регистре)
> переменной окружения или `.env`, без ручного парсинга.

## 0. `class Settings(BaseSettings)`

Единственный класс модуля. `model_config` задаёт `env_file=".env"`,
`env_file_encoding="utf-8"`, `extra="ignore"` (лишние переменные окружения
не вызывают ошибку валидации).

---

## 1. Провайдер LLM и режим исследования

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `llm_provider` | `str` | `"groq"` | Единственная точка переключения провайдера (`../llm/core.md §3.1`). Намеренно НЕ `Literal` — чтобы добавление нового провайдера не требовало менять тип поля. Допустимые значения в MVP: `"groq"`, `"openrouter"` (второй провайдер намеренно не документируется в этом заходе, см. `../CONTRIBUTING.md`). |
| `research_mode` | `Literal["web", "knowledge"]` | `"knowledge"` | `"web"` — прежний пайплайн (веб-поиск + fetch + Extractor/Critic по реальному тексту, даёт проверяемые `source_refs`, но зависит от сети). `"knowledge"` (дефолт) — `roles/elaborator.py` (`../roles/elaborator.md`) генерирует evidence из знаний модели, без сетевого I/O, заметки помечаются `frontmatter.source="model-knowledge"`. **Важно:** `Orchestrator.run()` (`../orchestrator/state_machine.md §4`) СЕЙЧАС явно блокирует `research_mode="web"` — миграция на новую структуру плана не завершена. |

---

## 2. OpenRouter (второй провайдер — намеренно не документируется подробно)

Поля существуют в коде (`openrouter_api_key`, `openrouter_base_url`,
`openrouter_selection_mode`, `openrouter_planning_models`,
`openrouter_writing_models` и связанные soft-лимиты/кэш-проверки) — по
решению, зафиксированному в `../CONTRIBUTING.md`, второй провайдер
(`llm/router.py`, `llm/openrouter_client.py`) вне текущего фокуса MVP и
здесь не расписывается по полям. Если провайдер снова станет активным
путём — документировать по тем же правилам, что и остальной `Settings`.

---

## 3. Groq (основной провайдер)

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_api_key` | `str` | `""` | Ключ Groq Free Tier. |
| `groq_model` | `str` | `"openai/gpt-oss-120b"` | Основная модель (planning/critic/vault_dedup/folder_assignment/synthesizer_write). |
| `groq_timeout_seconds` | `int` (`ge=1`) | `60` | HTTP-таймаут одного запроса. |
| `groq_rpm_soft_limit` | `int` (`ge=1`) | `25` | Клиентский soft-throttle RPM — реальный лимит free tier выше (~30 RPM), берётся с запасом, чтобы не упираться в TPM на длинных промптах. |
| `groq_rpd_soft_limit` | `int` (`ge=1`) | `10000` | Ориентировочный дневной soft-лимит. |
| `groq_tpm_limit` | `int` (`ge=1`) | `8000` | Явный TPM реальной модели — вынесено явным полем (а не только class-level fallback `GroqClient.DEFAULT_TPM_LIMIT`, `../llm/groq_client.md §6`), чтобы изменение реального лимита Groq (уже случалось для других моделей проекта) было видно прямо в конфиге при ревью. |

### 3.1. Groq: extraction-модель

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_extraction_model` | `str` | `"openai/gpt-oss-120b"` | Модель для `roles/elaborator.py` (самая частая по числу вызовов роль). Ранее стоял `groq/compound-mini` (заявленный TPM=70000), но на практике он маршрутизирует запросы на `llama-3.3-70b-versatile` со СКРЫТЫМ отдельным лимитом (наблюдалось `Limit=12000` в реальном логе 429) — заявленные 70K не отражали реальный бюджет. `openai/gpt-oss-120b` имеет скромный, но ЧЕСТНЫЙ TPM=8000 и официально поддерживает strict `json_schema`. |
| `groq_extraction_tpm_limit` | `int` (`ge=1`) | `8000` | TPM-лимит extraction-модели. |
| `groq_extraction_rpd_soft_limit` | `int` (`ge=1`) | `900` | Дневной soft-лимит extraction-клиента — запас от реального лимита 1000. |
| `groq_account_for_prompt_cache` | `bool` | `False` | Учитывать ли закэшированные Groq токены при расчёте эффективного расхода TPM-бюджета. |

### 3.2. Groq: adaptive rate limiting (`TokenRateLimiter`)

Значения по умолчанию совпадают с тем, что раньше было захардкожено в
конструкторах `TokenRateLimiter`/`TokenEstimateCalibrator` (`../llm/groq_client.md
§3, §5`) — вынесение в конфиг само по себе не меняет поведение по
умолчанию, только даёт возможность потюнить без правки кода.

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_limiter_safety_margin` | `float` (`gt=0.0, le=1.0`) | `0.85` | Множитель запаса от реального TPM-лимита (используем не более 85%). |
| `groq_margin_penalty_factor` | `float` (`gt=0.0, lt=1.0`) | `0.8` | На какую долю ужимается эффективный лимит за ОДНО срабатывание реального 429 (`0.8` = минус 20%). |
| `groq_margin_min_penalty` | `float` (`gt=0.0, le=1.0`) | `0.5` | Множитель штрафа не уходит ниже этой доли от базового лимита даже при серии 429 подряд. |
| `groq_margin_recovery_seconds` | `float` (`ge=0.0`) | `300.0` | Через сколько секунд без новых 429 лимит полностью восстанавливается. |
| `groq_margin_recovery_mode` | `Literal["step", "linear"]` | `"linear"` | `"step"` — margin_penalty остаётся ПОЛНОСТЬЮ ужатым все `groq_margin_recovery_seconds`, затем мгновенно скачет на 100% (прежнее поведение — давало эффект "система берёт 2000-3000 из 8000" до 5 минут после ОДНОЙ 429). `"linear"` (НОВЫЙ ДЕФОЛТ) — margin_penalty линейно растёт от значения на момент штрафа до 1.0 в течение того же окна. |
| `groq_share_limiter_when_same_model` | `bool` | `True` | Если модель extraction-клиента совпадает с основной — extraction-клиент переиспользует `TokenRateLimiter`/`TokenEstimateCalibrator` ОСНОВНОГО клиента (`../llm/core.md §3.3`), т.к. оба физически делят ОДИН TPM Groq API. Дефолт `True` — раздельные лимитеры при совпадающей модели ВСЕГДА источник риска голодания, а не осознанный trade-off. |

### 3.3. Groq: калибровка output-резерва (по роли)

Раньше был единственный хардкод `GroqClient.RESERVED_OUTPUT_TOKENS=1500` для
ВСЕХ ролей одинаково — но роли резко отличаются по длине ответа (`critic`/
`vault_dedup`/`folder_assignment` отвечают компактным JSON, `synthesizer_write`
пишет целую заметку).

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_reserved_output_tokens_default` | `int` (`ge=1`) | `1500` | Используется, пока по роли ЕЩЁ НЕТ ни одного наблюдения (холодный старт). Значение сознательно совпадает со старым хардкодом. |
| `groq_reserved_output_min_tokens` | `int` (`ge=1`) | `300` | Нижняя граница калиброванного резерва — даже если роль ЗА ВСЮ ИСТОРИЮ задачи отвечала коротко, резерв не опускается ниже этого пола. |
| `groq_output_calibration_ema_alpha` | `float` (`gt=0.0, le=1.0`) | `0.3` | EMA-коэффициент калибровки OUTPUT-резерва по роли — ОТДЕЛЬНЫЙ параметр от `groq_calibration_ema_alpha` (тот калибрует ВХОДНОЙ `prompt_tokens` в виде соотношения, этот — саму величину `completion_tokens` в токенах). |

### 3.4. Groq: калибровка оценки токенов по роли (prompt-ratio)

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_calibration_ema_alpha` | `float` (`gt=0.0, le=1.0`) | `0.3` | Вес нового наблюдения в EMA. |
| `groq_calibration_min_ratio` | `float` (`gt=0.0`) | `0.05` | Нижняя граница калиброванного коэффициента. |
| `groq_calibration_max_ratio` | `float` (`gt=0.0`) | `1.5` | Верхняя граница — защита от того, чтобы ОДИН нетипичный вызов не увёл коэффициент в крайность на весь остаток задачи. |

#### `_max_ratio_above_min(cls, v, info)` — `field_validator`

Валидатор поля `groq_calibration_max_ratio`: проверяет, что оно строго
больше уже провалидированного `groq_calibration_min_ratio`
(`info.data.get(...)`).

**Возвращаемое значение:** `float` — `v` без изменений, если проверка
пройдена. **Исключения:** `ValueError`, если
`min_ratio is not None and v <= min_ratio`.

### 3.5. Groq: точность "наивной" оценки токенов по символам

Раньше — хардкоженные константы прямо в `llm/common.py` (`../llm/core.md
§2.5`), не настраиваемые и не протестированные на реальных промптах
проекта. Значения по умолчанию НЕ изменены относительно старого хардкода —
сама по себе эта правка добавляет только настраиваемость.

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `groq_chars_per_token_cyrillic` | `float` (`gt=0.0`) | `2.3` | Символов на токен для кириллического текста. |
| `groq_chars_per_token_latin` | `float` (`gt=0.0`) | `4.0` | Символов на токен для латинского/смешанного текста. |
| `groq_cyrillic_ratio_threshold` | `float` (`ge=0.0, le=1.0`) | `0.3` | Порог доли кириллицы, выше которого текст считается "кириллическим". |

---

## 4. Объединение заметок в конце workflow

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `enable_draft_merging` | `bool` | `True` | См. `../staging/draft_merge.md` — ноль LLM-вызовов, чистая пересборка уже написанного текста ПОСЛЕ Writer+Critic, ПЕРЕД validation/staging. |
| `draft_merge_mode` | `Literal["all", "select"]` | `"all"` | `"all"` — ВСЕ написанные заметки (`action=create`) сливаются в одну итоговую АВТОМАТИЧЕСКИ. `"select"` — пользователь сам выбирает, какие заметки объединить. |

---

## 5. Общий бюджет и FREE_ONLY

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `free_only` | `bool` | `True` | Жёсткий флаг: только бесплатные провайдеры. Проверяется `validate_free_only()` (§12.2). |
| `max_llm_calls_per_task` | `int` (`ge=1`) | `40` | Общий бюджет вызовов на задачу — НЕ зависит от активного провайдера (`../orchestrator/budget.md §3`). |
| `max_llm_retries` | `int` (`ge=0`) | `3` | Зарезервированное поле — фактические retry настроены на уровне `tenacity`-декораторов конкретных клиентов (`../llm/groq_client.md §6.8`). |

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
| `staging_dir` | `Path` | `Path("./.obsidian_ai_kb/staging")` | Директория staging-changeset'ов (`../staging/changeset.md`). |
| `db_path` | `Path` | `Path("./.obsidian_ai_kb/vault_index.sqlite3")` | Файл SQLite-индекса Vault (`../vault/db.md`). |
| `checkpoint_dir` | `Path` | `Path("./.obsidian_ai_kb/checkpoints")` | Директория чекпоинтов задач (`../staging/checkpoint.md`). |

### 7.1. `_expand(cls, v)` — `field_validator`

`mode="before"`-валидатор для `vault_path`, `workdir`, `staging_dir`,
`db_path`: разворачивает `~` в домашнюю директорию (`Path(v).expanduser()`).

---

## 8. Retrieval / dedup

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `use_local_embeddings` | `bool` | `True` | Включает попытку создания `tools/dedup.py::LocalEmbedder` (`../tools/dedup.md`). |
| `embedding_model` | `str` | `"sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"` | Имя модели `sentence-transformers` для локальных эмбеддингов. |
| `dedup_high_threshold` | `float` (`ge=0.0, le=1.0`) | `0.85` | Порог "точно дубликат" для `classify_similarity`. |
| `dedup_low_threshold` | `float` (`ge=0.0, le=1.0`) | `0.55` | Порог "точно разные". |

---

## 9. Synthesizer / Writer

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `max_subpoints_per_generation_batch` | `int` (`ge=1`) | `6` | Потолок подпунктов заметки на ОДИН вызов Elaborator ПО КАЧЕСТВУ, не по токен-бюджету (`../llm/chunking.md §2`). |

---

## 10. Critic

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `max_critic_rounds` | `int` (`ge=0`) | `1` | Сколько раз Writer имеет право переписать заметку по замечаниям Critic-а в рамках ОДНОЙ задачи — жёсткий bounded retry (`../roles/critic.md`). `0` — критик полностью выключен. |

---

## 11. Прочее

| Поле | Тип | Дефолт | Назначение |
|---|---|---|---|
| `language` | `str` | `"ru"` | Язык интерфейса/заметок по умолчанию. |
| `allow_delete` | `bool` | `False` | Разрешает ли `../vault/writer.md::VaultWriter.delete_note` реально удалять файлы. |
| `git_enabled` | `bool` | `False` | Включает `../staging/commit.md::_git_commit` после каждого `commit_changeset`. |
| `max_sources_per_subtopic` | `int` (`ge=1`) | `4` | Потолок отобранных источников на подтему (только web-режим). |
| `max_search_results_per_query` | `int` (`ge=1`) | `6` | Сколько результатов запрашивать у веб-поиска (только web-режим). |
| `max_chunks_per_source` | `int` (`ge=1`) | `3` | Потолок единиц (чанков) на ОДИН источник (только web-режим). |

---

## 12. Методы класса

### 12.1. `ensure_dirs(self) -> None`

Создаёт (если не существуют) все рабочие директории: `workdir`,
`staging_dir`, `checkpoint_dir`, а также родительскую директорию `db_path`.
Все вызовы `mkdir(parents=True, exist_ok=True)`.

**Исключения:** `OSError` при проблемах ФС — не перехватывается.

**Кто вызывает:** `Orchestrator.__init__` (`../orchestrator/state_machine.md §4`),
`cli/main.py::approve`/`index` (`../cli/main.md`).

### 12.2. `validate_free_only(self) -> None`

Жёсткая проверка режима FREE ONLY. Вызывается при старте `Orchestrator` (и
напрямую в конструкторах `GroqClient`/`OpenRouterClient`). НИЧЕГО не
"чинит" автоматически — если `free_only=False`, поднимает исключение,
чтобы платный режим НИКОГДА не включался случайно/по умолчанию.

**Исключения:** `RuntimeError` — если `self.free_only=False` ("FREE_ONLY=false
запрещено в текущей версии MVP. Система спроектирована работать
исключительно на бесплатном API. Платные провайдеры сознательно не
реализованы.").

---

## 13. Модульные функции (вне класса)

### 13.1. `get_settings() -> Settings`

Синглтон-фабрика — при первом вызове создаёт `Settings()` (читает
`.env`/переменные окружения) и кэширует в модульной переменной `_settings`;
при последующих вызовах возвращает уже созданный объект БЕЗ пересоздания.

**Исключения:** `pydantic.ValidationError`, если какие-то поля не проходят
валидацию (напр. `groq_calibration_max_ratio <= groq_calibration_min_ratio`).

### 13.2. `reset_settings_cache() -> None`

Сбрасывает закэшированный `_settings` в `None` — ТОЛЬКО для тестов.

---

## Сводная схема: кто читает какие поля `Settings`

```
Orchestrator.__init__          → free_only, workdir/staging_dir/checkpoint_dir/db_path (ensure_dirs),
                                  embedding_model, use_local_embeddings, llm_provider,
                                  groq_rpm_soft_limit/rpd_soft_limit (через budget_limits_for_provider),
                                  max_llm_calls_per_task, groq_extraction_model

llm/factory.py::create_llm_client         → llm_provider, groq_api_key (внутри GroqClient)
llm/factory.py::create_extraction_llm_client → groq_extraction_model/_tpm_limit, groq_share_limiter_when_same_model

llm/groq_client.py::GroqClient.__init__   → ВСЕ поля groq_* (см. ../llm/groq_client.md §6, §7 — сводная таблица там же)

roles/vault_analyst.py       → dedup_high_threshold, dedup_low_threshold, default_notes_folder (косвенно, через Orchestrator)
roles/elaborator.py          → max_subpoints_per_generation_batch (через orchestrator/state_machine.py)
roles/critic.py              → max_critic_rounds (через orchestrator/state_machine.py)

staging/commit.py            → allow_delete, git_enabled (через cli/main.py::approve)
vault/writer.py::VaultWriter → allow_delete

cli/main.py                  → все пути (staging_dir, checkpoint_dir, db_path, vault_path),
                                 draft_merge_mode, max_llm_calls_per_task (для вывода лимитов)

orchestrator/state_machine.py → research_mode, enable_draft_merging, language
```

Документация по `config/settings.py` завершена. Обзор пакета — `_index.md`.
