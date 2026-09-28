# Документация: `llm/base.py`, `llm/common.py`, `llm/factory.py`

> Reference-док. Эти три файла — провайдер-нейтральный "фундамент" LLM-слоя:
> общий контракт (`base.py`), общие исключения/утилиты (`common.py`) и
> единственная точка выбора провайдера (`factory.py`). Конкретные провайдеры
> (`GroqClient`, `OpenRouterClient`, `RoleRoutingLLMClient`) — в
> `groq_client.md`. Сам процесс одного вызова — в `../flows/llm_cycle.md`.

---

## 1. `llm/base.py` — контракт `LLMClient`

### 1.1. `class LLMClient(Protocol)`

**Описание.** Структурная типизация (`typing.Protocol`) — не проверяется в
рантайме Python, только для статического анализа/читаемости. `roles/*.py`
импортируют этот тип для аннотаций вместо конкретного класса клиента —
переданный в роль клиент может быть любым из провайдеров, роли не должны
знать, какой именно (см. `llm/factory.py`, §3).

#### `generate_structured(self, *, role: str, prompt: str, response_model: type[T], status: TaskStatus, system_instruction: str | None = None) -> T`

**Описание.** Единственный метод контракта — отправляет один LLM-запрос и
возвращает распарсенный, провалидированный Pydantic-объект.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `role` | `str` | Строковый тег роли/этапа (напр. `"outline_planner"`, `"critic"`). Используется для: логирования (`LLMCallLog`, `../docs_storage_models.md §7.1`); role-специфичной калибровки резерва output-токенов в `GroqClient` (`groq_client.md`); маршрутизации в `RoleRoutingLLMClient` (`groq_client.md`). |
| `prompt` | `str` | Динамическая часть запроса — контекст конкретного вызова, собирается в `roles/*.py`. |
| `response_model` | `type[T]`, `T: BaseModel` | Pydantic-класс из `llm/schemas.py` (`../docs_llm_schemas_prompts_chunking.md §A`), задающий JSON Schema ожидаемого ответа. |
| `status` | `TaskStatus` | Объект статуса текущей сессии (`../docs_storage_models.md §7.2`) — через него ведётся общий счёт `llm_calls_used`. |
| `system_instruction` | `str \| None` | Статичная системная инструкция роли (`llm/prompts/*.py`, `../docs_llm_schemas_prompts_chunking.md §B`). Может быть переопределена вызывающим кодом. |

**Возвращаемое значение:** `T` — экземпляр `response_model`.

**Исключения:** не специфицированы Protocol'ом напрямую — фактический
контракт задаётся реализацией (см. `groq_client.md`:
`LLMFreeLimitReached`, `LLMSchemaError`, `LLMPromptTooLargeError` и т.д., все
наследуются от классов ниже, в §2).

---

## 2. `llm/common.py` — провайдер-нейтральный фундамент

**Назначение (из докстринга модуля).** Общий код для ВСЕХ провайдеров.
Правило проекта: роли и общий код (`llm/chunking.py`,
`../docs_llm_schemas_prompts_chunking.md §C`) должны ловить ТОЛЬКО типы
исключений отсюда (`LLMRateLimitError`/`LLMSchemaError`/
`LLMPromptTooLargeError`), никогда провайдер-специфичные классы напрямую
(`GroqRateLimitError` и т.п.) — это контракт по ошибкам, аналогичный
`LLMClient` по методам.

### 2.1. Иерархия исключений

```
Exception
 └── LLMError
      ├── LLMRateLimitError
      ├── LLMSchemaError
      ├── LLMPromptTooLargeError
      └── LLMProviderOverloadedError
```

#### `class LLMError(Exception)`

Общий базовый класс для всех ошибок любого клиента, реализующего
`LLMClient.generate_structured()`. Сам по себе не поднимается напрямую.

#### `class LLMRateLimitError(LLMError)`

