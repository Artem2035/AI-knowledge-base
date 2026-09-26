# Документация цикла LLM-вызова. Часть 1: запуск → Orchestrator → бюджет → фабрика клиента

> Охват: только файлы, участвующие в передаче запроса от CLI до создания/выбора
> `GroqClient` и вызова `generate_structured(...)`. Сам `GroqClient` и его внутренние
> механизмы (rate limiting, калибровка токенов) описаны в **Части 2**
> (`docs_llm_cycle_part2_groq_client.md`).
>
> Файлы, охваченные здесь: `cli/main.py`, `orchestrator/state_machine.py`,
> `orchestrator/budget.py`, `llm/factory.py`, `llm/base.py`, `storage/models.py`
> (только `TaskStatus`, т.к. он "путешествует" через весь цикл).

---

## 0. Общая схема цепочки вызова одной LLM-операции

```
cli/main.py (ask/resume)
    │  raw_query: str | None, resume_task_id: str | None
    ▼
Orchestrator.__init__(settings)          — создаёт self.llm, self.extraction_client, self.budget
    │
Orchestrator.run(raw_query, resume_task_id, progress_cb, plan_confirm_cb, merge_confirm_cb)
    │  внутри создаёт/загружает TaskStatus (status) — общий "счётчик" вызовов на сессию
    ▼
roles/*.py  (outline_planner.build_plan, elaborator.elaborate_outline,
             vault_analyst.resolve_notes_against_vault, critic.run_critic_cycle → synthesizer_writer.write_note)
    │  role.generate_structured(role=<строка-тег>, prompt=<str>, response_model=<BaseModel>,
    │                            status=status, system_instruction=<str|None>)
    ▼
LLMClient Protocol (llm/base.py)         — общий контракт, которому соответствует GroqClient
    ▼
GroqClient.generate_structured(...)      — см. Часть 2
    │  внутри: self.budget.check_and_register_task_call/check_rpd_soft_limit/wait_if_needed_for_rpm/register_call
    ▼
HTTP-запрос к Groq API → JSON → Pydantic-модель response_model → возврат вверх по цепочке
```

Ключевой принцип: **всё, что передаётся между слоями — это либо примитивы
(`str`, `bool`), либо Pydantic-модели (`TaskStatus`, `response_model`)**. Сырые
объекты HTTP-ответов никогда не всплывают выше `GroqClient`.

---

## 1. `cli/main.py` — точка входа

### 1.1. `_run_and_report(orch, *, raw_query, resume_task_id, settings) -> None`

**Описание.** Общая обвязка для команд `ask` и `resume`: запускает
`Orchestrator.run(...)`, показывает прогресс в консоли, а после завершения —
либо сообщение об остановке (лимит бюджета), либо diff предложенных изменений.
Не содержит собственной бизнес-логики цикла LLM — только UI-обвязку и callback'и.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `orch` | `Orchestrator` | Уже созданный экземпляр оркестратора (см. §2.1). |
| `raw_query` | `str \| None` | Исходный запрос пользователя на естественном языке. `None` при `resume`. |
| `resume_task_id` | `str \| None` | `task_id` ранее остановленной задачи. `None` при новом запросе. |
| `settings` | `Settings` | Текущая конфигурация (см. `config/settings.py`), нужна только для вывода лимитов в консоль. |

**Возвращаемое значение:** `None` (пишет в консоль и может завершить процесс через `typer.Exit`).

**Исключения:** не перехватывает исключения `Orchestrator.run()` напрямую — сама функция
оборачивает вызов в `try/finally` только для гарантированного `orch.close()`.

**Внутренние callback'и, передаваемые в `orch.run(...)`:**
- `progress_cb(stage: str) -> None` — печатает строку прогресса в консоль.
- `plan_confirm_cb(plan: Plan) -> bool` — приостанавливает spinner, вызывает
  `cli/plan_editor.py::confirm_plan(plan)`, возвращает `True`/`False` (утверждён ли план).
- `merge_confirm_cb(drafts: list[DraftNote]) -> list[DraftNote]` — приостанавливает spinner,
  вызывает `cli/draft_merge_editor.py::confirm_merges(drafts, mode=settings.draft_merge_mode)`.

### 1.2. `ask(query: str) -> None`

**Описание.** CLI-команда (`python -m cli.main ask "..."`). Создаёт `Settings`,
создаёт `Orchestrator`, запускает `_run_and_report` с новым запросом.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `query` | `str` | Обязательный аргумент CLI — запрос пользователя на естественном языке. |

