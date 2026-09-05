# MVP AI-системы управления знаниями для Obsidian
## Phase 1 — Архитектура + Phase 0 — GitHub Research
### (код ещё не пишется — документ на утверждение)

---

## 0. Резюме подхода

Система = **один Orchestrator + набор "ролей" (по сути — промптов/режимов одной и той же Gemini-модели) + детерминированные локальные инструменты + staging + human approval**.

Ключевая идея, которую стоит проговорить сразу: **не все 9 "ролей" из ТЗ — это отдельные вызовы LLM**. Часть из них (Extractor+Critic, Vault Analyst, Validator) либо объединяются в один Gemini-вызов, либо вообще не требуют LLM (дедупликация, YAML-валидация, поиск backlinks — это чистый код). Это прямое следствие требования "минимизировать количество Gemini calls" и "LLM не должен проверять то, что может проверить код".

---

## 1. Архитектура верхнего уровня

```
┌──────────────────────────────────────────────────────────────────┐
│                              USER (CLI)                          │
└───────────────────────────────┬──────────────────────────────────┘
                                 │ natural language query
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATOR                              │
│  state machine (не LLM) + бюджет Gemini-вызовов + error handling │
└───────────────────────────────┬──────────────────────────────────┘
      │                         │                         │
      ▼                         ▼                         ▼
┌───────────┐           ┌───────────────┐         ┌───────────────┐
│  GEMINI    │◄─────────┤   ROLE LAYER   ├────────►│  LOCAL TOOLS  │
│  CLIENT    │  prompts  │ (промпт-режимы│  calls  │ (детерминизм) │
│ (единств.  │           │  одной модели)│         │               │
│  LLM)      │           └───────────────┘         └───────┬───────┘
└───────────┘                                              │
                                                             ▼
                                                  ┌──────────────────┐
                                                  │  VAULT INDEX      │
                                                  │  (SQLite, локально)│
                                                  └────────┬───────────┘
                                                             │
                                                             ▼
                                                  ┌──────────────────┐
                                                  │  STAGING AREA     │
                                                  │ (diff, не Vault)  │
                                                  └────────┬───────────┘
                                                             │  user approve
                                                             ▼
                                                  ┌──────────────────┐
                                                  │  OBSIDIAN VAULT   │
                                                  │  (файловая система)│
                                                  └──────────────────┘
```

Важно: **Gemini — reasoning engine, локальный код — execution layer, Vault — knowledge layer**. Ни один "агент" не имеет собственного независимого цикла — все вызовы инициирует Orchestrator по жёсткому конечному автомату (finite state machine), а не автономный agent loop.

---

## 2. Роли: что реально требует Gemini, а что — нет

| # | Роль из ТЗ | Нужен ли отдельный Gemini call? | Реализация |
|---|---|---|---|
| 1 | **Orchestrator** | Нет | Чистый Python: state machine, бюджет вызовов, роутинг |
| 2 | **Planner** | Да, 1 вызов | Gemini: query → структура темы, подтемы, типы источников |
| 3 | **Researcher** | Частично | Поиск (DDGS, бесплатно, без LLM) + 1 вызов Gemini на ранжирование/отбор релевантных источников из уже найденных сниппетов |
| 4 | **Extractor** | Да, объединён с Critic | 1 вызов на пачку источников: факты + оценка достоверности одновременно (structured JSON output) |
| 5 | **Critic / Fact-Checker** | Объединён с Extractor (см. выше) | Противоречия между источниками детектятся тем же вызовом; сравнение с самим собой на дубли — кодом |
| 6 | **Vault Analyst** | Нет (LLM не нужен для 90% работы) | Локальный индекс (SQLite) + деterministic поиск (BM25/keyword) + embedding-опция (см. §6). Только финальное "это та же концепция или нет?" на пограничных случаях — 1 маленький Gemini-вызов на кандидата-дубликат |
| 7 | **Synthesizer** | Да, 1 вызов | Собирает Plan + Evidence + ExistingNotes → финальная структура заметок (какие новые, какие дополнить) |
| 8 | **Obsidian Writer** | Да, тот же вызов, что Synthesizer, или следующий | Markdown + frontmatter + wikilinks — можно сгенерировать в том же structured-output вызове, что и синтез, чтобы не плодить вызовы |
| 9 | **Linker / Knowledge Graph** | Нет | Код: сопоставление тегов/заголовков с существующими заметками, простановка `[[wikilinks]]`, обновление backlink-графа в SQLite |
| 10 | **Validator** | Нет | Чистый код: YAML lint, markdown lint, проверка существования файлов по wikilink, дубликаты по хэшу/similarity, "не удалять без подтверждения" |
| 11 | **Human Approval / Staging** | Нет | CLI diff-view + подтверждение |

**Итог: на один пользовательский запрос уровня "изучи тему X" ожидается ~4–6 вызовов Gemini**, а не 9+. Это и есть ответ на п.10 ТЗ ("минимизировать количество Gemini calls").

