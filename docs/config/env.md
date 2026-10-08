# Настройка `.env`: какие параметры указать и за что они отвечают

> Практическое руководство (файла кода нет). Полный справочник по полям (типы, валидаторы, кто читает) — `settings.md`, обзор пакета — `_index.md`.

## 0. Как это работает

- `Settings` (`config/settings.py`) читает переменные окружения и файл `.env` из **текущей рабочей директории** (откуда запускается `python -m cli.main ...`). Файл — UTF-8.
- Имя переменной = имя поля в верхнем регистре: `groq_api_key` → `GROQ_API_KEY`. Для булевых допустимы `true/false/1/0`.
- Неизвестные переменные игнорируются (`extra="ignore"`): опечатка в имени **не вызовет ошибки**, параметр просто останется по умолчанию. Так же игнорируются ключи удалённых полей (`MAX_CRITIC_ROUNDS`, `MAX_LLM_RETRIES`) из старых `.env`.
- `~` раскрывается автоматически только в `VAULT_PATH`, `WORKDIR`, `STAGING_DIR`, `DB_PATH`. В `CHECKPOINT_DIR` не раскрывается: задавайте абсолютный путь или путь без `~`. Пути в Windows лучше писать с прямыми слэшами.
- Значение из реальной переменной окружения приоритетнее значения в `.env`.
- `.env` содержит ключ API и **не должен попадать в систему контроля версий** (держите его вне публикуемых файлов).
- Поля-словари и списки (`GROQ_RESERVED_OUTPUT_BY_ROLE`, `OPENROUTER_*_MODELS`) задаются **JSON** (§5, §6).

## 1. Минимальный `.env` (Groq)

```env
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
VAULT_PATH=/абсолютный/путь/к/вашему/Vault
```

Остальное имеет рабочие значения по умолчанию. Ключ — https://console.groq.com/keys (карта не нужна).

## 2. Основные параметры

| Переменная | По умолчанию | За что отвечает | Когда менять |
|---|---|---|---|
| `LLM_PROVIDER` | `groq` | Провайдер: `groq` или `openrouter`. | Для OpenRouter — §5. |
| `GROQ_API_KEY` | пусто | Ключ Groq. Без него при `LLM_PROVIDER=groq` старт падает с `RuntimeError`. | **Всегда** указать. |
| `VAULT_PATH` | `./vault_placeholder` | Абсолютный путь к реальному Obsidian Vault. С дефолтом система читает несуществующую папку. | **Всегда** указать. |
| `FREE_ONLY` | `true` | Жёсткий запрет платных провайдеров. `false` → `RuntimeError` при старте. | Не менять. |
| `RESEARCH_MODE` | `knowledge` | `knowledge` — разделы пишутся из знаний модели (заметки помечаются `source: model-knowledge`); `web` — **заблокирован** в `Orchestrator.run()` (`OrchestratorStopped`). | Оставить `knowledge`. |
| `LANGUAGE` | `ru` | Язык задач и заметок. | Для запросов не на русском. |
| `MAX_LLM_CALLS_PER_TASK` | `40` | Потолок LLM-вызовов на **сессию** (`resume` открывает новый лимит). При достижении задача останавливается с сохранением прогресса. Типичная задача: ≈10–13 вызовов. | Увеличить для больших тем. |

## 3. Vault и рабочие директории

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `VAULT_NAME` | `MyVault` | Название Vault; только для вывода. |
| `DEFAULT_NOTES_FOLDER` | `Знания` | База для новых тем. Папка задачи: `<DEFAULT_NOTES_FOLDER>/<название темы>`; туда же кладётся MOC. |
| `WORKDIR` | `./.obsidian_ai_kb` | Корень рабочих файлов (вне Vault). |
| `STAGING_DIR` | `./.obsidian_ai_kb/staging` | Предлагаемые изменения до `approve`. |
| `DB_PATH` | `./.obsidian_ai_kb/vault_index.sqlite3` | SQLite-индекс Vault. |
| `CHECKPOINT_DIR` | `./.obsidian_ai_kb/checkpoints` | Чекпоинты для `resume` (версия формата 5). Остаются и после задачи с ошибками валидации. |