**Возвращаемое значение:** `None`.

**Исключения:** не ловит явно — падения `Orchestrator.__init__`/`run` всплывают как есть
(typer покажет traceback).

### 1.3. `resume(task_id: str) -> None`

**Описание.** CLI-команда для продолжения остановленной задачи. Отличие от `ask`:
`raw_query=None`, `resume_task_id=task_id`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | Идентификатор задачи из списка `resumable`. |

**Возвращаемое значение:** `None`.

**Исключения:** ловит `OrchestratorStopped` (например, чекпоинт не найден/битый) и
печатает панель ошибки, завершая процесс с кодом 1 (`typer.Exit(code=1)`).

### 1.4. `resumable()`, `approve(task_id)`, `pending()`, `index()`

Не участвуют в цикле LLM-вызова напрямую (approve пишет в Vault, index — просто
пересканирует Vault без LLM) — упомянуты здесь только для полноты картины CLI,
подробно не документируются в этом файле.

---

## 2. `orchestrator/state_machine.py` — `Orchestrator`

### 2.1. `class Orchestrator`

**Описание.** Не-LLM state machine. Владеет двумя LLM-клиентами
(`self.llm` — основной, `self.extraction_client` — для Elaborator/Extractor),
общим бюджетом вызовов, локальным индексом Vault (`self.db`) и эмбеддером.
Ни разу не пишет в реальный Vault (это делает `staging/commit.py` после `approve`).

#### `__init__(self, settings: Settings)`

**Описание.** Инициализирует всё, что нужно для одного запуска задачи:
проверяет `FREE_ONLY`, создаёт рабочие директории, открывает `VaultDB`,
пытается создать локальный эмбеддер, строит два `LLMBudget` (основной и
extraction) и через `llm/factory.py` создаёт два LLM-клиента.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `settings` | `Settings` (`config/settings.py`) | Единая конфигурация: провайдер (`llm_provider`), ключ Groq, лимиты, пути. |

**Возвращаемое значение:** —(конструктор).

**Исключения:**
- `RuntimeError` — если `settings.free_only=False` (через `settings.validate_free_only()`).
- `RuntimeError` — если `settings.groq_api_key` пуст и `llm_provider="groq"`
  (поднимается внутри `GroqClient.__init__`, см. Часть 2).

**Побочные эффекты:** создаёт директории (`workdir`, `staging_dir`, `checkpoint_dir`,
родителя `db_path`), открывает файл SQLite.

**Ключевые атрибуты после инициализации:**

| Атрибут | Тип | Назначение |
|---|---|---|
| `self.db` | `VaultDB` | Локальный индекс Vault (см. `vault/db.py`, не относится к циклу LLM). |
| `self.embedder` | `LocalEmbedder \| None` | Локальные эмбеддинги, не относится к циклу LLM. |
| `self.budget` | `LLMBudget` | Общий бюджет вызовов основного клиента. |
| `self.llm` | `LLMClient`-совместимый объект | Основной клиент — планирование, критик, vault-dedup, folder assignment, writer. |
| `self.extraction_budget` | `LLMBudget` | Отдельный бюджет для extraction/elaboration. |
| `self.extraction_client` | `LLMClient`-совместимый объект | Клиент для `roles/elaborator.py` / `roles/extractor_critic.py`. |

#### `close(self) -> None`

**Описание.** Закрывает соединение с `VaultDB`. Вызывается в `finally` в `cli/main.py`.

**Параметры:** нет.
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

#### `sync_vault_index(self) -> dict`

**Описание.** Инкрементальная переиндексация Vault. Не использует LLM.
Упомянута для полноты — вызывается в начале `run()` перед любыми LLM-шагами.

**Возвращаемое значение:** `dict` со статистикой (`scanned`, `updated`, `unchanged`, `removed`).

#### `run(self, raw_query=None, *, resume_task_id=None, progress_cb=None, plan_confirm_cb=None, merge_confirm_cb=None) -> RunResult`