Расшифровка минимального набора вызовов на типовой запрос:
1. **Planner call** — структура темы, подтемы, ключевые концепции, что искать.
2. **Researcher-selection call** — из сырых результатов поиска выбрать релевантные и оценить качество источника (можно объединить с (3), если объём небольшой).
3. **Extractor+Critic call(ы)** — по 1 на источник или батчами по 2–3 источника (ограничение по контексту и по необходимости привязки evidence→source). Это самая "тяжёлая" по числу вызовов роль — здесь основной риск упереться в RPM/RPD лимит.
4. **Vault-dedup call(ы)** — только для пограничных случаев, где локальный keyword/embedding retrieval даёт неоднозначный результат (similarity в "серой зоне").
5. **Synthesizer+Writer call** — 1 вызов,结构ированный вывод: список заметок (новых/дополняемых), их markdown-тело, frontmatter, теги, связи.

---

## 3. Данные между этапами (структурированные объекты)

Все объекты — Pydantic-модели (валидируются кодом, не LLM). Между ролями летают **не текстовые простыни**, а эти структуры:

```
Task            — исходный запрос + распознанный intent + язык
Plan            — тема, список подтем/концепций, типы нужных источников
SourceCandidate — url, title, snippet, релевантность (score)
Evidence        — text, concept, source_id, confidence, contradicts[]
ExistingNote    — path, title, frontmatter, tags, links, similarity_score
DraftNote       — title, slug, folder, frontmatter, body_md, tags, links_out, source_refs
Relationship     — from_note, to_note, type (wikilink/tag/backlink)
ValidationReport — ok: bool, errors[], warnings[]
StagingChangeset — creates[], updates[], deletes[] (deletes всегда пусты в MVP по умолчанию)
Status           — этап, потрачено Gemini calls, оставшийся бюджет, ошибки
```

Все они сериализуются в JSON и сохраняются построчно в SQLite/файлы staging — это даёт "бесплатный" resume/retry: если Gemini упал на шаге 4, не нужно пересчитывать шаги 1–3.

---

## 4. Workflow одного запроса (пошагово)

Пример: *«Изучи тему Retrieval-Augmented Generation»*

```
1. User → CLI → Task
2. Orchestrator: normalize query, определить язык (ru), режим (research_and_populate)
3. [GEMINI #1] Planner: Task → Plan (подтемы: embeddings, vector DB, retrieval, 
   reranking, chunking, generation, evaluation)
4. Researcher (код): для каждой подтемы → DDGS free search → SourceCandidate[]
5. [GEMINI #2] Researcher-selection: отфильтровать/ранжировать SourceCandidate[] 
   (можно merge с шагом 3, если бюджет поджимает)
6. Researcher (код): web_fetch по отобранным URL → сырой текст
7. [GEMINI #3..N] Extractor+Critic: сырой текст источника → Evidence[] 
   (факты + confidence + противоречия), батчами
8. Vault Analyst (код): по каждой Plan-концепции → локальный retrieval 
   (BM25/embeddings) по SQLite-индексу → ExistingNote[] кандидаты
9. [GEMINI #N+1, опционально] Vault-dedup: только для неоднозначных 
   ExistingNote-кандидатов — "это та же концепция, что и Evidence X?"
10. [GEMINI #N+2] Synthesizer+Writer: Plan + Evidence + ExistingNote[] → 
    DraftNote[] (что создать, что дополнить, конкретный markdown/frontmatter/tags)
11. Linker (код): DraftNote[] × ExistingNote[] → Relationship[] (wikilinks, backlinks)
12. Validator (код): YAML/Markdown lint, dead-link check, dup-check, 
    no-delete-check → ValidationReport
13. Staging (код): StagingChangeset записывается в staging/ (не в Vault!)
14. CLI: показать diff пользователю (новые файлы, изменяемые файлы, новые связи)
15. User: approve / reject / edit
16. [если approve] Commit (код): StagingChangeset → реальный Vault, 
    опционально git commit
17. Orchestrator: финальный отчёт (Status)
```

**Узкие места:**
- Шаг 7 (Extractor+Critic) — линейно растёт с числом источников → главный риск по RPM/RPD.
- Шаг 6 (web_fetch) — не Gemini-лимит, но может быть медленным/нестабильным (сайты блокируют скрейпинг).
- Шаг 9 — опционален, включается только если keyword/embedding score в "серой зоне" (например, 0.4–0.7 similarity) — иначе дубли решаются кодом.

---

## 5. Как работает Gemini Free Tier и как под него подстроиться

По данным на 2026 год (актуальные цифры нужно всегда проверять в Google AI Studio → Rate Limits, т.к. они меняются):