> Рабочие директории **должны быть вне Vault** (или в скрытой папке: `.`-папки индексатор пропускает). Относительные пути считаются от текущей директории запуска, поэтому запускайте команды всегда из одного места или задайте абсолютные пути.

## 4. Безопасность записи

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `ALLOW_DELETE` | `false` | Разрешает удаление файлов Vault. В MVP список удалений всегда пуст; оставьте `false`. |
| `GIT_ENABLED` | `false` | После `approve` выполняет автоматическую фиксацию изменений в `VAULT_PATH`. Нужен репозиторий; сбой не ломает запись. |

## 5. OpenRouter (второй провайдер)

Используется при `LLM_PROVIDER=openrouter`. `GROQ_*` при этом игнорируются. Провайдер не документируется подробно (`../CONTRIBUTING.md`).

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `OPENROUTER_API_KEY` | пусто | Ключ https://openrouter.ai/keys. Обязателен. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Базовый URL. |
| `OPENROUTER_TIMEOUT_SECONDS` | `60` | HTTP-таймаут. |
| `OPENROUTER_SELECTION_MODE` | `auto` | `auto` — при сбое модели переход к следующей в списке; `manual` — только первая, при сбое остановка. |
| `OPENROUTER_PLANNING_MODELS` | Nemotron 3 Ultra, GLM 5.2, Inkling Small (`:free`) | Модели по приоритету для всех ролей, кроме написания текста. |
| `OPENROUTER_WRITING_MODELS` | Gemma 4 26B, Gemma 4 31B (`:free`) | Модели для роли `synthesizer_write`. ⚠ Эта роль в текущем конвейере больше не вызывается (Writer удалён), так что группа фактически не используется. |
| `OPENROUTER_PLANNING_RPM_SOFT_LIMIT` / `..._RPD_SOFT_LIMIT` | `15` / `150` | Мягкие лимиты planning-группы. |
| `OPENROUTER_WRITING_RPM_SOFT_LIMIT` / `..._RPD_SOFT_LIMIT` | `15` / `150` | То же для writing-группы. |
| `OPENROUTER_CHECK_KEY_BEFORE_CALL`, `OPENROUTER_KEY_CHECK_CACHE_SECONDS` | `true` / `60` | Зарезервировано: в коде клиента проверки ключа нет. |

**Формат списков моделей.** Валидатор в коде принимает строку через запятую, но `pydantic-settings` для полей-списков сначала разбирает значение из окружения как JSON. Надёжный вариант:

```env
OPENROUTER_PLANNING_MODELS=["nvidia/nemotron-3-ultra-550b-a55b:free","z-ai/glm-5.2:free"]
```

Идентификаторы моделей сверяйте с каталогом OpenRouter: они меняются.

## 6. Groq: модели и лимиты

