# Настройка `.env`: какие параметры указать и за что они отвечают

> Практическое руководство. Полный справочник по полям (типы, валидаторы, кто читает) — `settings.md`, обзор пакета — `_index.md`.

## 0. Как это работает

- `Settings` (`config/settings.py`) читает переменные окружения и файл `.env` из **текущей рабочей директории** (откуда запускается `python -m cli.main ...`). Файл — UTF-8.
- Имя переменной = имя поля в верхнем регистре: `groq_api_key` → `GROQ_API_KEY`. Регистр значения не важен для имён; для булевых допустимы `true/false/1/0`.
- Неизвестные переменные игнорируются (`extra="ignore"`), опечатка в имени **не вызовет ошибки** — параметр просто останется по умолчанию.
- Пути: `~` раскрывается автоматически (`VAULT_PATH`, `WORKDIR`, `STAGING_DIR`, `DB_PATH`). Пути в Windows лучше писать с прямыми слэшами или в кавычках.
- Значение из реальной переменной окружения приоритетнее значения в `.env`.
- `.env` содержит ключ API и **не должен попадать в Git** (должен быть в `.gitignore`).

## 1. Минимальный `.env` (Groq)

```env
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
VAULT_PATH=/абсолютный/путь/к/вашему/Vault
```

Этого достаточно для запуска; остальное имеет рабочие значения по умолчанию. Ключ — https://console.groq.com/keys (карта не нужна).

## 2. Обязательные и основные параметры

| Переменная | По умолчанию | За что отвечает | Когда менять |
|---|---|---|---|
| `LLM_PROVIDER` | `groq` | Провайдер LLM: `groq` или `openrouter`. | Для OpenRouter — см. §5. |
| `GROQ_API_KEY` | пусто | Ключ Groq. Без него при `LLM_PROVIDER=groq` старт падает с `RuntimeError`. | **Всегда** указать. |
| `VAULT_PATH` | `./vault_placeholder` | Абсолютный путь к реальному Obsidian Vault. Если оставить значение по умолчанию, система будет читать несуществующую папку. | **Всегда** указать. |
| `FREE_ONLY` | `true` | Жёсткий запрет платных провайдеров. Значение `false` приводит к `RuntimeError` при старте (платный режим не реализован). | Не менять. |
| `RESEARCH_MODE` | `knowledge` | Источник материала: `knowledge` — из знаний модели (заметки помечаются `source: model-knowledge`); `web` — веб-поиск. **`web` сейчас блокируется в `Orchestrator.run()`** (`OrchestratorStopped`). | Оставить `knowledge`. |
| `LANGUAGE` | `ru` | Язык задач и заметок. | Для запросов не на русском. |
| `MAX_LLM_CALLS_PER_TASK` | `40` | Потолок LLM-вызовов на одну **сессию** (`resume` открывает новый лимит). При достижении задача останавливается с сохранением прогресса. | Увеличить для больших тем (много заметок × критик). |

## 3. Vault и рабочие директории

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `VAULT_NAME` | `MyVault` | Название Vault; только для вывода, на логику не влияет. |
| `DEFAULT_NOTES_FOLDER` | `Знания` | Базовая папка для новых тем. Итоговая папка задачи: `<DEFAULT_NOTES_FOLDER>/<название темы>`. |
| `WORKDIR` | `./.obsidian_ai_kb` | Корень рабочих файлов проекта (вне Vault). |
| `STAGING_DIR` | `./.obsidian_ai_kb/staging` | Где лежат предлагаемые изменения до `approve`. |
| `DB_PATH` | `./.obsidian_ai_kb/vault_index.sqlite3` | SQLite-индекс Vault. |
| `CHECKPOINT_DIR` | `./.obsidian_ai_kb/checkpoints` | Чекпоинты для `resume`. |

> Рабочие директории **должны быть вне Vault** (или в скрытой папке — `.`-папки индексатор пропускает). Относительные пути считаются от текущей директории запуска, поэтому запускайте команды всегда из одного места либо задайте абсолютные пути.

## 4. Безопасность записи

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `ALLOW_DELETE` | `false` | Разрешает удаление файлов Vault. В MVP список удалений всегда пуст; оставьте `false`. |
| `GIT_ENABLED` | `false` | После `approve` делает `git add -A` + `git commit` в `VAULT_PATH`. Нужен git-репозиторий; сбой git не ломает запись. |

## 5. OpenRouter (второй провайдер)

