# Документация: `orchestrator/state_machine.py` — не-LLM конечный автомат задачи

> Reference-док. Обзор пакета — `_index.md`. Исключения/бюджет вызовов —
> `budget.md`. Сам процесс одного запроса (что вызывается в каком порядке и
> что через что течёт) описан отдельно, в `../flows/llm_cycle.md` — там же
> ссылки сюда за деталями конкретных сигнатур. Общие структуры данных
> (`Plan`, `DraftNote`, `TaskStatus` и т.д.) — в `../storage/models.md`,
> здесь не дублируются.

**Назначение (из докстринга модуля).** Чистый Python, который: понимает
пользовательский запрос (нормализует `Task`); вызывает роли в фиксированной
последовательности; прокидывает структурированные данные между этапами;
контролирует бюджет LLM-вызовов и останавливается при исчерпании лимита
(без fallback на платный tier); персистит прогресс после каждого шага
(`staging/checkpoint.py`, см. `../staging/checkpoint.md §2`), чтобы задачу
можно было продолжить позже (`resume <task_id>`); никогда сам не пишет в
реальный Vault (это `staging/commit.py`, только после явного `approve`).

Важный нюанс про бюджет при resume: `MAX_LLM_CALLS_PER_TASK` — лимит на
ОДНУ СЕССИЮ/ПОПЫТКУ (`status.llm_calls_used` обнуляется в начале каждого
вызова `run()`, в т.ч. при resume), а не на задачу за всё её время жизни.
Иначе после однократного исчерпания лимита задачу нельзя было бы продолжить
никогда. Накопительный расход по всем попыткам хранится отдельно в
`TaskCheckpoint.total_llm_calls_used` (`../staging/checkpoint.md §2.2`) —
только для отчёта пользователю.

## 1. `KNOWLEDGE_MODE_FRONTMATTER_SOURCE: str = "model-knowledge"`

**Описание.** Пометка `frontmatter.source` (см.
`tools/markdown_tools.py::_ALLOWED_FRONTMATTER_KEYS`,
`../tools/markdown_tools.md §1`) для заметок, написанных в
`RESEARCH_MODE=knowledge` — без внешних проверяемых источников. Совпадает
по смыслу, но не по значению написания, с
`roles/elaborator.py::MODEL_KNOWLEDGE_SOURCE_ID = "model_knowledge"`
(`../roles/elaborator.md`): одна константа маркирует `Evidence` (внутренний
объект), другая — YAML frontmatter итоговой заметки (видимое пользователю
значение).

## 2. `class OrchestratorStopped(Exception)`

**Описание.** Управляемая остановка задачи — например, чекпоинт не найден
или битый при resume, либо `settings.research_mode == "web"` (сейчас явно
не поддерживается, см. §4). Прогресс сохранён (кроме случая, когда сам
чекпоинт оказался нечитаем).

**Конструктор:** `__init__(self, message: str, task_id: str)` — сохраняет
`self.task_id` дополнительно к стандартному сообщению `Exception`.

## 3. `class RunResult` (dataclass)

**Описание.** Результат вызова `Orchestrator.run(...)`.

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | Идентификатор задачи. |
| `changeset` | `StagingChangeset \| None` | `None`, если задача остановлена (не дошла до staging). |
| `status` | `TaskStatus` | Счётчик LLM-вызовов ЭТОЙ сессии. |
| `stopped` | `bool` | `True` — остановлена по бюджету/лимиту/отказу пользователя утвердить план. |
| `message` | `str` | Человекочитаемое сообщение (для CLI). |

## 4. `class Orchestrator`

### `__init__(self, settings: Settings)`

**Описание.** Инициализирует всё, что нужно для одного запуска задачи:
проверяет `FREE_ONLY` (`settings.validate_free_only()`), создаёт рабочие
директории (`settings.ensure_dirs()`), открывает `VaultDB`, пытается
создать локальный эмбеддер, строит ДВА `LLMBudget` (`budget.md §3`,
основной и extraction) и через `llm/factory.py` (`../llm/core.md §3`)
создаёт два LLM-клиента.

