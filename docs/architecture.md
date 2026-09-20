# MVP AI-системы управления знаниями для Obsidian
## Phase 1 — Архитектура + Phase 0 — GitHub Research
### (код ещё не пишется — документ на утверждение)

---

## 0. Резюме подхода

Система = **один Orchestrator + набор "ролей" (по сути — промптов/режимов одной и той же LLM) + детерминированные локальные инструменты + staging + human approval**.

**Единственный LLM-провайдер в MVP — Groq Free Tier** (`llm_provider="groq"` в `config/settings.py`, см. §5). Архитектура рассчитана на добавление других провайдеров в будущем без изменения кода ролей: `llm/factory.py` — единственная точка выбора провайдера, `llm/base.py::LLMClient` — общий Protocol, которому должен соответствовать любой новый клиент (по образцу `llm/groq_client.py`). Внутренние идентификаторы (`LLMBudget`, `LLMFreeLimitReached`, `MAX_LLM_CALLS_PER_TASK`) сознательно названы провайдер-нейтрально, а не "Groq*", именно чтобы не привязывать их к конкретному провайдеру.

Ключевая идея, которую стоит проговорить сразу: **не все 9 "ролей" из ТЗ — это отдельные вызовы LLM**. Часть из них (Extractor+Critic, Vault Analyst, Validator) либо объединяются в один LLM-вызов, либо вообще не требуют LLM (дедупликация, YAML-валидация, поиск backlinks — это чистый код). Это прямое следствие требования "минимизировать количество LLM calls" и "LLM не должен проверять то, что может проверить код".

---

## 1. Архитектура верхнего уровня