Используется, если `LLM_PROVIDER=openrouter`. Остальные `GROQ_*` при этом игнорируются.

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `OPENROUTER_API_KEY` | пусто | Ключ https://openrouter.ai/keys. Обязателен. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Базовый URL. |
| `OPENROUTER_TIMEOUT_SECONDS` | `60` | HTTP-таймаут запроса. |
| `OPENROUTER_SELECTION_MODE` | `auto` | `auto` — при сбое модели перейти к следующей в списке; `manual` — только первая модель, при сбое остановка. |
| `OPENROUTER_PLANNING_MODELS` | Nemotron 3 Ultra, GLM 5.2, Inkling Small (все `:free`) | Модели по приоритету для всех ролей, кроме написания заметок. |
| `OPENROUTER_WRITING_MODELS` | Gemma 4 26B, Gemma 4 31B (`:free`) | Модели только для роли `synthesizer_write` (текст заметок). |
| `OPENROUTER_PLANNING_RPM_SOFT_LIMIT` / `..._RPD_SOFT_LIMIT` | `15` / `150` | Мягкие лимиты запросов в минуту/день для planning-группы. |
| `OPENROUTER_WRITING_RPM_SOFT_LIMIT` / `..._RPD_SOFT_LIMIT` | `15` / `150` | То же для writing-группы. |
| `OPENROUTER_CHECK_KEY_BEFORE_CALL` | `true` | Зарезервировано; в показанном коде клиента проверки ключа нет. |
| `OPENROUTER_KEY_CHECK_CACHE_SECONDS` | `60` | Аналогично, зарезервировано. |

**Формат списков моделей.** В коде есть валидатор, принимающий строку через запятую, но `pydantic-settings` для полей-списков сначала пробует разобрать значение из окружения как JSON и при неудаче выдаёт ошибку. Надёжный вариант для `.env` — JSON-массив:

```env
OPENROUTER_PLANNING_MODELS=["nvidia/nemotron-3-ultra-550b-a55b:free","z-ai/glm-5.2:free"]
```

(Запись через запятую без скобок проверьте на своей версии `pydantic-settings`; при ошибке используйте JSON.) Идентификаторы моделей сверяйте с каталогом OpenRouter — они меняются.

## 6. Groq: модели и лимиты

Значения по умолчанию соответствуют free tier `openai/gpt-oss-120b` (30 RPM, 1000 RPD, 8000 TPM, 200 000 TPD). При смене модели сверяйте лимиты с консолью Groq.

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Основная модель (планирование, критик, дедупликация, папки, запись). Модели `openai/gpt-oss-20b/120b` используют строгую JSON Schema, остальные — `json_object`. |
| `GROQ_TIMEOUT_SECONDS` | `60` | Таймаут одного запроса. |
| `GROQ_TPM_LIMIT` | `8000` | Реальный лимит токенов в минуту основной модели. Обновляйте вручную, если Groq изменит лимит. |
| `GROQ_RPM_SOFT_LIMIT` | `25` | Мягкий локальный лимит запросов в минуту (реальный ~30). |
| `GROQ_RPD_SOFT_LIMIT` | `10000` | Мягкий дневной лимит основного клиента; счётчик живёт только в памяти процесса. |
| `GROQ_EXTRACTION_MODEL` | `openai/gpt-oss-120b` | Модель для роли Elaborator (самая частая по числу вызовов). |
| `GROQ_EXTRACTION_TPM_LIMIT` | `8000` | TPM-лимит extraction-модели. |
| `GROQ_EXTRACTION_RPD_SOFT_LIMIT` | `900` | Дневной soft-лимит extraction-клиента (реальный 1000). |
| `GROQ_SHARE_LIMITER_WHEN_SAME_MODEL` | `true` | Если extraction-модель совпадает с основной, оба клиента делят один TPM-лимитер, как и физически один лимит Groq. |

## 7. Groq: тонкая настройка TPM-лимитера (обычно не трогать)

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `GROQ_LIMITER_SAFETY_MARGIN` | `0.85` | Доля реального TPM, которую разрешено использовать. |
| `GROQ_MARGIN_PENALTY_FACTOR` | `0.8` | Во сколько раз ужимается лимит после реального 429. |
| `GROQ_MARGIN_MIN_PENALTY` | `0.5` | Ниже какой доли базового лимита штраф не опускается. |
| `GROQ_MARGIN_RECOVERY_SECONDS` | `300` | За сколько секунд без 429 лимит восстанавливается. |
| `GROQ_MARGIN_RECOVERY_MODE` | `linear` | `linear` — плавное восстановление; `step` — ступенька в конце окна. |
| `GROQ_ACCOUNT_FOR_PROMPT_CACHE` | `false` | Вычитать ли закэшированные Groq токены из расхода бюджета. |
| `GROQ_RESERVED_OUTPUT_TOKENS_DEFAULT` | `1500` | Резерв под ответ, пока по роли нет наблюдений. |
| `GROQ_RESERVED_OUTPUT_MIN_TOKENS` | `300` | Нижняя граница калиброванного резерва под ответ. |
| `GROQ_OUTPUT_CALIBRATION_EMA_ALPHA` | `0.3` | Скорость адаптации резерва под ответ. |
| `GROQ_CALIBRATION_EMA_ALPHA` | `0.3` | Скорость адаптации оценки токенов промпта. |
| `GROQ_CALIBRATION_MIN_RATIO` / `GROQ_CALIBRATION_MAX_RATIO` | `0.05` / `1.5` | Границы калибровочного коэффициента. **Max должен быть строго больше Min**, иначе `ValidationError`. |
| `GROQ_CHARS_PER_TOKEN_CYRILLIC` | `2.3` | Символов на токен для кириллицы (наивная оценка). |
| `GROQ_CHARS_PER_TOKEN_LATIN` | `4.0` | То же для латиницы. |
| `GROQ_CYRILLIC_RATIO_THRESHOLD` | `0.3` | Доля кириллицы, выше которой текст считается русским. |