Значения по умолчанию соответствуют free tier `openai/gpt-oss-120b` (30 RPM, 1000 RPD, 8000 TPM, 200 000 TPD). При смене модели сверяйте лимиты с консолью Groq.

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Основная модель (planner, vault_dedup, folder_assignment, annotator). `openai/gpt-oss-20b/120b` используют строгую JSON Schema, считают токены через `tiktoken` и принимают параметры рассуждений; остальные — `json_object`. |
| `GROQ_TIMEOUT_SECONDS` | `60` | Таймаут одного запроса. |
| `GROQ_TPM_LIMIT` | `8000` | Реальный TPM основной модели. Обновляйте вручную, если Groq изменит лимит. |
| `GROQ_USE_TIKTOKEN` | `true` | Считать токены через `tiktoken` (для gpt-oss). `false` → эвристика по символам. |
| `GROQ_RPM_SOFT_LIMIT` | `25` | Мягкий локальный лимит запросов в минуту (реальный ~30). |
| `GROQ_RPD_SOFT_LIMIT` | `10000` | Мягкий дневной лимит основного клиента; счётчик только в памяти процесса. |
| `GROQ_EXTRACTION_MODEL` | `openai/gpt-oss-120b` | Модель Elaborator (самая частая по числу вызовов). |
| `GROQ_EXTRACTION_TPM_LIMIT` | `8000` | TPM extraction-модели. |
| `GROQ_EXTRACTION_RPD_SOFT_LIMIT` | `900` | Дневной soft-лимит extraction-клиента (реальный 1000). |
| `GROQ_SHARE_LIMITER_WHEN_SAME_MODEL` | `true` | Если extraction-модель совпадает с основной, оба клиента делят один TPM-лимитер и калибратор (физически один лимит Groq). |
| `GROQ_RESERVED_OUTPUT_BY_ROLE` | `{"elaborator": 2500, "outline_planner": 3000, "annotator": 2000}` | Стартовый резерв токенов вывода по ролям (JSON-объект; значение **заменяет словарь целиком**, не дополняет). После нескольких вызовов роли резерв подстраивается по факту. |

## 7. Groq: тонкая настройка лимитера (обычно не трогать)

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `GROQ_LIMITER_SAFETY_MARGIN` | `0.95` | Доля реального TPM, которую разрешено использовать. |
| `GROQ_MARGIN_PENALTY_FACTOR` | `0.8` | Во сколько раз ужимается лимит после реального 429. |
| `GROQ_MARGIN_MIN_PENALTY` | `0.5` | Ниже какой доли базового лимита штраф не опускается. |
| `GROQ_MARGIN_RECOVERY_SECONDS` | `300` | За сколько секунд без 429 лимит восстанавливается. |
| `GROQ_MARGIN_RECOVERY_MODE` | `linear` | `linear` — плавно; `step` — ступенька в конце окна. |
| `GROQ_ACCOUNT_FOR_PROMPT_CACHE` | `false` | Вычитать ли закэшированные Groq токены из расхода бюджета. |
| `GROQ_RESERVED_OUTPUT_TOKENS_DEFAULT` | `1500` | Резерв под ответ для ролей без записи в `GROQ_RESERVED_OUTPUT_BY_ROLE`, пока нет наблюдений. |
| `GROQ_RESERVED_OUTPUT_MIN_TOKENS` | `300` | Нижняя граница калиброванного резерва под ответ. |
| `GROQ_OUTPUT_CALIBRATION_EMA_ALPHA` | `0.3` | Скорость адаптации резерва под ответ. |
| `GROQ_CALIBRATION_EMA_ALPHA` | `0.3` | Скорость адаптации оценки токенов промпта. |
| `GROQ_CALIBRATION_MIN_RATIO` / `GROQ_CALIBRATION_MAX_RATIO` | `0.05` / `1.5` | Границы калибровочного коэффициента. **Max строго больше Min**, иначе `ValidationError`. |
| `GROQ_CHARS_PER_TOKEN_CYRILLIC` / `_LATIN` | `2.3` / `4.0` | Символов на токен; запасная эвристика, если `tiktoken` отключён или недоступен. |
| `GROQ_CYRILLIC_RATIO_THRESHOLD` | `0.3` | Доля кириллицы, выше которой текст считается русским (для эвристики). |

## 8. Качество генерации и объединение заметок

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `MAX_SUBPOINTS_PER_GENERATION_BATCH` | `3` | Сколько подпунктов Elaborator пишет за один вызов. Меньше — глубже разделы, но больше вызовов. |
| `ENABLE_DRAFT_MERGING` | `false` | Включает шаг объединения готовых заметок (без LLM). |
| `DRAFT_MERGE_MODE` | `all` | `all` — все новые заметки сливаются в одну автоматически; `select` — вы выбираете группы. Действует только при `ENABLE_DRAFT_MERGING=true`. |