```
                     ┌───────────────────────────┐
                     │        USER (CLI)         │
                     └─────────────┬─────────────┘
                                   │ natural language query
                                   ▼
                     ┌───────────────────────────┐
                     │       ORCHESTRATOR        │
                     │  state machine (не LLM)   │
                     │  + бюджет LLM-вызовов      │
                     │  + error handling          │
                     └──────┬──────────┬──────────┘
                            │          │
                 ┌──────────┘          └──────────┐
                 ▼                                ▼
        ┌─────────────────┐              ┌─────────────────┐
        │   ROLE LAYER     │◄────calls────┤  LOCAL TOOLS     │
        │ (промпт-режимы   │              │  (детерминизм)   │
        │  одной модели)   │              └────────┬─────────┘
        └────────┬─────────┘                       │
                 │ prompts                          │
                 ▼                                  │
        ┌─────────────────┐                         │
        │   LLM CLIENT     │                         │
        │ (Groq — единст-  │                         │
        │  венный провайдер │                        │
        │  в MVP)          │                         │
        └─────────────────┘                         │
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

Важно: **LLM (Groq Free Tier) — reasoning engine, локальный код — execution layer, Vault — knowledge layer**. Ни один "агент" не имеет собственного независимого цикла — все вызовы инициирует Orchestrator по жёсткому конечному автомату (finite state machine), а не автономный agent loop.

---

## 2. Роли: что реально требует LLM (Groq), а что — нет

### 2.1. RESEARCH_MODE: два режима сбора материала

Начиная с этой версии система поддерживает переключатель `RESEARCH_MODE` (`config/settings.py`), определяющий, ОТКУДА берётся материал для заметок:

- **`web`** (прежний путь) — Researcher ищет источники в интернете (DuckDuckGo, бесплатно) и скачивает их текст; Extractor+Critic извлекает evidence из РЕАЛЬНОГО текста источников. Даёт проверяемые `source_refs` в каждой заметке, но зависит от нестабильного сетевого I/O (поиск/скрейпинг могут падать, тормозить, блокироваться сайтами) и тратит больше LLM-вызовов.
- **`knowledge`** (дефолт с этой версии) — веб-поиск и скачивание страниц полностью пропускаются. Вместо Extractor+Critic материал по каждой подтеме плана генерирует роль **Elaborator** (`roles/elaborator.py`) — из знаний самой модели, без внешнего текста. Быстрее, не зависит от сети, но заметки не имеют проверяемых источников: они помечаются `frontmatter.source: model-knowledge` (см. §6.4) и должны восприниматься как черновой конспект, требующий вашей проверки, а не как исследование с цитируемыми источниками.

Обе роли (Extractor+Critic и Elaborator) выдают один и тот же тип объекта — `Evidence` (см. §3) — поэтому все последующие этапы (Vault Analyst, Synthesizer, Writer) не знают и не должны знать, в каком режиме работает система.

Отдельно от режима сбора материала, после Writer в обоих режимах может подключаться роль **Critic** (`roles/critic.py`) — она ревьюит уже НАПИСАННУЮ заметку (не сырой evidence) на внутреннюю согласованность и полноту, и может попросить Writer переписать её. Это НЕ тот же Critic, что был частью Extractor+Critic в `web`-режиме (тот сверял утверждения с текстом источника — здесь сверять не с чем, поэтому новый Critic ограничен тем, что можно проверить без внешней истины: логикой текста и соответствием уже собранным evidence). Подробнее — §2.3.

### 2.2. Таблица ролей

| # | Роль из ТЗ | Нужен ли отдельный LLM call (Groq)? | Реализация |
|---|---|---|---|
| 1 | **Orchestrator** | Нет | Чистый Python: state machine, бюджет вызовов, роутинг, ветвление по RESEARCH_MODE |
| 2 | **Planner** | Да, 1 вызов | LLM (Groq): query → структура темы, подтемы, типы источников |
| 3 | **Researcher** | Частично, ТОЛЬКО в RESEARCH_MODE=web | Поиск (DDGS, бесплатно, без LLM) + 1 вызов LLM (Groq) на ранжирование/отбор релевантных источников из уже найденных сниппетов. В RESEARCH_MODE=knowledge шаг полностью пропускается — 0 вызовов |
| 4 | **Extractor** (RESEARCH_MODE=web) / **Elaborator** (RESEARCH_MODE=knowledge) | Да | web: 1 вызов на пачку источников (факты + оценка достоверности одновременно). knowledge: 1 вызов на пачку подтем плана — раскрывает подтему из знаний модели, без входного текста |
| 5 | **Critic / Fact-Checker** (сверка с источником) | Объединён с Extractor (см. выше), только web | Противоречия между источниками детектятся тем же вызовом; сравнение с самим собой на дубли — кодом. В knowledge-режиме сверять не с чем — вместо этого роль #7.1 ниже |
| 6 | **Vault Analyst** | Нет (LLM не нужен для 90% работы) | Локальный индекс (SQLite) + детерминированный поиск (BM25/keyword) + embedding-опция (см. §6). Только финальное "это та же концепция или нет?" на пограничных случаях — 1 маленький вызов LLM (Groq) на кандидата-дубликат |
| 7 | **Synthesizer** | Да, 1 вызов | Собирает Plan + Evidence + ExistingNotes → план заметок (какие новые, какие дополнить, распределение evidence) |
| 7.1 | **Critic** (ревью заметки, новая роль) | Да, 0-2 вызова НА ЗАМЕТКУ (bounded) | Проверяет уже написанный текст заметки на согласованность/полноту, может запросить переписывание — не более `MAX_CRITIC_ROUNDS` раз (см. §2.3) |
| 8 | **Obsidian Writer** | Да, 1 вызов на заметку (+ до `MAX_CRITIC_ROUNDS` повторов, если Critic просит переписать) | Markdown + frontmatter + wikilinks — по одному вызову на КАЖДУЮ заметку из плана Synthesizer-а |
| 9 | **Linker / Knowledge Graph** | Нет | Код: сопоставление тегов/заголовков с существующими заметками, простановка `[[wikilinks]]`, обновление backlink-графа в SQLite |
| 10 | **Validator** | Нет | Чистый код: YAML lint, markdown lint, проверка существования файлов по wikilink, дубликаты по хэшу/similarity, "не удалять без подтверждения" |
| 11 | **Human Approval / Staging** | Нет | CLI diff-view (с пометками `model-knowledge`/`needs_review`, см. §7) + подтверждение |

**Итог по вызовам на типовой запрос:**
- **RESEARCH_MODE=web** (как и раньше): ~4–11 вызовов (Planner 1, Researcher-selection 1, Extractor+Critic 3–6, Vault-dedup 0–2, Synthesizer 1, Writer N, Critic-ревью 0–2N).
- **RESEARCH_MODE=knowledge** (новый дефолт): ~3–8 вызовов на типовой запрос — Planner (1) + Elaborator (1–3, батчами по подтемам) + Vault-dedup (0–2) + Synthesizer (1) + Writer (N) + Critic-ревью (0–2N, где N — число заметок, обычно 4–6). Ниже за счёт полного отсутствия Researcher/Extractor-шагов и отсутствия сетевого I/O как источника сбоев.

### 2.3. Critic: bounded retry, а не цикл до "ok"

`MAX_CRITIC_ROUNDS` (`config/settings.py::max_critic_rounds`, по умолчанию 1) — жёсткий потолок числа переписываний ОДНОЙ заметки по замечаниям критика, а не цикл "пока не одобрит". Логика (`roles/critic.py::run_critic_cycle`):

1. Writer пишет заметку.
2. Если `max_critic_rounds > 0` — Critic ревьюит её.
3. Если вердикт `rewrite` — Writer переписывает с учётом `feedback`, счётчик раундов растёт.
4. Шаги 2–3 повторяются, пока вердикт не станет `ok`, ИЛИ пока не будет исчерпан `max_critic_rounds`.
5. Если бюджет раундов исчерпан, а вердикт всё ещё `rewrite` — заметка уходит в staging КАК ЕСТЬ, с пометкой `needs_review=true` (видна в diff, см. §7) — **без** дополнительного финального ре-ревью (это сознательный выбор в пользу экономии вызовов, а не гарантии, что финальная версия одобрена).

Это то же архитектурное решение, что и retry в `orchestrator/budget.py` (жёсткий потолок вместо неограниченного цикла) — распространённое дальше, на LLM-based проверку контента, а не только на сетевые ретраи.

---

## 3. Данные между этапами (структурированные объекты)

Все объекты — Pydantic-модели (валидируются кодом, не LLM). Между ролями летают **не текстовые простыни**, а эти структуры:

```
Task            — исходный запрос + распознанный intent + язык
Plan            — тема, список подтем/концепций, типы нужных источников
SourceCandidate — url, title, snippet, релевантность (score) — используется только в RESEARCH_MODE=web
Evidence        — text, concept, source_id, confidence, contradicts[], verified —
                  общий формат для Extractor+Critic (web) и Elaborator (knowledge);
                  verified=True только для web-режима, в knowledge всегда False