- Лимиты считаются по **трём измерениям одновременно**: RPM (запросов/мин), TPM (токенов/мин), RPD (запросов/день) — превышение любого из них даёт `429`.
- Free tier сейчас практически ограничен моделями **Flash / Flash-Lite** (Pro-модели преимущественно выведены из бесплатного тира или имеют очень низкий RPD).
- Ориентировочно: Flash ~10 RPM / ~250–1500 RPD (в зависимости от версии модели), Flash-Lite ~15 RPM / выше RPD. Это **не гарантированные цифры** — система должна читать реальный лимит с ошибки API, а не хардкодить.
- RPD сбрасывается в полночь по Pacific Time.
- Лимиты — на проект/API-key, не суммируются между ключами (создание второго ключа "для обхода лимита" — прямое нарушение вашего же требования FREE_ONLY / никакого искусственного скейлинга, поэтому в MVP не делаем).

### Как система с этим работает
1. **GeminiClient** — единственная точка входа к LLM. Все вызовы идут только через него.
2. Перед каждым вызовом — проверка локального счётчика (`calls_today`, `calls_this_minute`) относительно конфигурируемых `MAX_CALLS_PER_TASK` и известного/наблюдаемого лимита.
3. При `429` — exponential backoff + jitter, ограниченное число retry (например, 3), затем — **hard stop**, не переход на платный tier.
4. При явном исчерпании RPD — задача останавливается с понятным сообщением пользователю: *"Достигнут дневной лимит бесплатного Gemini API. Прогресс сохранён в staging, можно продолжить завтра"* — ключевое: **прогресс не теряется**, потому что каждый этап персистится (см. §3).
5. Глобальный **бюджет вызовов на задачу** (`MAX_GEMINI_CALLS_PER_TASK`, например 15) — жёсткий потолок независимо от лимитов API, чтобы не было "тихого" разрастания количества вызовов при большой теме.

---

## 6. Vault как Knowledge Graph: индекс и retrieval

### 6.1 Локальный индекс (SQLite)
Таблицы (минимум для MVP):
```sql
notes(path, title, content_hash, raw_content, summary, created_at, updated_at)
frontmatter(note_path, key, value)          -- YAML разложен построчно
tags(note_path, tag)
links(source_path, target_path, link_type)  -- wikilink / tag-link
backlinks — вычисляется как обратный запрос к links, отдельной таблицы не нужно
embeddings(note_path, vector BLOB)          -- опционально, см. ниже
```
Индекс строится/обновляется **инкрементально** по content_hash — не весь Vault пересканируется на каждый запрос.

### 6.2 Retrieval без отправки всего Vault в LLM
1. Planner выдаёт список концепций/подтем.
2. Для каждой — локальный поиск:
   - **Базовый MVP-вариант**: keyword/BM25 поиск по title+tags+summary (библиотека `rank-bm25`, чистый Python, без сети).
   - **Опциональное улучшение (не в MVP-0, но архитектурно предусмотрено)**: локальные эмбеддинги (например, через `sentence-transformers` офлайн-модель) для семантического сходства — сближается с тем, что уже делает плагин *Smart Connections*, но своей локальной реализацией, без стороннего API.
3. В Gemini передаются **только**: заголовки + summary + frontmatter топ-N кандидатов (никогда — весь текст всех заметок, никогда — весь Vault).
4. Если top-1 similarity выше явного порога (например, >0.85) — дубликат детектируется кодом, LLM не нужен. Если в "серой зоне" — 1 маленький Gemini-вызов "это та же концепция?".

### 6.3 Vault как граф
`notes` + `links` + `tags` в SQLite уже формируют граф. Linker обновляет рёбра при каждом commit. Backlinks — просто обратный join, отдельно не хранится (чтобы не рассинхронизировать).

---

## 7. Staging — обязательный слой перед записью в Vault

```
DraftNote[] + Relationship[] + ValidationReport
        │
        ▼
staging/<task_id>/
   ├── changeset.json         # что именно предлагается: create/update/delete
   ├── notes/*.md              # реальные файлы-кандидаты (не в Vault!)
   ├── diff_summary.md         # человекочитаемый diff
   └── validation_report.json
        │
        ▼
   CLI показывает diff → user approve
        │
        ▼ (только после approve)
   Commit: копирование staging/notes/* → Vault, git commit (если включен)
```

Правила:
- **Delete запрещён по умолчанию** (`ALLOW_DELETE=false` в конфиге) — Validator блокирует любой changeset с deletes, если флаг не включён явно.
- Commit — атомарная операция с возможностью rollback (если Git включён — просто `git revert`; если нет — staging хранит snapshot "before" для отдельных изменяемых файлов).
- Ничего не пишется в реальный Vault до явного `approve` от пользователя в CLI.

---

## 8. Технологии и почему

