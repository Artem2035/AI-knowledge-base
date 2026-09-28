# Документация: `orchestrator/budget.py` — контроль лимитов LLM-провайдера

> Reference-док. Обзор пакета — `_index.md`. Кто вызывает эти классы —
> `state_machine.md` (`Orchestrator.__init__`) и `../llm/groq_client.md`
> (`GroqClient.generate_structured` вызывает методы `LLMBudget` на каждом
> запросе).

**Назначение (из докстринга модуля).** Два независимых уровня защиты,
провайдер-нейтральные (используются `GroqClient`, см.
`../llm/groq_client.md`):

1. `MAX_LLM_CALLS_PER_TASK` — жёсткий потолок на одну задачу/сессию, не
   связан напрямую с реальным лимитом API.
2. Локальный soft-throttle по RPM (ожидание перед вызовом, если недавно
   было много запросов) — снижает шанс словить 429, но НЕ источник истины.
   Источник истины — реальный ответ API (см. `../llm/groq_client.md`).

Ничего здесь не "чинит" исчерпание лимита переключением на платный tier —
только останавливает и сообщает.

## 1. `class LLMFreeLimitReached(Exception)`

**Описание.** Поднимается, когда бесплатный API провайдера возвращает
устойчивую 429 (после retry на уровне клиента), либо когда достигнут
дневной soft-limit (`check_rpd_soft_limit`, §3). Ловится в
`Orchestrator.run()` (`state_machine.md §4`).

## 2. `class LLMTaskBudgetExceeded(Exception)`

**Описание.** Поднимается при достижении `MAX_LLM_CALLS_PER_TASK`
(`check_and_register_task_call`, §3). Ловится в `Orchestrator.run()`
(`state_machine.md §4`).

## 3. `class LLMBudget`

### `__init__(self, max_calls_per_task: int, rpm_soft_limit: int, rpd_soft_limit: int)`

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `max_calls_per_task` | `int` | Жёсткий потолок вызовов LLM на одну задачу (сессию). Из `settings.max_llm_calls_per_task`. |
| `rpm_soft_limit` | `int` | Сколько вызовов допускается за скользящее окно 60 секунд (клиентский throttle). |
| `rpd_soft_limit` | `int` | Ориентировочный дневной лимит (soft, не источник истины). |

**Возвращаемое значение:** — (конструктор). Инициализирует
`self._minute_window: deque[float]` и `self._day_count = 0`.

**Исключения:** не поднимает.

### `check_and_register_task_call(self, status: TaskStatus) -> None`

**Описание.** Проверяет, не превышен ли `max_calls_per_task`, используя
`status.llm_calls_used`. Вызывается клиентом провайдера в начале
`generate_structured` (до сетевого вызова).

**Параметры:** `status: TaskStatus` (`../storage/models.md §7.2`) — объект
статуса текущей сессии, откуда читается `llm_calls_used`.

**Возвращаемое значение:** `None`.

**Исключения:** `LLMTaskBudgetExceeded`, если
`status.llm_calls_used >= self.max_calls_per_task`.

### `wait_if_needed_for_rpm(self) -> None`

**Описание.** Простой локальный throttle: не более `rpm_soft_limit` вызовов
за скользящее окно 60 секунд. Блокирует поток через `time.sleep`, если
нужно. Best-effort — не заменяет реальную обработку 429 со стороны API.

**Параметры:** нет. **Возвращаемое значение:** `None`. **Исключения:** не
поднимает.

### `check_rpd_soft_limit(self) -> None`

**Описание.** Проверяет накопленный `self._day_count` (живёт только в
памяти процесса, не персистится) против `rpd_soft_limit`.

**Параметры:** нет. **Возвращаемое значение:** `None`.

**Исключения:** `LLMFreeLimitReached`, если
`self._day_count >= self.rpd_soft_limit`.

### `register_call(self, status: TaskStatus, role: str, ok: bool, error: str | None = None) -> None`

**Описание.** Фиксирует факт совершённого вызова: инкрементирует
`status.llm_calls_used`, добавляет запись в `status.llm_calls_log`
(`../storage/models.md §7.1`, `LLMCallLog`), инкрементирует внутренний
дневной счётчик. Вызывается клиентом провайдера после каждого вызова
(успешного или упавшего).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `status` | `TaskStatus` | Куда записать факт вызова. |
| `role` | `str` | Тег роли (напр. `"critic"`, `"synthesizer_write"`) — для трассируемости в логе. |
| `ok` | `bool` | Успешно ли завершился вызов. |
| `error` | `str \| None` | Текст ошибки, если `ok=False`. |

**Возвращаемое значение:** `None`. **Исключения:** не поднимает.

---

Документация по `orchestrator/budget.py` завершена. `Orchestrator` — см.
`state_machine.md`.