Устаревшие ключи `MAX_CRITIC_ROUNDS` и `MAX_LLM_RETRIES` из старых `.env` игнорируются.

## 9. Поиск дубликатов и эмбеддинги

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `USE_LOCAL_EMBEDDINGS` | `true` | Локальные эмбеддинги (`sentence-transformers`). При недоступности модели система тихо переходит на BM25. Первая загрузка ~470 МБ. |
| `EMBEDDING_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Модель эмбеддингов. |
| `DEDUP_HIGH_THRESHOLD` | `0.85` | Схожесть, с которой заметка считается дубликатом (дополняем существующую, `action=update`). |
| `DEDUP_LOW_THRESHOLD` | `0.55` | Ниже — разные концепции. Между порогами — один LLM-вызов «та же концепция?». |

## 10. Параметры web-режима

Действуют только при `RESEARCH_MODE=web` (заблокирован): `MAX_SOURCES_PER_SUBTOPIC` (4), `MAX_SEARCH_RESULTS_PER_QUERY` (6), `MAX_CHUNKS_PER_SOURCE` (3).

## 11. Готовый шаблон `.env`

```env
# --- обязательное ---
LLM_PROVIDER=groq
GROQ_API_KEY=
VAULT_PATH=

# --- безопасность ---
FREE_ONLY=true
ALLOW_DELETE=false
GIT_ENABLED=false

# --- режим и бюджет ---
RESEARCH_MODE=knowledge
LANGUAGE=ru
MAX_LLM_CALLS_PER_TASK=40
MAX_SUBPOINTS_PER_GENERATION_BATCH=3
ENABLE_DRAFT_MERGING=false
DRAFT_MERGE_MODE=all

# --- Vault ---
DEFAULT_NOTES_FOLDER=Знания

# --- Groq ---
GROQ_MODEL=openai/gpt-oss-120b
GROQ_EXTRACTION_MODEL=openai/gpt-oss-120b
GROQ_TPM_LIMIT=8000
GROQ_USE_TIKTOKEN=true

# --- дубликаты ---
USE_LOCAL_EMBEDDINGS=true
DEDUP_HIGH_THRESHOLD=0.85
DEDUP_LOW_THRESHOLD=0.55
```

## 12. Типичные проблемы

| Симптом | Причина |
|---|---|
| `GROQ_API_KEY не задан` | Пустой ключ или `.env` не найден (запуск не из той директории). |
| `FREE_ONLY=false запрещено` | Платный режим не реализован: верните `true`. |
| `OrchestratorStopped` про `RESEARCH_MODE=web` | Web-режим заблокирован: используйте `knowledge`. |
| Изменение в `.env` «не действует» | Опечатка в имени (неизвестные ключи игнорируются) либо переменная задана в окружении. |
| Ошибка разбора `OPENROUTER_*_MODELS` или `GROQ_RESERVED_OUTPUT_BY_ROLE` | Значение должно быть JSON (массив или объект). |
| Индекс пуст / заметки не находятся | `VAULT_PATH` не указан или указывает на `./vault_placeholder`. |
| Задача часто останавливается по лимиту | Увеличьте `MAX_LLM_CALLS_PER_TASK`, уменьшите число заметок в плане; TPM Groq (8000) — самое узкое место. |
| Warning `note_language_mismatch` | Модель написала заметку не на русском: проверьте текст перед `approve`. |
| Warning `path_autofixed` | Заметке добавлен суффикс « (2)» из-за конфликта пути (`../validation/autofix.md`): проверьте имя перед `approve`. |
| `ask` завершился с ошибками валидации | Чекпоинт сохранён: устраните причину и выполните `resume <task_id>`, LLM-вызовы не тратятся. |

Документация по настройке `.env` завершена. Полный справочник по полям — `settings.md`, обзор пакета — `_index.md`.