| Компонент | Технология | Почему |
|---|---|---|
| Язык | Python 3.11+ | Богатая экосистема для LLM/файлов/CLI, легко расширяется |
| LLM client | `google-genai` (официальный Python SDK Gemini) | Официальная поддержка, structured output (JSON schema), встроенный retry-friendly интерфейс |
| Structured output | Pydantic v2 + Gemini JSON-mode | Строгая валидация между ролями без "угадывания" текста |
| CLI | `Typer` или `rich` + `prompt_toolkit` | Простой, приятный CLI без веб-сложности; `rich` даёт красивый diff-вывод |
| Индекс Vault | SQLite (`sqlite3` stdlib) | Ноль внешних зависимостей, бесплатно, локально, ACID |
| Markdown-парсинг | `python-frontmatter` + `markdown-it-py` | Зрелые, MIT-лицензия, разделяют YAML frontmatter и тело |
| YAML-валидация | `PyYAML` (safe_load) | Стандарт |
| Веб-поиск (бесплатно) | `ddgs` (бывш. duckduckgo-search) | Без API-ключа, MIT, активно поддерживается |
| Fetch страниц | `httpx` + `trafilatura`/`readability-lxml` | Извлечение чистого текста из HTML без лишнего шума |
| Retry/backoff | `tenacity` | Готовая, проверенная реализация exponential backoff + jitter |
| Локальные эмбеддинги (опционально, не в MVP-0) | `sentence-transformers` (офлайн модель) | Бесплатно, без сети, для будущего semantic retrieval |
| Git-интеграция | `GitPython` или прямой subprocess `git` | Опциональный versioning Vault-изменений |
| Конфиг | `pydantic-settings` + `.env` (`python-dotenv`) | Разделение конфига и логики, типизация, `.env` в `.gitignore` |

**Почему не LangGraph/CrewAI как каркас для MVP**: у нас **линейный, детерминированный workflow с явным HITL-шагом**, а не сложный ветвящийся multi-agent граф. LangGraph отлично подходит, когда нужны параллельные ветки, динамическая маршрутизация, персистентные чекпоинты "из коробки" — это ценно и вероятно понадобится в v2 (когда появится, например, параллельный research по подтемам). Для MVP-0 берём **паттерн** LangGraph HITL (`interrupt()`/`Command(resume=...)`) как референс для дизайна approval-шага, но реализуем его простым Python-кодом (staging + CLI confirm), не подключая саму библиотеку — меньше зависимостей, проще отладка, тот же результат для single-user CLI. Если позже понадобится: veб-UI, параллелизация, персистентность через checkpointer — тогда подключение LangGraph станет оправданным (архитектура это не блокирует, т.к. Orchestrator уже оперирует явным состоянием).

---

## 9. Структура проекта (предложение)

```
obsidian-ai-kb/
├── config/
│   ├── settings.py          # pydantic-settings: пути, лимиты, FREE_ONLY, язык
│   └── .env.example
├── orchestrator/
│   ├── state_machine.py     # шаги workflow, переходы, error handling
│   └── budget.py            # контроль числа Gemini calls / rate limit
├── gemini/
│   ├── client.py            # единственная точка вызова API, retry/backoff
│   ├── schemas.py           # Pydantic-схемы structured output для каждой роли
│   └── prompts/             # промпты по ролям (planner.md, extractor.md, ...)
├── roles/                   # "роли" = промпт-режимы + бизнес-логика вокруг вызова
│   ├── planner.py
│   ├── researcher.py
│   ├── extractor_critic.py
│   ├── vault_analyst.py
│   ├── synthesizer_writer.py
├── tools/                   # skills — переиспользуемые детерминированные функции
│   ├── web_search.py        # обёртка над ddgs
│   ├── web_fetch.py         # httpx + trafilatura
│   ├── markdown_tools.py    # генерация/парсинг md, frontmatter
│   ├── yaml_tools.py
│   └── dedup.py             # BM25/embedding similarity
├── vault/
│   ├── index.py             # построение/обновление SQLite-индекса
│   ├── db.py                # схема и доступ к SQLite
│   ├── reader.py             # чтение реального Vault
│   └── writer.py             # запись (используется только Commit-этапом)
├── retrieval/
│   └── search.py             # локальный поиск по индексу (BM25 / embeddings)
├── staging/
│   ├── changeset.py          # StagingChangeset модель + сериализация
│   └── diff.py                # человекочитаемый diff для CLI
├── validation/
│   ├── markdown_validator.py
│   ├── yaml_validator.py
│   ├── link_validator.py
│   └── dedup_validator.py
├── storage/
│   └── models.py              # общие Pydantic-модели (Task, Plan, Evidence, ...)
├── cli/
│   └── main.py                 # entrypoint, progress, diff view, approve prompt
├── tests/
│   ├── test_vault/            # тестовый Vault fixtures
│   ├── test_validation/
│   ├── test_staging/
│   └── test_gemini_mocked/    # моки Gemini для тестов без реальных вызовов
├── docs/
│   └── architecture.md         # этот документ
├── .env                         # не в Git
├── .gitignore
├── pyproject.toml
└── README.md
```