**Описание.** 429 / rate limit от API провайдера, устойчивый после retry на
уровне клиента. На верхнем уровне оборачивается в
`orchestrator.budget.LLMFreeLimitReached` (`../orchestrator/budget.md §1`) —
единственное место, которое остаётся провайдер-специфичным по смыслу
обработки. Конкретные подклассы: `GroqRateLimitError`,
`OpenRouterRateLimitError` (`groq_client.md`).

**`__init__(self, message: str, retry_after: float | None = None)`**

| Имя | Тип | Назначение |
|---|---|---|
| `message` | `str` | Текст ошибки. |
| `retry_after` | `float \| None` | Секунды до следующей попытки, распарсенные из текста ошибки API (см. `parse_retry_after`, §2.4). `None`, если не указано явно. |

Атрибут после инициализации: `self.retry_after`.

#### `class LLMSchemaError(LLMError)`

**Описание.** Модель вернула JSON, невалидный по `response_model` даже
после repair-эвристик (`repair_json`, §2.3). Вызывающий код может
отреагировать уменьшением батча, а не просто повторным тем же запросом.
Конкретные подклассы: `GroqSchemaError`, `OpenRouterSchemaError`.

#### `class LLMPromptTooLargeError(LLMError)`

**Описание.** Промпт+система+ожидаемый output превышают доступный бюджет
контекста/TPM. Поднимается ДО (проактивно) или ВМЕСТО (реактивно, по 413)
сетевого вызова. **Намеренно НЕ входит** в список триггеров failover в
`llm/router.py::_FAILOVER_TRIGGERS` (`groq_client.md`) — слишком
большой промпт не повод пробовать другую модель того же класса задач, это
должен решать вызывающий код (чанкинг). Конкретные подклассы:
`GroqPromptTooLargeError`, `OpenRouterPromptTooLargeError`.

#### `class LLMProviderOverloadedError(LLMError)`

**Описание.** Апстрим-провайдер модели временно перегружен (напр.
OpenRouter `503`/`provider_overloaded`). Retryable и failover-triggering.
**Не используется `GroqClient`** (нет промежуточного апстрим-провайдера, как
у OpenRouter). Конкретный подкласс: `OpenRouterProviderOverloadedError`.

### 2.2. `WORD_DIGITS: dict[str, str]`

Константа: числа словами по-английски → строковые цифры (`{"zero": "0", ...,
"ten": "10"}`). Нужна для `repair_json` (§2.3) — слабые модели иногда
подставляют вместо цифр в JSON текст вида `"0. Nine"` вместо `0.9`.

### 2.3. `repair_json(raw: str) -> str`

**Описание.** Чинит два частых способа, которыми слабые модели ломают JSON:
markdown-обёртку (` ```json ... ``` `) и число словами после точки
(`"0. Nine"` → `"0.9"`). Не гарантирует валидность результата — вызывающий
код (`GroqClient._parse_with_repair`) обязан обернуть повторный
`model_validate_json` в `try/except`.

**Параметры:** `raw: str`.
**Возвращаемое значение:** `str` — исправленный (или неизменённый) текст.
**Исключения:** не поднимает.

### 2.4. Распознавание типа ошибки по тексту

Работают с `str(exc)`, т.к. и Groq, и OpenRouter в проекте построены поверх
одного и того же SDK-пакета `openai`.

#### `is_rate_limit_error(exc: Exception) -> bool`

`True`, если `str(exc).lower()` содержит `"429"`, `"rate limit"` или
`"rate_limit"`.

#### `is_request_too_large_error(exc: Exception) -> bool`

`True`, если содержит `"413"`, `"request_too_large"` или `"request entity
too large"`.

#### `parse_retry_after(message: str) -> float | None`

Извлекает число секунд из фразы вида `"Please try again in 10.02s"`
(`_RETRY_AFTER_RE = re.compile(r"try again in\s+([\d.]+)\s*s", re.IGNORECASE)`).
`None`, если паттерн не найден или не парсится как `float`.

### 2.5. Оценка числа токенов без внешних зависимостей