**Описание.** Главный метод: выполняет весь workflow задачи до этапа `staging`
включительно, вызывая роли в фиксированном порядке. Каждый шаг персистится в
`TaskCheckpoint` сразу после успешного завершения (см. `staging/checkpoint.py`) —
это то, что делает `resume` возможным без повторной траты бюджета.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `raw_query` | `str \| None` | Запрос пользователя. Обязателен, если `resume_task_id is None`. |
| `resume_task_id` | `str \| None` | `task_id` для продолжения. Взаимоисключим с `raw_query` (по смыслу). |
| `progress_cb` | `Callable[[str], None] \| None` | Вызывается на каждом крупном шаге с человекочитаемой меткой. |
| `plan_confirm_cb` | `Callable[[Plan], bool] \| None` | Вызывается ровно один раз на задачу сразу после построения/загрузки `Plan`, **до** самого дорогого этапа (elaboration). `None` ⇒ план утверждается автоматически. |
| `merge_confirm_cb` | `Callable[[list[DraftNote]], list[DraftNote]] \| None` | Вызывается после Writer+Critic, **до** validation/staging, только если `settings.enable_draft_merging=True`. |

**Возвращаемое значение:** `RunResult` — dataclass:
```python
@dataclass
class RunResult:
    task_id: str
    changeset: StagingChangeset | None   # None, если задача остановлена
    status: TaskStatus                    # счётчик LLM-вызовов ЭТОЙ сессии
    stopped: bool                         # True — остановлена по бюджету/лимиту
    message: str                          # человекочитаемое сообщение
```

**Исключения, которые ловятся ВНУТРИ метода (не всплывают наружу):**
- `LLMFreeLimitReached` (`orchestrator/budget.py`) — исчерпан реальный/soft лимит API.
- `LLMTaskBudgetExceeded` (`orchestrator/budget.py`) — превышен `MAX_LLM_CALLS_PER_TASK`.

В обоих случаях `run()` сохраняет чекпоинт на последнем успешном шаге и
возвращает `RunResult(stopped=True, ...)`, а не поднимает исключение выше.

**Исключения, которые ПОДНИМАЮТСЯ наружу:**
- `OrchestratorStopped` — например, если `settings.research_mode == "web"`
  (сейчас не поддерживается), или если внутри `_load_or_create_state` не найден чекпоинт.
- `ValueError` — если `raw_query` не передан и `resume_task_id` тоже не передан.

**Порядок вызовов ролей внутри `run()` (только те, что дёргают LLM):**

| Шаг | Роль / функция | Клиент | `role=` (тег для GroqClient) |
|---|---|---|---|
| Planning | `outline_planner.build_plan(task, self.llm, status)` | `self.llm` | `"outline_planner"` |
| Elaborating | `elaborator.elaborate_outline(plan, self.extraction_client, status, ...)` | `self.extraction_client` | `"elaborator"` |
| Vault analysis (серая зона) | `vault_analyst.resolve_notes_against_vault(plan, searcher, self.llm, status, ...)` | `self.llm` | `"vault_dedup"`, `"folder_assignment"` |
| Synthesis (на каждую заметку) | `critic.run_critic_cycle(note, evidence, ..., self.llm, status, ...)` | `self.llm` | `"synthesizer_write"`, `"critic"` |

Все эти вызовы получают ОДИН и тот же объект `status: TaskStatus` — именно
через него ведётся общий счётчик `llm_calls_used` для `MAX_LLM_CALLS_PER_TASK`,
независимо от того, какой из двух клиентов (`self.llm`/`self.extraction_client`)
расходует бюджет.

**Локальная функция `persist(stage_label: str) -> None`** (определена внутри `run`):
сохраняет `TaskCheckpoint` на диск немедленно после шага. Вызывается очень часто
(после каждого батча elaboration, после каждой написанной заметки) — это и есть
механизм resume.

**Локальная функция `report(stage: str) -> None`:** просто прокси к `progress_cb`,
если он передан.

#### `_load_or_create_state(self, *, raw_query, resume_task_id, report) -> tuple[TaskCheckpoint, Task, TaskStatus, int]`

**Описание.** Приватный метод: либо загружает существующий `TaskCheckpoint` с диска
(при `resume`), либо создаёт новый `Task`/`TaskStatus`/`TaskCheckpoint` (при новом запросе).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `raw_query` | `str \| None` | См. `run()`. |
| `resume_task_id` | `str \| None` | См. `run()`. |
| `report` | `Callable[[str], None]` | Локальная функция логирования прогресса из `run()`. |

**Возвращаемое значение:** кортеж `(checkpoint, task, status, base_total_calls)`:

| Элемент | Тип | Назначение |
|---|---|---|
| `checkpoint` | `TaskCheckpoint` | Персистентное состояние задачи (см. `staging/checkpoint.py`). |
| `task` | `Task` | `task_id`, `raw_query`, `language`. |
| `status` | `TaskStatus` | **Счётчик вызовов ТЕКУЩЕЙ сессии** — обнуляется даже при resume. |
| `base_total_calls` | `int` | Сколько вызовов было потрачено во ВСЕХ предыдущих сессиях (только для отчёта). |

**Исключения:**
- `OrchestratorStopped` — если `resume_task_id` передан, но `load_checkpoint(...)`
  вернул `None` (чекпоинт не найден/битый/несовместимая версия).
- `ValueError` — если ни `raw_query`, ни `resume_task_id` не переданы.

**Важный нюанс про бюджет при resume** (дословно из докстринга модуля):
`MAX_LLM_CALLS_PER_TASK` — лимит на ОДНУ СЕССИЮ, а не на задачу за всё время жизни.
`status.llm_calls_used` обнуляется в начале каждого вызова `run()`, в т.ч. при resume.
Накопительный расход по всем попыткам хранится отдельно в
`checkpoint.total_llm_calls_used` — только для отчёта пользователю.

---

## 3. `orchestrator/budget.py` — контроль лимитов

### 3.1. `class LLMFreeLimitReached(Exception)`

**Описание.** Поднимается, когда бесплатный API провайдера возвращает устойчивую
429 после исчерпания retry, либо достигнут дневной soft-limit
(`check_rpd_soft_limit`). Ловится в `Orchestrator.run()`.

### 3.2. `class LLMTaskBudgetExceeded(Exception)`

**Описание.** Поднимается при достижении `MAX_LLM_CALLS_PER_TASK`
(`check_and_register_task_call`). Ловится в `Orchestrator.run()`.

### 3.3. `class LLMBudget`

**Описание.** Два независимых уровня защиты: (1) жёсткий потолок вызовов
на задачу; (2) локальный soft-throttle по RPM. НЕ является источником истины
по реальным лимитам API — источник истины — реальный ответ Groq (см. Часть 2,
`GroqRateLimitError`).

#### `__init__(self, max_calls_per_task: int, rpm_soft_limit: int, rpd_soft_limit: int)`

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `max_calls_per_task` | `int` | Жёсткий потолок вызовов LLM на одну задачу (сессию). Из `settings.max_llm_calls_per_task`. |
| `rpm_soft_limit` | `int` | Сколько вызовов допускается за скользящее окно 60 секунд (клиентский throttle). |
| `rpd_soft_limit` | `int` | Ориентировочный дневной лимит (soft, не источник истины). |

**Возвращаемое значение:** — (конструктор). Инициализирует `self._minute_window: deque[float]`
и `self._day_count = 0`.

#### `check_and_register_task_call(self, status: TaskStatus) -> None`

**Описание.** Проверяет, не превышен ли `max_calls_per_task`, используя
`status.llm_calls_used`. Вызывается GroqClient В НАЧАЛЕ `generate_structured`
(до сетевого вызова).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `status` | `TaskStatus` | Объект статуса ТЕКУЩЕЙ задачи/сессии, откуда читается `llm_calls_used`. |

**Возвращаемое значение:** `None`.

**Исключения:** `LLMTaskBudgetExceeded`, если `status.llm_calls_used >= self.max_calls_per_task`.

#### `wait_if_needed_for_rpm(self) -> None`

**Описание.** Простой локальный throttle: не более `rpm_soft_limit` вызовов
за скользящее окно 60 секунд. Блокирует поток через `time.sleep`, если нужно.
Best-effort — не заменяет реальную обработку 429 со стороны API.

**Параметры:** нет.
**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

#### `check_rpd_soft_limit(self) -> None`

**Описание.** Проверяет накопленный за объект `self._day_count` (живёт только
в памяти процесса, не персистится) против `rpd_soft_limit`.

**Параметры:** нет.
**Возвращаемое значение:** `None`.
**Исключения:** `LLMFreeLimitReached`, если `self._day_count >= self.rpd_soft_limit`.

#### `register_call(self, status: TaskStatus, role: str, ok: bool, error: str | None = None) -> None`