Отличие от "примера" в ТЗ: роли и skills разделены явно (`roles/` — что решает LLM, `tools/` — что решает код), плюс отдельный `retrieval/` и `validation/`, потому что это два блока с наибольшим количеством детерминированной логики без LLM.

---

## 10. Что можно сделать полностью бесплатно

- **LLM**: Gemini Free Tier (Flash/Flash-Lite) — единственный обязательный внешний сервис.
- **Веб-поиск**: DuckDuckGo через `ddgs`, без ключа.
- **Fetch/извлечение контента**: `httpx` + `trafilatura` — бесплатно, локально.
- **Индекс/поиск по Vault**: SQLite + BM25 — 100% локально.
- **Опциональные эмбеддинги**: локальная модель (`sentence-transformers`, офлайн) — без API.
- **Git-версионирование** (опционально): бесплатно, локально.
- **Валидация**: весь Markdown/YAML/dedup/link-check — чистый код, бесплатно.

Единственное потенциальное "платно" — если пользователь захочет более качественный внешний поиск (Tavily и т.п.) в будущем — это **явно исключено** из MVP согласно ограничению.

---

## 11. Ограничения MVP (честно)

1. **Качество бесплатного веб-поиска** (DuckDuckGo без ключа) ниже, чем у платных research-API (Tavily/Exa) — возможны менее релевантные источники, скрейпинг может блокироваться некоторыми сайтами.
2. **RPM/RPD Gemini Free** — на "богатую" тему с 8+ подтемами и множеством источников можно упереться в дневной лимит; задача должна уметь **приостанавливаться и продолжаться на следующий день** без потери прогресса (это заложено в дизайне staging/persist).
3. **Semantic dedup в MVP-0 базовый** (BM25, keyword) — возможны пропущенные смысловые дубликаты с другой формулировкой; полноценные локальные эмбеддинги — следующая итерация.
4. **Single-user, локальный, без UI** — CLI, без веб-интерфейса, без multi-user.
5. **Нет автоматического отката произвольной сложности** — rollback реализован просто (git revert / snapshot "before"), не полноценная транзакционная СУБД поверх файловой системы.
6. **Извлечение фактов зависит от качества Flash-модели** — младшие модели дают более простые/ошибочные extraction, отсюда важность Critic-шага и человеческого approve перед записью.

---

## 12. Phase 0 — GitHub Research (открытые репозитории/паттерны)

### 12.1 Итоговая таблица

| Роль/Skill | Repository | License | Статус/активность | LLM/провайдер в оригинале | Что взять | Прямое использование кода? |
|---|---|---|---|---|---|---|
| Orchestrator / HITL pattern | `langchain-ai/langgraph` (docs/patterns `interrupt()`/`Command`) | MIT | Активно поддерживается, mainstream | LLM-agnostic | **Паттерн** pause/resume/approve state — реализуем сами без зависимости от библиотеки | Нет, только паттерн |
| Orchestrator / HITL пример (шаблон) | `KirtiJha/langgraph-interrupt-workflow-template` | MIT (уточнить в репо) | Свежий, актуальный шаблон, демонстрирует именно tool-approval и multi-step HITL на Gemini | Provider-agnostic, есть демо на Gemini | Референс структуры interrupt/approve/redirect для нашего staging-approve шага | Нет, только паттерн |
| Researcher | `assafelovic/gpt-researcher` | Apache-2.0 | Зрелый, крупный, активно поддерживается, поддерживает "any LLM provider" и локальные документы | Multi-provider (OpenAI по умолчанию, но абстрагирован) | Идея разделения plan→search→scrape→aggregate; **не тянем весь фреймворк** (у него свой веб-сервер, Docker, много зависимостей) — берём только архитектурную идею "многократный поиск снижает шанс единичной ошибки" | Нет (слишком тяжёлый для наших нужд), только паттерн |
| Researcher / free search tool | `ddgs` (duckduckgo-search, PyPI/GitHub) | MIT | Активно поддерживается, широко используется как fallback-поиск в agent-проектах | — (не LLM) | **Прямое использование библиотеки** как инструмента поиска | Да, прямая зависимость |
| Researcher / free web fetch | `nickclyde/duckduckgo-mcp-server` (как референс интерфейса) | MIT | 900+ stars, простой tool-interface (search + fetch_content) | LLM-agnostic (MCP) | Интерфейс двух инструментов "search" / "fetch_content" как образец для `tools/web_search.py` и `tools/web_fetch.py` | Нет, только интерфейс-паттерн (у нас не MCP, а прямой Python-вызов) |
| Vault Analyst / Obsidian access | `coddingtonbear/obsidian-local-rest-api` | MIT | Очень активно поддерживается (релизы в 2026), популярный, стандарт де-факто | — | Если решим не читать файлы Vault напрямую с диска, а через REST — этот плагин даёт готовый, безопасный API-слой (в т.ч. свой встроенный MCP-сервер) | Используем как **внешнюю зависимость (Obsidian-плагин)**, не копируем код |
| Vault Analyst / semantic search референс | `brianpetro/obsidian-smart-connections` | **Source-available, "Smart Plugins License"** (модифицированный MIT + noncompete, ранее был GPLv3) | Очень популярен (4300+ stars), активно развивается, НО лицензия изменилась в конце 2025 с GPLv3 на кастомную с ограничениями на конкурирующие продукты | Локальные эмбеддинги, опционально сторонние API (в т.ч. Gemini) | **Только архитектурная идея** "локальные эмбеддинги + zero-config индексация" | **Нет** — лицензия прямо запрещает "general-purpose competing Obsidian offerings", код не копируем, только вдохновляемся общей идеей |
| Vault Analyst / MCP доступ к Vault (референс) | `cyanheads/obsidian-mcp-server` | Проверить лицензию в репо перед использованием | Форкается активно (используется другими организациями) | — | Референс структуры операций "read/write/search/edit notes, tags, frontmatter" через REST API | Нет, только интерфейс-паттерн |
| Writer / Markdown+YAML | `python-frontmatter` (eyeseast) | MIT | Стабильная, широко используется, простая | — | Прямое использование для парсинга/генерации frontmatter | Да, прямая зависимость |
| Validator | нет отдельного репо — собственная реализация | — | — | — | Deterministic-валидация не требует стороннего фреймворка: `PyYAML.safe_load` + `markdown-it-py` AST + собственные проверки wikilink/backlink | Пишем сами |
| Retry/rate-limit | `jd/tenacity` | Apache-2.0/MIT (проверить, обычно Apache-2.0) | Очень зрелая, стандарт индустрии | — | Exponential backoff + jitter "из коробки" | Да, прямая зависимость |