**Параметры:** `settings: Settings` (см. `../config/settings.md`).

**Возвращаемое значение:** — (конструктор).

**Исключения:**
- `RuntimeError` — если `settings.free_only=False`.
- `RuntimeError` — если ключ провайдера пуст (поднимается внутри
  конструктора конкретного клиента, см. `../llm/groq_client.md`).

**Ключевые атрибуты после инициализации:**

| Атрибут | Тип | Назначение |
|---|---|---|
| `self.db` | `VaultDB` | Локальный индекс Vault (`../vault/db.md`). |
| `self.embedder` | `LocalEmbedder \| None` | Локальные эмбеддинги (`../tools/dedup.md §1`). |
| `self.budget` | `LLMBudget` | Общий бюджет вызовов основного клиента (`budget.md §3`). |
| `self.llm` | `LLMClient`-совместимый объект | Основной клиент — planning, критик, vault-dedup, folder assignment, writer. |
| `self.extraction_budget` | `LLMBudget` | Отдельный бюджет для extraction/elaboration. |
| `self.extraction_client` | `LLMClient`-совместимый объект | Клиент для `roles/elaborator.py`. |

Оба клиента создаются через `llm/factory.py::create_llm_client` /
`create_extraction_llm_client` (`../llm/core.md §3`) — конкретный тип
(`GroqClient`) `Orchestrator` не знает и не должен знать
(`llm/base.py::LLMClient` Protocol, `../llm/core.md §1`).

### `close(self) -> None`

Закрывает соединение с `VaultDB`. Вызывается в `finally` в `cli/main.py`
(`../cli/main.md`).

### `sync_vault_index(self) -> dict`

**Описание.** Инкрементальная переиндексация Vault. Не использует LLM.
Вызывается в начале `run()` перед любыми LLM-шагами.

**Возвращаемое значение:** `dict` со статистикой (`scanned`, `updated`,
`unchanged`, `removed`) — см. `vault/index.py::VaultIndexer.sync`,
`../vault/index.md`.

### `run(self, raw_query=None, *, resume_task_id=None, progress_cb=None, plan_confirm_cb=None, merge_confirm_cb=None) -> RunResult`

**Описание.** Главный метод — выполняет весь workflow задачи до этапа
`staging` включительно. Порядок и содержимое самих шагов (planning →
elaborating → vault analysis → synthesis → validation → staging) — см.
`../flows/llm_cycle.md`, здесь описан только сам метод как API.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `raw_query` | `str \| None` | Запрос пользователя. Обязателен, если `resume_task_id is None`. |
| `resume_task_id` | `str \| None` | `task_id` для продолжения. |
| `progress_cb` | `Callable[[str], None] \| None` | Вызывается на каждом крупном шаге с человекочитаемой меткой. |
| `plan_confirm_cb` | `Callable[[Plan], bool] \| None` | Вызывается РОВНО ОДИН РАЗ на задачу (флаг `checkpoint.plan_approved`) сразу после построения/загрузки `Plan`, ДО самого дорогого этапа (elaboration). Если вернул `False` — контролируемая остановка, план (1 дешёвый вызов) уже сохранён. Если не передан — план утверждается автоматически. |
| `merge_confirm_cb` | `Callable[[list[DraftNote]], list[DraftNote]] \| None` | Вызывается после Writer+Critic, ДО validation/staging, только если `settings.enable_draft_merging=True`. |

**Возвращаемое значение:** `RunResult` (см. §3).

**Исключения, которые ловятся ВНУТРИ метода (не всплывают наружу):**
`LLMFreeLimitReached`, `LLMTaskBudgetExceeded` (`budget.md §1–2`) — в обоих
случаях сохраняется чекпоинт на последнем успешном шаге и возвращается
`RunResult(stopped=True, ...)`.