**Описание.** Фиксирует факт совершённого вызова: инкрементирует
`status.llm_calls_used`, добавляет запись в `status.llm_calls_log`,
инкрементирует внутренний дневной счётчик. Вызывается GroqClient ПОСЛЕ
каждого вызова (успешного или упавшего).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `status` | `TaskStatus` | Куда записать факт вызова. |
| `role` | `str` | Тег роли (напр. `"critic"`, `"synthesizer_write"`) — для трассируемости в логе. |
| `ok` | `bool` | Успешно ли завершился вызов. |
| `error` | `str \| None` | Текст ошибки, если `ok=False`. |

**Возвращаемое значение:** `None`.
**Исключения:** не поднимает.

---

## 4. `llm/factory.py` — единственная точка выбора провайдера

### 4.1. `create_llm_client(settings: Settings, budget: LLMBudget)`

**Описание.** Создаёт ОСНОВНОЙ LLM-клиент по значению `settings.llm_provider`.
Единственная точка ветвления по провайдеру во всей системе.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `settings` | `Settings` | Источник `llm_provider`, `groq_api_key`, и т.д. |
| `budget` | `LLMBudget` | Передаётся в `GroqClient` напрямую (для `openrouter` — игнорируется, см. ниже). |

**Возвращаемое значение:**
- `GroqClient` — если `settings.llm_provider == "groq"`.
- `RoleRoutingLLMClient` — если `settings.llm_provider == "openrouter"` (объект,
  реализующий тот же `generate_structured(...)`, но маршрутизирующий по роли
  на группы моделей-кандидатов; **не входит в объём Части 2**, т.к. это не GroqClient).

**Исключения:** `ValueError`, если `settings.llm_provider` не `"groq"` и не `"openrouter"`.

### 4.2. `create_extraction_llm_client(settings: Settings, budget: LLMBudget, primary_client=None)`

**Описание.** Создаёт ОТДЕЛЬНЫЙ клиент для ролей `elaborator`/`extractor_critic`
(самые частые по числу вызовов). На Groq использует
`settings.groq_extraction_model`/`settings.groq_extraction_tpm_limit` вместо
основных `groq_model`/`groq_tpm_limit`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `settings` | `Settings` | Источник конфигурации extraction-модели. |
| `budget` | `LLMBudget` | Отдельный бюджет для extraction-клиента (`Orchestrator.extraction_budget`). |
| `primary_client` | `LLMClient \| None` | Уже созданный основной клиент (`Orchestrator.self.llm`). Используется ТОЛЬКО для провайдера `"groq"`. |

**Возвращаемое значение:** новый `GroqClient` (или результат `create_llm_client`,
если провайдер не `"groq"`).

**Логика шаринга лимитера (важно для понимания бюджета токенов):**
если одновременно выполнены три условия —
1. `settings.groq_share_limiter_when_same_model` (дефолт `True`),
2. `settings.groq_extraction_model == settings.groq_model`,
3. `primary_client` — реальный экземпляр `GroqClient` —

то extraction-клиент получает `shared_limiter=primary_client._limiter` и
`shared_calibrator=primary_client._calibrator` — то есть **физически один и тот
же `TokenRateLimiter`/`TokenEstimateCalibrator`** используется обоими клиентами,
потому что они бьют в один и тот же реальный TPM-лимит Groq API. Если условия
не выполнены — создаётся полностью независимый `GroqClient` со своими объектами.

**Исключения:** не ловит собственных; может поднять то же, что `GroqClient.__init__`
(`RuntimeError` при отсутствии `groq_api_key`).

### 4.3. `budget_limits_for_provider(settings: Settings) -> tuple[int, int]`

**Описание.** Возвращает `(rpm_soft_limit, rpd_soft_limit)` для основного
`LLMBudget`, создаваемого в `Orchestrator.__init__`.

**Параметры:** `settings: Settings`.

**Возвращаемое значение:** `tuple[int, int]` — `(rpm, rpd)`.
Для `"groq"`: `(settings.groq_rpm_soft_limit, settings.groq_rpd_soft_limit)`.
Для `"openrouter"`: `(settings.openrouter_planning_rpm_soft_limit, settings.openrouter_planning_rpd_soft_limit)`.

**Исключения:** `ValueError` при неизвестном `llm_provider`.

### 4.4. `extraction_budget_limits(settings: Settings) -> tuple[int, int]`

**Описание.** То же самое, но для extraction-бюджета (`Orchestrator.extraction_budget`).

**Возвращаемое значение:** для `"groq"` — `(settings.groq_rpm_soft_limit, settings.groq_extraction_rpd_soft_limit)`.