### 12.2 Выводы по секции 33 ТЗ (архитектура/поддержка/документация/тесты/лицензия/зависимости/Gemini-совместимость/MVP-fit)

- **`gpt-researcher`**: архитектурно зрелый, хорошо документирован, есть тесты, Apache-2.0 (разрешает использование). Но: рассчитан на полноценный веб-сервис (свой FastAPI backend, React frontend, Docker-compose), Gemini-совместимость есть (multi-provider), но **fit с нашим single-user CLI низкий** — тянуть его целиком значит нарушить принцип "не Frankenstein". **Решение: не подключаем как зависимость, берём только идею workflow (plan→multi-source search→aggregate→reduce bias через множественные источники).**

- **`obsidian-local-rest-api`**: MIT, очень активно поддерживается (в т.ч. свежие релизы в июле 2026), даёт встроенный MCP-сервер прямо из коробки, хорошая документация (интерактивные OpenAPI docs). **Высокий MVP-fit**, если вы хотите работать с Vault не напрямую по файловой системе, а через локальный HTTPS API — это безопаснее (нет риска случайно затронуть файлы, которые Obsidian сейчас держит открытыми/индексирует) и сразу даёт MCP-интерфейс для будущего расширения (например, чтобы Claude Code или другой ассистент тоже мог работать с тем же Vault). **Рекомендация обсудить с вами (см. §13 "Решения, требующие вашего выбора")**: работать через этот REST API-плагин, либо читать/писать файлы напрямую с диска (без Obsidian-плагина) — у обоих подходов разные плюсы.

- **`obsidian-smart-connections`**: важно, что лицензия **изменилась с GPLv3 на кастомную "Smart Plugins License"** в декабре 2025, с формулировкой, ограничивающей "конкурирующие Obsidian-продукты общего назначения" — это прямой юридический риск по вашему же требованию (п.34 ТЗ: "если GPL/AGPL или лицензия с потенциальными юр.последствиями — предупредить и не включать автоматически"). **Явно предупреждаю и не включаю код или архитектуру, скопированную из этого репозитория.** Общая идея "локальные эмбеддинги без API-ключа" — общеизвестный паттерн, не защищённый копирайтом сам по себе, поэтому упоминаю его только как référence "что бывает на рынке", не как источник кода/паттерна для копирования.

- **LangGraph HITL-паттерн**: сам LangGraph — MIT, зрелый, отлично документирован, у Anthropic/LangChain много официальных примеров именно на `interrupt()/Command(resume=...)`. **Fit высокий как паттерн, низкий как обязательная зависимость** для MVP-0 (см. §8 — простой Python state machine достаточен для линейного workflow с одной approval-точкой). Оставляем архитектуру совместимой: если в будущем понадобится параллельный research по нескольким подтемам одновременно (map-reduce, что как раз демонстрирует шаблон `KirtiJha/langgraph-interrupt-workflow-template`), миграция на LangGraph не потребует переписывать модели данных — они уже структурированы как отдельные Pydantic-объекты.