## 8. Качество генерации и объединение заметок

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `MAX_SUBPOINTS_PER_GENERATION_BATCH` | `6` | Сколько подпунктов раскрывается за один вызов Elaborator. Меньше — глубже, но больше вызовов. |
| `MAX_CRITIC_ROUNDS` | `1` | Сколько раз Writer может переписать заметку по замечаниям критика. `0` — критик выключен. Каждый раунд стоит до 2 вызовов на заметку. |
| `ENABLE_DRAFT_MERGING` | `true` | Включает шаг объединения готовых заметок (без LLM). |
| `DRAFT_MERGE_MODE` | `all` | `all` — все новые заметки сливаются в одну автоматически; `select` — вы выбираете группы вручную. |

## 9. Поиск дубликатов и эмбеддинги

| Переменная | По умолчанию | За что отвечает |
|---|---|---|
| `USE_LOCAL_EMBEDDINGS` | `true` | Локальные эмбеддинги (`sentence-transformers`). При недоступности модели система тихо переходит на BM25. Первая загрузка ~470 МБ. |
| `EMBEDDING_MODEL` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | Модель эмбеддингов. |
| `DEDUP_HIGH_THRESHOLD` | `0.85` | Схожесть, с которой заметка считается дубликатом (дополняем существующую). |
| `DEDUP_LOW_THRESHOLD` | `0.55` | Ниже — разные концепции. Между порогами — один LLM-вызов «та же концепция?». |

## 10. Параметры web-режима

Действуют только при `RESEARCH_MODE=web` (сейчас заблокирован): `MAX_SOURCES_PER_SUBTOPIC` (4), `MAX_SEARCH_RESULTS_PER_QUERY` (6), `MAX_CHUNKS_PER_SOURCE` (3). `MAX_LLM_RETRIES` (3) объявлен, но в клиентах не используется — повторы заданы в коде.

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
MAX_CRITIC_ROUNDS=1
ENABLE_DRAFT_MERGING=true
DRAFT_MERGE_MODE=all

# --- Vault ---
DEFAULT_NOTES_FOLDER=Знания

# --- Groq ---
GROQ_MODEL=openai/gpt-oss-120b
GROQ_EXTRACTION_MODEL=openai/gpt-oss-120b
GROQ_TPM_LIMIT=8000

# --- дубликаты ---
USE_LOCAL_EMBEDDINGS=true
DEDUP_HIGH_THRESHOLD=0.85
DEDUP_LOW_THRESHOLD=0.55
```

## 12. Типичные проблемы

| Симптом | Причина |
|---|---|
| `GROQ_API_KEY не задан` | Пустой ключ или `.env` не найден (запуск не из той директории). |
| `FREE_ONLY=false запрещено` | Платный режим не реализован — верните `true`. |
| `OrchestratorStopped` про `RESEARCH_MODE=web` | Web-режим временно заблокирован — используйте `knowledge`. |
| Изменение в `.env` «не действует» | Опечатка в имени (неизвестные ключи игнорируются) либо такая же переменная задана в окружении. |
| Ошибка разбора `OPENROUTER_*_MODELS` | Список моделей нужно писать JSON-массивом (см. §5). |
| Индекс пуст / заметки не находятся | `VAULT_PATH` не указан или указывает на `./vault_placeholder`. |
| Задача часто останавливается по лимиту | Увеличьте `MAX_LLM_CALLS_PER_TASK`, уменьшите `MAX_CRITIC_ROUNDS` или число заметок в плане; TPM-лимит Groq (8000) — самое узкое место. |

Документация по настройке `.env` завершена. Полный справочник по полям — `settings.md`, обзор пакета — `_index.md`.