**Исключения, которые поднимаются наружу:**
- `OrchestratorStopped` — если `settings.research_mode == "web"` (сейчас не
  поддерживается — миграция на новую структуру плана не завершена), либо
  если `_load_or_create_state` не нашёл чекпоинт при resume.
- `ValueError` — если ни `raw_query`, ни `resume_task_id` не переданы.

**Локальные функции внутри `run`:**
- `report(stage: str) -> None` — прокси к `progress_cb`, если он передан.
- `persist(stage_label: str) -> None` — сохраняет `TaskCheckpoint` на диск
  немедленно после шага (обновляет `last_completed_stage`, `status`,
  `total_llm_calls_used`). Вызывается очень часто (после каждого батча
  elaboration, после каждой написанной заметки) — это и есть механизм
  resume.

### `_load_or_create_state(self, *, raw_query, resume_task_id, report) -> tuple[TaskCheckpoint, Task, TaskStatus, int]`

**Описание.** Приватный метод: либо загружает существующий `TaskCheckpoint`
с диска (при resume), либо создаёт новый `Task`/`TaskStatus`/`TaskCheckpoint`
(при новом запросе).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `raw_query` | `str \| None` | См. `run()`. |
| `resume_task_id` | `str \| None` | См. `run()`. |
| `report` | `Callable[[str], None]` | Локальная функция логирования прогресса из `run()`. |

**Возвращаемое значение:** `(checkpoint, task, status, base_total_calls)`:

| Элемент | Тип | Назначение |
|---|---|---|
| `checkpoint` | `TaskCheckpoint` | Персистентное состояние задачи (`../staging/checkpoint.md §2`). |
| `task` | `Task` | `task_id`, `raw_query`, `language`. |
| `status` | `TaskStatus` | Счётчик вызовов ТЕКУЩЕЙ сессии — обнуляется даже при resume. |
| `base_total_calls` | `int` | Сколько вызовов было потрачено во ВСЕХ предыдущих сессиях (только для отчёта). |

**Исключения:**
- `OrchestratorStopped` — если `resume_task_id` передан, но
  `load_checkpoint(...)` вернул `None` (не найден/битый/несовместимая
  версия).
- `ValueError` — если ни `raw_query`, ни `resume_task_id` не переданы.

---

## Сводная таблица: какие поля `Settings` читает этот модуль

| Поле `Settings` | Где используется |
|---|---|
| `free_only` | `Orchestrator.__init__` → `validate_free_only()` |
| `workdir`/`staging_dir`/`checkpoint_dir`/`db_path` | `Orchestrator.__init__` → `ensure_dirs()` |
| `embedding_model`, `use_local_embeddings` | `Orchestrator.__init__` → `try_create_embedder` |
| `llm_provider` | `Orchestrator.__init__` → `budget_limits_for_provider`/`create_llm_client` |
| `max_llm_calls_per_task` | `LLMBudget.__init__` (оба бюджета) |
| `groq_extraction_model` (через `extraction_budget_limits`) | `Orchestrator.__init__` |
| `research_mode` | `Orchestrator.run()` — блокирует `"web"` |
| `enable_draft_merging`, `draft_merge_mode` | `Orchestrator.run()` — вызов `merge_confirm_cb` |
| `default_notes_folder` | `Orchestrator.run()` — построение `topic_folder` для Vault Analyst |
| `dedup_high_threshold`, `dedup_low_threshold` | `Orchestrator.run()` → `vault_analyst.resolve_notes_against_vault` |
| `max_subpoints_per_generation_batch` | `Orchestrator.run()` → `elaborator.elaborate_outline` |
| `max_critic_rounds` | `Orchestrator.run()` → `critic.run_critic_cycle` |
| `allow_delete` | `Orchestrator.run()` → `run_validation` |

Документация по `orchestrator/state_machine.py` завершена. Продолжение
(workflow одного запроса целиком) — см. `../flows/llm_cycle.md`.