- **`ddgs` / `tenacity` / `python-frontmatter`**: все MIT/Apache, маленькие, стабильные, широко используемые библиотеки без экзотических зависимостей — единственные три сторонние библиотеки, которые предлагаю подключать "как есть", без адаптации.

### 12.3 Что НЕ стоит использовать и почему

- **CrewAI** — не нашёл архитектурного преимущества для нашего линейного одно-пользовательского workflow; добавляет собственную модель "crew/agent/task" поверх которой всё равно пришлось бы городить staging/HITL вручную — оверинжиниринг для MVP.
- **`obsidian-smart-connections`** (код) — юридический риск лицензии (см. выше). Не используем даже частично.
- Полноценный **`gpt-researcher`** как зависимость — избыточен (свой сервер, Docker, много неиспользуемых фич типа "чат по загруженным документам", "report generation UI").
- Любые research/search MCP-обёртки, которые по умолчанию подключают платные fallback (Tavily/Exa/SerpAPI) "на всякий случай" — прямое нарушение вашего требования FREE_ONLY; даже если платный вызов "опционален", лишний риск случайного расхода не нужен в MVP.

### 12.4 Итоговая карта: Agent → Skill → Source → Adaptation → Gemini

```
Orchestrator
  ↓ skill: state machine, budget control
  ↓ source: собственная реализация (паттерн HITL — референс LangGraph docs)
  ↓ adaptation: полностью свой код, без внешней agent-framework зависимости
  ↓ Gemini: не вызывает LLM напрямую, только маршрутизирует к ролям

Planner
  ↓ skill: query decomposition → topic structure
  ↓ source: собственный промпт (общий паттерн decomposition, не копия чужого prompt-файла)
  ↓ adaptation: structured JSON output через google-genai SDK
  ↓ Gemini: 1 вызов (Flash)

Researcher
  ↓ skill: web search (ddgs) + fetch (httpx/trafilatura) + source ranking
  ↓ source: ddgs (прямая зависимость, MIT) + идея pipeline из gpt-researcher (паттерн, не код)
  ↓ adaptation: собственная обёртка tools/web_search.py, tools/web_fetch.py
  ↓ Gemini: 1 вызов на отбор/ранжирование релевантных источников

Extractor + Critic (объединены)
  ↓ skill: evidence extraction + contradiction/quality check
  ↓ source: собственный промпт с structured output (facts[], confidence, contradicts[])
  ↓ adaptation: батчинг по источникам для минимизации вызовов
  ↓ Gemini: N вызовов (по батчам источников) — основной "бюджетный" расход

Vault Analyst
  ↓ skill: индексация (SQLite), BM25-поиск, dedup-порог
  ↓ source: собственная реализация + опционально obsidian-local-rest-api (MIT) как транспорт к Vault
  ↓ adaptation: без LLM для 90% случаев; Gemini только на "серую зону" similarity
  ↓ Gemini: 0-2 вызова (только для неоднозначных дублей)

Synthesizer + Writer (объединены)
  ↓ skill: итоговая структура заметок + генерация markdown/frontmatter/tags
  ↓ source: собственный промпт + python-frontmatter (MIT) для сборки файла
  ↓ adaptation: structured output → DraftNote[] → сериализация в .md
  ↓ Gemini: 1 вызов

Linker
  ↓ skill: сопоставление сущностей с существующими заметками → wikilinks/backlinks
  ↓ source: собственная реализация (граф в SQLite)
  ↓ adaptation: чистый код
  ↓ Gemini: 0 вызовов

Validator
  ↓ skill: YAML/Markdown/link/dedup/no-delete проверки
  ↓ source: PyYAML + markdown-it-py (MIT) + собственные проверки
  ↓ adaptation: чистый код, детерминированно
  ↓ Gemini: 0 вызовов

Human Approval / Staging
  ↓ skill: diff-показ, approve/reject, atomic commit
  ↓ source: паттерн из LangGraph HITL docs (референс), CLI-реализация своя
  ↓ adaptation: собственный CLI (rich/typer)
  ↓ Gemini: 0 вызовов
```

**Ожидаемый расход Gemini на типовой запрос**: Planner (1) + Researcher-selection (1) + Extractor/Critic (обычно 3–6, по числу источников/батчей) + Vault-dedup (0–2) + Synthesizer/Writer (1) = **примерно 6–11 вызовов**, что комфортно укладывается даже в самый скромный дневной лимит free tier (сотни RPD) и позволяет делать по несколько тем-запросов в день без риска упереться в лимит при разумном `MAX_GEMINI_CALLS_PER_TASK` (предлагаю поставить потолок **15** на задачу как защитный "предохранитель").

---

## 13. Решения, требующие вашего выбора (архитектурно значимые)