**Исключения:** `ValueError` при неизвестном `llm_provider`.

---

## 5. `llm/base.py` — контракт `LLMClient`

### 5.1. `class LLMClient(Protocol)`

**Описание.** Структурная типизация (не проверяется в рантайме Python, только
для статического анализа/читаемости). И `GroqClient`, и `OpenRouterClient`,
и `RoleRoutingLLMClient` соответствуют этому протоколу по duck typing.
`roles/*.py` импортируют этот тип для аннотаций вместо конкретного класса клиента.

#### `generate_structured(self, *, role: str, prompt: str, response_model: type[T], status: TaskStatus, system_instruction: str | None = None) -> T`

**Описание.** Единственный метод контракта — отправляет один LLM-запрос и
возвращает распарсенный, провалидированный Pydantic-объект.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `role` | `str` | Строковый тег роли/этапа (напр. `"outline_planner"`, `"critic"`). Используется для: (а) логирования в `LLMCallLog`; (б) role-specific калибровки резерва output-токенов в GroqClient (см. Часть 2); (в) маршрутизации в `RoleRoutingLLMClient` (не относится к GroqClient напрямую). |
| `prompt` | `str` | Динамическая часть запроса — контекст конкретного вызова (собирается в `roles/*.py`). |
| `response_model` | `type[T]`, `T: BaseModel` | Pydantic-класс из `llm/schemas.py`, задающий JSON Schema ожидаемого ответа. |
| `status` | `TaskStatus` | Объект статуса текущей сессии — через него ведётся общий счёт `llm_calls_used`. |
| `system_instruction` | `str \| None` | Статичная системная инструкция роли (из `llm/prompts/*.py`). Опционально может быть переопределена вызывающим кодом (см. `synthesizer_writer.write_note`, где может подставляться вариант с `MERGE_AWARENESS_GUIDANCE`). |

**Возвращаемое значение:** `T` — экземпляр `response_model`, провалидированный Pydantic.

**Исключения:** не специфицированы Protocol'ом напрямую — фактический контракт
задаётся реализацией (см. Часть 2 для `GroqClient`: `LLMFreeLimitReached`,
`GroqSchemaError`, `GroqPromptTooLargeError` и т.д., все наследуются от
провайдер-нейтральных классов в `llm/common.py`).

---

## 6. `storage/models.py::TaskStatus` — что "путешествует" через весь цикл

### 6.1. `class TaskStatus(BaseModel)`

**Описание.** Единственный изменяемый (мутируемый на месте) объект, который
передаётся ПО ССЫЛКЕ через весь цикл `Orchestrator → роль → LLMClient → GroqClient`.
Именно поэтому `LLMBudget.check_and_register_task_call`/`register_call` могут
видеть накопленный расход независимо от того, через какой именно клиент
(`self.llm` или `self.extraction_client`) идёт конкретный вызов — оба получают
ОДИН и тот же объект `status` из `Orchestrator.run()`.

**Поля:**

| Поле | Тип | Назначение |
|---|---|---|
| `task_id` | `str` | Идентификатор задачи. |
| `stage` | `str` | Текущий этап workflow (`"planning"`, `"extracting"`, `"synthesizing"`, ...). |
| `llm_calls_used` | `int` | Счётчик вызовов **этой сессии** (см. §2.4 про resume). |
| `llm_calls_log` | `list[LLMCallLog]` | Список записей о каждом вызове (`role`, `timestamp`, `ok`, `error`). |
| `stopped_reason` | `str \| None` | Причина остановки (напр. текст `LLMFreeLimitReached`). |
| `finished` | `bool` | `True`, если задача успешно дошла до `staging`. |

**Где создаётся:** `Orchestrator._load_or_create_state()`, всегда заново
(`llm_calls_used=0`) — даже при `resume`.

---

## Что дальше (Часть 2)

Часть 2 (`docs_llm_cycle_part2_groq_client.md`) детально описывает:
- `GroqClient` — конструктор, `generate_structured`, все приватные методы;
- `TokenRateLimiter`, `TokenEstimateCalibrator`, `CacheObservability`;
- исключения `GroqRateLimitError`/`GroqSchemaError`/`GroqPromptTooLargeError`;
- `llm/common.py` — провайдер-нейтральная иерархия исключений и утилиты
  (`repair_json`, `estimate_tokens`, `chars_per_token`, `is_rate_limit_error` и др.),
  используемые `GroqClient` напрямую.