ExistingNote    — path, title, frontmatter, tags, links, similarity_score
DraftNote       — title, slug, folder, frontmatter, body_md, tags, links_out, source_refs,
                  critic_rounds, needs_review
Relationship     — from_note, to_note, type (wikilink/tag/backlink)
ValidationReport — ok: bool, errors[], warnings[]
StagingChangeset — creates[], updates[], deletes[] (deletes всегда пусты в MVP по умолчанию)
Status           — этап, потрачено LLM calls (Groq), оставшийся бюджет, ошибки
```

Все они сериализуются в JSON и сохраняются построчно в SQLite/файлы staging — это даёт "бесплатный" resume/retry: если LLM-провайдер (Groq) упал на шаге 4, не нужно пересчитывать шаги 1–3.

---

## 4. Workflow одного запроса (пошагово)

Пример: *«Изучи тему Retrieval-Augmented Generation»* (в RESEARCH_MODE=knowledge, дефолт):

```
1. User → CLI → Task
2. Orchestrator: normalize query, определить язык (ru), режим (research_and_populate)
3. [LLM #1, Groq] Planner: Task → Plan (подтемы: embeddings, vector DB, retrieval, 
   reranking, chunking, generation, evaluation)
4. [ПРОПУЩЕНО в knowledge-режиме] Researcher: веб-поиск + fetch страниц
5. [LLM #2..N, Groq] Elaborator: раскрывает каждую подтему плана из знаний модели
   батчами → Evidence[] (source_id="model_knowledge", verified=False)
6. Vault Analyst (код): по каждой Plan-концепции → локальный retrieval 
   (BM25/embeddings) по SQLite-индексу → ExistingNote[] кандидаты
7. [LLM, Groq, опционально] Vault-dedup: только для неоднозначных 
   ExistingNote-кандидатов — "это та же концепция, что и Evidence X?"
8. [LLM, Groq] Synthesizer (Note Planner): Plan + Evidence + ExistingNote[] → 
   план заметок (create/update, распределение evidence по заметкам)
9. Для КАЖДОЙ заметки из плана:
   9.1. [LLM, Groq] Writer пишет заметку
   9.2. [LLM, Groq, если max_critic_rounds>0] Critic ревьюит текст заметки
   9.3. Если вердикт rewrite — Writer переписывает (до max_critic_rounds раз)
   9.4. DraftNote получает frontmatter.source="model-knowledge",
        critic_rounds, needs_review (если бюджет ревью исчерпан без "ok")
10. Linker (код): DraftNote[] × ExistingNote[] → Relationship[] (wikilinks, backlinks)
11. Validator (код): YAML/Markdown lint, dead-link check, dup-check, 
    no-delete-check → ValidationReport
12. Staging (код): StagingChangeset записывается в staging/ (не в Vault!),
    diff явно помечает заметки с source=model-knowledge и needs_review=true
13. CLI: показать diff пользователю (новые файлы, изменяемые файлы, новые связи,
    предупреждения о непроверенном контенте)
14. User: approve / reject / edit
15. [если approve] Commit (код): StagingChangeset → реальный Vault, 
    опционально git commit
16. Orchestrator: финальный отчёт (Status)
```

В RESEARCH_MODE=web шаги 4–5 заменяются на прежний путь (поиск → отбор источников → fetch → Extractor+Critic по тексту источников), остальные шаги (6–16) идентичны.

**Узкие места:**
- **RESEARCH_MODE=web**: шаг "Extractor+Critic" линейно растёт с числом источников → главный риск по RPM/RPD; шаг "web_fetch" — не лимит LLM-провайдера, но может быть медленным/нестабильным (сайты блокируют скрейпинг) — именно эта нестабильность стала одной из причин ввести `knowledge`-режим.
- **RESEARCH_MODE=knowledge**: узкое место сдвигается на шаг 9 (Writer+Critic по каждой заметке) — линейно растёт с числом заметок (`max_notes_per_task`), но не зависит от сети.
- Vault-dedup (шаг 7) — опционален, включается только если keyword/embedding score в "серой зоне" (например, 0.4–0.7 similarity) — иначе дубли решаются кодом.

---

## 5. Как работает Free Tier Groq и как под него подстроиться

### 5.1 Groq Free Tier (единственный провайдер в MVP)

Лимиты считаются по **четырём измерениям одновременно** на уровне конкретной модели: RPM (запросов/мин), RPD (запросов/день), TPM (токенов/мин), TPD (токенов/день) — превышение любого из них даёт `429`. Актуальные цифры нужно проверять в Groq Console → Rate Limits, т.к. они меняются; ниже — значения на момент написания документа для моделей, реально используемых системой:

| Модель | RPM | RPD | TPM | TPD |
|---|---:|---:|---:|---:|
| `openai/gpt-oss-120b` (дефолт, `groq_model`/`groq_extraction_model`) | 30 | 1 000 | 8 000 | 200 000 |
| `openai/gpt-oss-20b` | 30 | 1 000 | 8 000 | 200 000 |
| `groq/compound` / `groq/compound-mini` | 30 | 250 | 70 000 | без лимита |
| `qwen/qwen3.8-27b` | 30 | 1 000 | 8 000 | 200 000 |

Практически значимая особенность Groq: **TPM (8 000 токенов/мин) — самое узкое место**, а не RPM или RPD — при системном промпте + JSON Schema уже в несколько сотен токенов и evidence-контексте на заметку легко подойти к пределу одного вызова. Поэтому `llm/groq_client.py::TokenRateLimiter` — не опциональная деталь, а центральный механизм клиента: sliding-window + leaky-bucket по TPM, адаптивная калибровка оценки токенов по роли (`TokenEstimateCalibrator`), учёт Groq prompt caching (кэшированные токены не считаются в лимит) и adaptive safety margin после реального `429`. См. подробный докстринг `llm/groq_client.py` — там описаны все итерации этой логики и почему.

RPD сбрасывается по UTC согласно политике Groq. Лимиты — на аккаунт/API-key, не суммируются между ключами (создание второго ключа "для обхода лимита" — прямое нарушение требования FREE_ONLY / никакого искусственного скейлинга, поэтому в MVP не делаем).

### 5.2 Расширение на другие провайдеры (не реализовано в MVP)

Чтобы добавить провайдера (например, если в будущем понадобится обход по RPD/TPM нескольких free tier), нужно реализовать `llm/base.py::LLMClient` в новом `llm/<provider>_client.py` (см. докстринг `llm/groq_client.py` — там описаны все механизмы клиентского rate-limiting, которые стоит повторить: sliding-window, backoff, обработка 429) и добавить одну ветку в `llm/factory.py::create_llm_client`. В MVP такой ветки нет — единственный провайдер это Groq.

### 5.3 Как система с этим работает

1. **GroqClient** — единственная точка входа к LLM в MVP. Все вызовы идут только через `llm/factory.py::create_llm_client`, который выбирает клиент по `settings.llm_provider`; `roles/*` работают с общим Protocol `llm/base.py::LLMClient` и не знают, какой провайдер активен — это и есть точка расширения на будущее.
2. Перед каждым вызовом — проверка локального счётчика (`calls_today`, `calls_this_minute`) относительно конфигурируемых мягких лимитов (`groq_rpm_soft_limit`/`groq_rpd_soft_limit`) — это **best-effort throttle**, не источник истины (источник истины — реальный ответ API, см. докстринг `orchestrator/budget.py`).
3. При `429` — exponential backoff + jitter, ограниченное число retry (например, 3), затем — **hard stop**, не переход на платный tier.
4. При явном исчерпании RPD — задача останавливается с понятным сообщением пользователю: *"Достигнут дневной лимит бесплатного API. Прогресс сохранён в staging, можно продолжить завтра"* — ключевое: **прогресс не теряется**, потому что каждый этап персистится (см. §3).
5. Глобальный **бюджет вызовов на задачу** (`MAX_LLM_CALLS_PER_TASK`, например 40) — жёсткий потолок независимо от лимитов API, чтобы не было "тихого" разрастания количества вызовов при большой теме. В RESEARCH_MODE=knowledge реальный расход обычно заметно ниже потолка (см. §2.2) — потолок остаётся общим "предохранителем" на случай богатой темы с большим числом заметок и критик-раундов.
6. Для роли Extractor/Elaborator (самая частая по числу вызовов, см. §2.2) используется ОТДЕЛЬНЫЙ клиент/бюджет (`orchestrator/state_machine.py::self.extraction_client`) — позволяет использовать модель с другим TPM-профилем (`groq_extraction_model`/`groq_extraction_tpm_limit`) независимо от модели остальных ролей.

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
   - **Опциональное улучшение**: локальные эмбеддинги (`sentence-transformers`, офлайн модель) для семантического сходства.
3. В LLM передаются **только**: заголовки + summary + frontmatter топ-N кандидатов (никогда — весь текст всех заметок, никогда — весь Vault).
4. Если top-1 similarity выше явного порога (например, >0.85) — дубликат детектируется кодом, LLM не нужен. Если в "серой зоне" — 1 маленький вызов LLM (Groq) "это та же концепция?".

### 6.3 Vault как граф
`notes` + `links` + `tags` в SQLite уже формируют граф. Linker обновляет рёбра при каждом commit. Backlinks — просто обратный join, отдельно не хранится (чтобы не рассинхронизировать).

### 6.4 Пометка происхождения заметки (RESEARCH_MODE)

`tools/markdown_tools.py::_ALLOWED_FRONTMATTER_KEYS` расширен ключом `source` (в дополнение к `title`/`tags`/`created`). Заметки, написанные в RESEARCH_MODE=knowledge, получают `frontmatter.source: model-knowledge` — это видно прямо в Obsidian при открытии файла, а не только в diff при approve. Заметки из RESEARCH_MODE=web этот ключ не получают (обратная совместимость, ключ просто отсутствует).

Это осознанный компромисс: система не отслеживает происхождение построчно (нет смешанного режима "часть фактов из веба, часть от модели" в одной заметке) — весь `Evidence` в рамках одной задачи получен ОДНИМ способом, определяемым `RESEARCH_MODE` на момент запуска задачи.

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
- **Diff явно помечает** (`staging/diff.py`) заметки с `frontmatter.source == "model-knowledge"` (⚠ без внешних источников) и заметки с `needs_review=true` (⚠ критик не одобрил после исчерпания попыток) — это последняя точка, где пользователь видит эти сигналы перед тем, как заметка попадёт в реальный Vault.

---

## 8. Технологии и почему

| Компонент | Технология | Почему |
|---|---|---|
| Язык | Python 3.11+ | Богатая экосистема для LLM/файлов/CLI, легко расширяется |
| LLM client | `openai` SDK (Groq, OpenAI-совместимый base_url) | Structured output (JSON schema/json_object), retry-friendly интерфейс; дополнительно клиентский TPM-лимитер (см. §5.1). Архитектура допускает подключение других SDK через `llm/<provider>_client.py` в будущем |
| Structured output | Pydantic v2 + JSON-mode | Строгая валидация между ролями без "угадывания" текста |
| CLI | `Typer` + `rich` | Простой, приятный CLI без веб-сложности; `rich` даёт красивый diff-вывод |
| Индекс Vault | SQLite (`sqlite3` stdlib) | Ноль внешних зависимостей, бесплатно, локально, ACID |
| Markdown-парсинг | `python-frontmatter` + `markdown-it-py` | Зрелые, MIT-лицензия, разделяют YAML frontmatter и тело |
| YAML-валидация | `PyYAML` (safe_load) | Стандарт |
| Веб-поиск (бесплатно, только RESEARCH_MODE=web) | `ddgs` (бывш. duckduckgo-search) | Без API-ключа, MIT, активно поддерживается |
| Fetch страниц (только RESEARCH_MODE=web) | `httpx` + `trafilatura`/`readability-lxml` | Извлечение чистого текста из HTML без лишнего шума |
| Retry/backoff | `tenacity` | Готовая, проверенная реализация exponential backoff + jitter |
| Локальные эмбеддинги | `sentence-transformers` (офлайн модель) | Бесплатно, без сети, для semantic retrieval по Vault |
| Git-интеграция | `GitPython` или прямой subprocess `git` | Опциональный versioning Vault-изменений |
| Конфиг | `pydantic-settings` + `.env` (`python-dotenv`) | Разделение конфига и логики, типизация, `.env` в `.gitignore` |

---

## 9. Структура проекта

```
obsidian-ai-kb/
├── config/
│   ├── settings.py          # pydantic-settings: пути, лимиты, FREE_ONLY,
│   │                         # RESEARCH_MODE, MAX_CRITIC_ROUNDS, язык
│   └── .env.example
├── orchestrator/
│   ├── state_machine.py     # шаги workflow, ветвление по RESEARCH_MODE,
│   │                         # переходы, error handling
│   └── budget.py             # контроль числа LLM calls / rate limit
├── llm/
│   ├── base.py               # Protocol LLMClient
│   ├── factory.py            # выбор провайдера (в MVP — только groq)
│   ├── schemas.py            # Pydantic-схемы structured output по ролям
│   ├── prompts/               # статические системные инструкции ролей
│   │   ├── outline_planner.py
│   │   ├── vault_analyst.py
│   │   ├── synthesizer_writer.py
│   │   ├── elaborator.py    # RESEARCH_MODE=knowledge
│   │   └── critic.py        # ревью написанной заметки
│   ├── groq_client.py
│   └── chunking.py            # батчинг элементов под TPM-бюджет
├── roles/                   # "роли" = сборка динамического prompt + бизнес-логика
│   ├── outline_planner.py
│   ├── researcher.py         # активен только в RESEARCH_MODE=web
│   ├── extractor_critic.py    # активен только в RESEARCH_MODE=web
│   ├── elaborator.py          # активен только в RESEARCH_MODE=knowledge
│   ├── vault_analyst.py
│   ├── synthesizer_writer.py
│   └── critic.py               # ревью написанной заметки, оба режима
├── tools/                   # skills — переиспользуемые детерминированные функции
│   ├── web_search.py        # обёртка над ddgs (RESEARCH_MODE=web)
│   ├── web_fetch.py         # httpx + trafilatura (RESEARCH_MODE=web)
│   ├── markdown_tools.py    # генерация/парсинг md, frontmatter (включая source)
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
│   ├── checkpoint.py          # resume-состояние (общее для обоих RESEARCH_MODE)
│   ├── commit.py               # запись в реальный Vault после approve
│   └── diff.py                # человекочитаемый diff для CLI, включая
│                                # пометки model-knowledge/needs_review
├── validation/
│   ├── markdown_validator.py
│   ├── yaml_validator.py
│   ├── link_validator.py
│   └── dedup_validator.py
├── storage/
│   └── models.py              # общие Pydantic-модели (Task, Plan, Evidence
│                                # с полем verified, DraftNote с critic_rounds/
│                                # needs_review, ...)
├── cli/
│   └── main.py                 # entrypoint, progress, diff view, approve prompt
├── tests/
│   ├── test_vault/            # тестовый Vault fixtures
│   ├── test_validation/
│   ├── test_staging/
│   ├── test_elaborator.py
│   ├── test_critic.py
│   └── test_groq_client.py    # моки openai/Groq для тестов без реальных вызовов
├── docs/
│   └── architecture.md         # этот документ
├── .env                         # не в Git
├── .gitignore
├── pyproject.toml
└── README.md
```

---

## 10. Что можно сделать полностью бесплатно

- **LLM**: Groq Free Tier (см. §5.1) — единственный обязательный внешний сервис.
- **Веб-поиск** (только RESEARCH_MODE=web): DuckDuckGo через `ddgs`, без ключа.
- **Fetch/извлечение контента** (только RESEARCH_MODE=web): `httpx` + `trafilatura` — бесплатно, локально.
- **Индекс/поиск по Vault**: SQLite + BM25 — 100% локально.
- **Опциональные эмбеддинги**: локальная модель (`sentence-transformers`, офлайн) — без API.
- **Git-версионирование** (опционально): бесплатно, локально.
- **Валидация**: весь Markdown/YAML/dedup/link-check — чистый код, бесплатно.

Единственное потенциальное "платно" — если пользователь захочет более качественный внешний поиск (Tavily и т.п.) в будущем — это **явно исключено** из MVP согласно ограничению.

---

## 11. Ограничения MVP (честно)

1. **RESEARCH_MODE=knowledge (дефолт) не имеет проверяемых источников.** Заметки пишутся из знаний модели, без сверки с реальным текстом — возможны фактические ошибки, устаревшая информация, неточные детали (числа, версии, даты). Заметки помечаются `source: model-knowledge` во frontmatter именно поэтому — это ЧЕРНОВОЙ конспект, требующий вашей проверки, а не исследование. Роль Critic (§2.3) проверяет только внутреннюю согласованность текста и полноту относительно уже собранных evidence — она НЕ является заменой фактчекинга по внешним источникам, поскольку у неё те же слепые пятна, что и у Writer.
2. **RESEARCH_MODE=web** (если включён явно) — качество бесплатного веб-поиска (DuckDuckGo без ключа) ниже, чем у платных research-API (Tavily/Exa) — возможны менее релевантные источники, скрейпинг может блокироваться некоторыми сайтами.
3. **RPM/RPD/TPM Groq Free** — на "богатую" тему с 8+ подтемами и множеством заметок можно упереться в дневной лимит (особенно в TPM, см. §5.1); задача должна уметь **приостанавливаться и продолжаться на следующий день** без потери прогресса (это заложено в дизайне staging/checkpoint).
4. **Semantic dedup в MVP-0 базовый** (BM25 + опциональные локальные эмбеддинги) — возможны пропущенные смысловые дубликаты с другой формулировкой.
5. **Single-user, локальный, без UI** — CLI, без веб-интерфейса, без multi-user.
6. **Нет автоматического отката произвольной сложности** — rollback реализован просто (git revert / snapshot "before"), не полноценная транзакционная СУБД поверх файловой системы.
7. **Critic bounded retry не гарантирует финальное качество** — после исчерпания `MAX_CRITIC_ROUNDS` заметка уходит в staging как есть, с пометкой `needs_review`, без гарантии, что переписанная версия лучше исходной (финальное ре-ревью сознательно не делается, см. §2.3).