Прежде чем перейти к Phase 2 (MVP design) и тем более к коду, нужно ваше решение по нескольким развилкам:

**Вариант A — Доступ к Vault напрямую через файловую систему** (без Obsidian-плагинов)
- ✅ Проще всего, нет зависимости от того, запущен ли Obsidian
- ✅ Не требует установки стороннего плагина
- ⚠️ Риск конфликта, если Obsidian держит файл открытым/индексирует его в момент записи (на практике для Markdown-файлов это почти всегда безопасно — Obsidian сам следит за изменениями на диске)

**Вариант B — Через `obsidian-local-rest-api`** (MIT-плагин, придётся установить в Obsidian)
- ✅ Официальный, активно поддерживаемый способ, "знает" о состоянии Obsidian
- ✅ Сразу даёт готовый MCP-сервер на будущее (можно будет подключить, например, Claude Desktop к тому же Vault)
- ⚠️ Дополнительная зависимость: нужно установить и включить community-плагин в Obsidian, держать Obsidian запущенным во время работы системы

Я рекомендую **Вариант A для MVP-0** (проще, меньше движущихся частей, полностью соответствует "не усложняй систему") с архитектурой, не блокирующей переход на Вариант B позже (весь доступ к Vault уже изолирован в модуле `vault/reader.py` и `vault/writer.py` — замена на REST-клиент не затронет остальную систему). Согласны?

**Развилка 2 — Semantic dedup**: в MVP-0 использовать только BM25/keyword (без эмбеддингов) или сразу добавить локальные эмбеддинги (`sentence-transformers`, офлайн)?
- Keyword-only — быстрее реализовать, но хуже ловит дубли с другой формулировкой ("векторная база данных" vs "vector database" не совпадут по словам).
- Локальные эмбеддинги — чуть дольше первичная настройка (загрузка модели ~100-400MB при первом запуске), но заметно лучше качество дедупликации, и это всё ещё **бесплатно и локально**, никакого API.

Я рекомендую **сразу включить локальные эмбеддинги** в MVP-0, так как "не создавать дубликаты" — это прямо названо "главным правилом" в вашем ТЗ (§3, Vault Analyst), а keyword-only даёт заметно больше ложноотрицательных срабатываний именно на этой ключевой функции. Согласны, или предпочитаете более простой keyword-only старт?

**Развилка 3 — Git-версионирование Vault**: включать по умолчанию (`GIT_ENABLED=true`) или оставить выключенным как опцию?
Я рекомендую **включить по умолчанию**, если ваш Vault уже не под Git — это даёт бесплатный, надёжный rollback-механизм почти без усилий (просто `git init` + commit после каждого approved-изменения). Если Vault уже синхронизируется чем-то другим (Obsidian Sync, iCloud, Syncthing) — Git всё равно безопасно сосуществует рядом, просто локальная история.

---

## 14. План реализации по этапам (после вашего подтверждения)

- **Phase 2 — MVP design**: финализировать минимальный набор компонентов (на основе ответов на §13), точные Pydantic-схемы, конкретные промпты по ролям, конфигурацию `.env.example`.
- **Phase 3 — Implementation**: реализация по слоям — сначала `storage/vault/retrieval/validation` (весь код без LLM и без сети, тестируемый изолированно), затем `gemini/client` с моками, затем `roles/*`, затем `orchestrator` и `cli`.
- **Phase 4 — Testing**: тестовый Vault (10-15 заметок с разными frontmatter/тегами/связями, включая намеренные "почти дубликаты"), unit-тесты на validation/dedup/staging, интеграционный прогон с замоканным Gemini (без реальных вызовов — не тратим лимит на тесты), затем один контролируемый прогон с реальным Gemini на тестовом Vault, отдельный тест намеренного исчерпания лимита (симуляция 429) — проверка корректной остановки без fallback на платный tier.
- **Phase 5 — Real Vault**: только после успешного Phase 4, и только с `ALLOW_DELETE=false`, с обязательным первым запуском в режиме "dry-run" (staging строится, но approve не выполняется автоматически — вы сначала просто смотрите на предложенные изменения на реальном Vault, ничего не коммитя).

---

## Резюме — что нужно от вас сейчас

1. Подтвердить архитектуру в целом (разделы 1–11).
2. Ответить на 3 развилки в §13 (доступ к Vault напрямую vs через REST-плагин; keyword-only vs локальные эмбеддинги для дедупликации; Git по умолчанию вкл/выкл).
3. Подтвердить итоговую карту GitHub-исследования (§12) — ничего из стороннего кода, кроме трёх маленьких MIT-библиотек (`ddgs`, `tenacity`, `python-frontmatter`) и опционально MIT-плагина `obsidian-local-rest-api`, не включается напрямую; всё остальное — только архитектурные паттерны.

После вашего подтверждения перехожу к Phase 2 (точный MVP-дизайн) и затем к Phase 3 (реализация). Код пока не пишу.