Грубая эвристика (не настоящий токенайзер) — коэффициенты подобраны
отдельно для кириллицы и латиницы (кириллица кодируется BPE-токенайзерами
существенно менее эффективно).

#### `chars_per_token(text, *, cyrillic_ratio_threshold=0.3, cyrillic_chars_per_token=2.3, latin_chars_per_token=4.0) -> float`

**Описание.** Считает символы в диапазоне `"а"-"я"`/`"ё"`, делит на
`len(text)`, сравнивает долю с порогом. Возвращает один из двух
коэффициентов. Пустой текст → `latin_chars_per_token`.

#### `estimate_tokens(text, *, cyrillic_ratio_threshold=0.3, cyrillic_chars_per_token=2.3, latin_chars_per_token=4.0) -> int`

**Описание.** `int(len(text) / chars_per_token(...)) + 1` (`+1` — ненулевая
оценка даже для короткого текста, грубая компенсация служебных токенов).
Пустой текст → `0`.

**Где переопределяются коэффициенты:** `GroqClient` читает их из
`settings.groq_chars_per_token_*` (см. `groq_client.md`,
`../docs_config_settings.md §3.5`) — дефолты здесь являются "хардкодом на
случай отсутствия Settings", не единственным источником истины.

**Кто вызывает `estimate_tokens`:** `llm/chunking.py::split_items_into_batches`
(`../docs_llm_schemas_prompts_chunking.md §C.1`), `GroqClient._estimate_tokens`,
`OpenRouterClient.available_prompt_budget_tokens` (с дефолтными
коэффициентами, не настраивается из Settings для OpenRouter).

---

## 3. `llm/factory.py` — единственная точка выбора провайдера

**Назначение.** `Orchestrator` вызывает эти функции один раз при старте —
дальше вся система работает с объектом, реализующим `generate_structured`,
не зная, какой конкретно провайдер используется.

### 3.1. `create_llm_client(settings: Settings, budget: LLMBudget)`

**Описание.** Создаёт ОСНОВНОЙ LLM-клиент по значению `settings.llm_provider`
— единственная точка ветвления по провайдеру во всей системе.

**Параметры:** `settings: Settings`, `budget: LLMBudget`
(`../orchestrator/budget.md §3`) — для `openrouter` игнорируется (см. §3.2).

**Возвращаемое значение:**
- `GroqClient` — если `llm_provider == "groq"`.
- `RoleRoutingLLMClient` — если `llm_provider == "openrouter"` (см.
  `groq_client.md`).

**Исключения:** `ValueError`, если `llm_provider` не `"groq"` и не
`"openrouter"` — сообщение прямо указывает, как добавить нового провайдера
(реализовать `generate_structured(...)` в `llm/<provider>_client.py` по
образцу `llm/groq_client.py`/`llm/openrouter_client.py`, добавить одну ветку
сюда).

### 3.2. `_create_openrouter_router(settings: Settings)` (приватная)

**Описание.** Строит `RoleRoutingLLMClient` поверх ДВУХ групп
моделей-кандидатов OpenRouter — planning (`settings.openrouter_planning_models`)
и writing (`settings.openrouter_writing_models`, замаплена только на роль
`"synthesizer_write"`). У каждой модели-кандидата свой `LLMBudget`, но один и
тот же soft-лимит применяется ко всем кандидатам своей группы (упрощение —
раздельная настройка per-модель добавила бы конфигурационный шум,
непропорциональный MVP). Все бюджеты пишут в общий `TaskStatus.llm_calls_used`,
так что `MAX_LLM_CALLS_PER_TASK` остаётся единым потолком на задачу.

**Параметры:** `settings: Settings`.

**Возвращаемое значение:** `RoleRoutingLLMClient`
(`default_client=planning_group`, `role_map={"synthesizer_write": writing_group}`,
`selection_mode=settings.openrouter_selection_mode`).

**Исключения:** пробрасывает то, что поднимет `OpenRouterClient.__init__`
(см. `groq_client.md`).

### 3.3. `create_extraction_llm_client(settings: Settings, budget: LLMBudget, primary_client=None)`

**Описание.** Создаёт ОТДЕЛЬНЫЙ клиент для ролей
`elaborator`/`extractor_critic` (самые частые по числу вызовов). На Groq
использует `settings.groq_extraction_model`/`groq_extraction_tpm_limit`
вместо основных `groq_model`/`groq_tpm_limit`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `settings` | `Settings` | Источник конфигурации extraction-модели. |
| `budget` | `LLMBudget` | Отдельный бюджет для extraction-клиента (`Orchestrator.extraction_budget`). |
| `primary_client` | `LLMClient \| None` | Уже созданный основной клиент (`Orchestrator.self.llm`). Используется ТОЛЬКО для провайдера `"groq"`. |

**Возвращаемое значение:** новый `GroqClient` (либо результат
`create_llm_client`, если провайдер не `"groq"`).

**Логика шаринга TPM-лимитера/калибратора** (важно для бюджета токенов): если
одновременно выполнены три условия — (1)
`settings.groq_share_limiter_when_same_model` (дефолт `True`), (2)
`settings.groq_extraction_model == settings.groq_model`, (3) `primary_client`
— реальный экземпляр `GroqClient` — то extraction-клиент получает
`shared_limiter=primary_client._limiter` и
`shared_calibrator=primary_client._calibrator`: физически ОДИН и тот же
`TokenRateLimiter`/`TokenEstimateCalibrator` используется обоими клиентами,
т.к. они бьют в один и тот же реальный TPM Groq API. Если условия не
выполнены — создаётся полностью независимый `GroqClient`. Подробности этих
классов — `groq_client.md`.

**Исключения:** не ловит собственных; может поднять то же, что
`GroqClient.__init__` (`RuntimeError` при отсутствии `groq_api_key`).

### 3.4. `budget_limits_for_provider(settings: Settings) -> tuple[int, int]`

**Описание.** Возвращает `(rpm_soft_limit, rpd_soft_limit)` для ОСНОВНОГО
`LLMBudget`, создаваемого в `Orchestrator.__init__`. Для `"openrouter"` этот
объект не передаётся реальным клиентам напрямую (см. §3.2, там свои
`LLMBudget` на кандидата) — значение здесь используется только как разумный
дефолт для неиспользуемого напрямую экземпляра `Orchestrator.budget`.

**Возвращаемое значение:** для `"groq"` —
`(settings.groq_rpm_soft_limit, settings.groq_rpd_soft_limit)`; для
`"openrouter"` —
`(settings.openrouter_planning_rpm_soft_limit, settings.openrouter_planning_rpd_soft_limit)`.

**Исключения:** `ValueError` при неизвестном `llm_provider`.

### 3.5. `extraction_budget_limits(settings: Settings) -> tuple[int, int]`

**Описание.** То же самое, но для extraction-бюджета
(`Orchestrator.extraction_budget`).

**Возвращаемое значение:** для `"groq"` —
`(settings.groq_rpm_soft_limit, settings.groq_extraction_rpd_soft_limit)`;
для `"openrouter"` — те же planning-лимиты, что и в §3.4 (нет отдельной
extraction-группы в OpenRouter — planning-модели обслуживают и эту роль).

**Исключения:** `ValueError` при неизвестном `llm_provider`.

---

## Сводная таблица: кто вызывает `llm/factory.py`

| Вызывающий код | Функция | Назначение |
|---|---|---|
| `Orchestrator.__init__` (`../orchestrator/state_machine.md §4`) | `budget_limits_for_provider`, `create_llm_client` | Основной клиент/бюджет. |
| `Orchestrator.__init__` | `extraction_budget_limits`, `create_extraction_llm_client(primary_client=self.llm)` | Extraction-клиент/бюджет, с возможным шарингом лимитера. |

Документация по `llm/base.py`, `llm/common.py`, `llm/factory.py` завершена.
Провайдеры (`GroqClient`, `OpenRouterClient`, `RoleRoutingLLMClient`) — см.
`groq_client.md`.
