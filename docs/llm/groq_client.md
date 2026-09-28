# Документация: `llm/groq_client.py`

> Reference-док. `GroqClient` — единственный реализованный в MVP провайдер
> `LLMClient` (`../llm/core.md §1`). `llm/router.py` (`RoleRoutingLLMClient`)
> и `llm/openrouter_client.py` (`OpenRouterClient`) существуют в кодовой базе
> как второй провайдер с failover, но пока намеренно не документируются
> отдельно — см. `../README.md` за статусом. Сам процесс одного вызова (что
> вызывает `GroqClient` и что вызывает его) — в `../flows/llm_cycle.md`.

---

## 1. Исключения `GroqClient`

### 1.1. `class GroqRateLimitError(LLMRateLimitError)`

**Описание.** Оборачивает 429 от Groq API для retry-логики `tenacity` (см.
декоратор `@retry` на `_call_with_retry`, §6.8). Наследует `retry_after` от
`LLMRateLimitError` (`../llm/core.md §2.1`).

**Конструктор:** не переопределён — используется
`LLMRateLimitError.__init__(message, retry_after=None)`.

### 1.2. `class GroqSchemaError(LLMSchemaError)`

**Описание.** JSON от модели невалиден даже после repair-попыток
(`../llm/core.md §2.3`). Поднимается в `_parse_with_repair` (§6.7).

### 1.3. `class GroqPromptTooLargeError(LLMPromptTooLargeError)`

**Описание.** Промпт+система+ожидаемый output превышают доступный TPM-
бюджет. Может подниматься проактивно (в `generate_structured`, до сетевого
вызова — если даже система+схема сами по себе не влезают) или реактивно (в
`_call_with_retry`, если Groq вернул реальный 413).

---

## 2. Вспомогательные функции модуля

### 2.1. `_STRICT_SCHEMA_SUPPORTED_MODELS: set[str]`

Константа-множество: `{"openai/gpt-oss-20b", "openai/gpt-oss-120b"}` —
модели Groq, поддерживающие строгий режим `json_schema` (constrained
decoding). Используется в `GroqClient.__init__` для выбора стратегии
форматирования ответа.

### 2.2. `_to_strict_json_schema(schema: dict) -> dict`

**Описание.** Рекурсивно приводит JSON Schema из
`response_model.model_json_schema()` (Pydantic) к виду, требуемому Groq
strict-режимом: у каждого object-узла `additionalProperties=False`, и
`required` содержит **все** ключи `properties` (Groq strict не
поддерживает частично опциональные объекты — поля с `default` в Pydantic
всё равно будут возвращены моделью явно, это не мешает валидации).

**Параметры:** `schema: dict` — результат `response_model.model_json_schema()`.

**Возвращаемое значение:** `dict` — новая схема (не мутирует входной
аргумент — `schema = dict(schema)` в начале).

**Исключения:** не поднимает.

**Что обходит рекурсивно:** `$defs`/`definitions` (вложенные Pydantic-
модели), `properties` объектных узлов, `items` (элементы массивов),
`anyOf`/`oneOf`/`allOf` (Pydantic v2 компилирует `Optional[X]` в `anyOf` с
веткой `{"type": "null"}`).

### 2.3. `_is_schema_unsupported_error(exc: Exception) -> bool`

**Описание.** Отличает "схема отклонена API" (известные проблемы
`gpt-oss-120b` с несовместимыми конструкциями JSON Schema, напр.
`regex`/`format: e164`) от прочих ошибок. На эту категорию имеет смысл
ОДНОКРАТНО откатиться на `json_object` в рамках того же вызова, а не ронять
всю задачу.

**Параметры:** `exc: Exception` — пойманное исключение из
`self._client.chat.completions.create(...)`.

**Возвращаемое значение:** `bool` — `True`, если `str(exc).lower()`
содержит один из маркеров: `"unsupported_feature"`, `"invalid json schema"`,
`"response_format"`, `"json_validate_failed"`, `"does not validate"`,
`"missing properties"`.

**Исключения:** не поднимает.

**Где используется:** `_call_with_retry` (§6.8) — при
`response_format.get("type") == "json_schema"` и переданном
`fallback_format` — рекурсивный вызов `_call_with_retry` с
`fallback_format`/`fallback_system`.

---

## 3. `class TokenEstimateCalibrator`

**Описание.** Адаптивная калибровка "наивной" (посимвольной,
`../llm/core.md §2.5`) оценки токенов **отдельно по каждой роли**. Совмещает
две независимые по природе калибровки:

- **prompt-ratio** (`_ratio_by_role`) — безразмерный множитель "во сколько
  раз наивная оценка ошиблась относительно реального расхода промпта".
  Применяется как коэффициент к новой наивной оценке.
- **output-EMA** (`_output_ema_by_role`) — абсолютное число токенов
  (`completion_tokens`), скользящее среднее реальной длины ответа модели по
  роли. Заменяет статичную константу `RESERVED_OUTPUT_TOKENS=1500` на всех
  ролях.

Это два разных словаря и два разных публичных метода намеренно — умножение
константы-резерва на "во сколько раз ошиблись во входе" было бы
концептуальной ошибкой (разные единицы измерения).

### 3.1. `__init__(self, ema_alpha=0.3, min_ratio=0.05, max_ratio=1.5, *, output_ema_alpha=0.3, output_min_floor=300)`

| Имя | Тип | Назначение |
|---|---|---|
| `ema_alpha` | `float` | Вес нового наблюдения в EMA для prompt-ratio. Из `settings.groq_calibration_ema_alpha`. |
| `min_ratio` | `float` | Нижняя граница калиброванного prompt-ratio. Из `settings.groq_calibration_min_ratio`. |
| `max_ratio` | `float` | Верхняя граница prompt-ratio. Из `settings.groq_calibration_max_ratio`. |
| `output_ema_alpha` | `float` (keyword-only) | Вес нового наблюдения в EMA для output-резерва. Из `settings.groq_output_calibration_ema_alpha`. |
| `output_min_floor` | `int` (keyword-only) | Минимальный калиброванный output-резерв в токенах. Из `settings.groq_reserved_output_min_tokens`. |

**Возвращаемое значение:** — (конструктор). `self._ratio_by_role: dict[str,
float] = {}`, `self._output_ema_by_role: dict[str, float] = {}`,
`self._lock = threading.Lock()`.

**Исключения:** не поднимает.

### 3.2. `correct(self, role: str, naive_estimate: int) -> int`

**Описание.** Возвращает скорректированную оценку общего расхода
(system+prompt+reserved) для роли. Без наблюдений по роли — возвращает
`naive_estimate` как есть (безопасный дефолт, эквивалентный поведению "до
калибровки").

**Возвращаемое значение:** `int` — `max(int(naive_estimate * ratio), 1)`,
либо `naive_estimate` без изменений. Потокобезопасен.

### 3.3. `observe(self, role: str, naive_estimate: int, actual_effective_tokens: int) -> None`

**Описание.** Обновляет EMA prompt-ratio для роли по факту реального
вызова. `actual_effective_tokens` — уже ПОСЛЕ вычета закэшированных Groq
токенов (если `settings.groq_account_for_prompt_cache=True`) — то, что
реально стоило роли по TPM-бюджету, а не формальный `prompt_tokens`.

**Возвращаемое значение:** `None`. Если `naive_estimate <= 0` — выходит без
изменений. `sample_ratio = actual_effective_tokens / naive_estimate`,
зажимается в `[min_ratio, max_ratio]`, затем EMA.

### 3.4. `reserved_output_tokens(self, role: str, default: int) -> int`

**Описание.** Сколько токенов резервировать под ответ модели для данной
роли. Без наблюдений — `default` неизменным (тот же уровень безопасности,
что у прежнего статичного `RESERVED_OUTPUT_TOKENS`). После первого
наблюдения — EMA реального `completion_tokens`, но не ниже
`output_min_floor`.

### 3.5. `observe_output(self, role: str, actual_completion_tokens: int) -> None`

**Описание.** Обновляет EMA реального `completion_tokens` для роли. Если
`actual_completion_tokens <= 0` — выходит без изменений.

---

## 4. `class CacheObservability`

**Описание.** Отслеживает ТОЛЬКО бинарный факт (был кэш-хит Groq prompt
cache или нет) по каждой роли — исключительно для диагностики/логов.
**Никогда** не участвует в расчёте бюджета токенов — в отличие от
`TokenEstimateCalibrator`, который реально влияет на резервируемый бюджет.

### 4.1. `__init__(self) -> None`

`self._calls_by_role: dict[str, int] = {}`, `self._hits_by_role: dict[str,
int] = {}`, `self._lock = threading.Lock()`.

### 4.2. `observe(self, role: str, cache_hit: bool) -> None`

Регистрирует один вызов роли и был ли он кэш-хитом. Потокобезопасен.

### 4.3. `hit_rate(self, role: str) -> float | None`

Доля кэш-хитов по роли; `None`, если по роли вообще не было вызовов.

### 4.4. `summary(self) -> dict[str, tuple[int, int]]`

Полная сводка: для каждой роли — `(hits, calls)`.

---

## 5. `class TokenRateLimiter`

**Описание.** Клиентский лимитер по токенам в минуту (TPM) — центральный
механизм `GroqClient`, а не опциональная деталь (реальный лимит Groq free
tier — 8000 TPM, самое узкое место среди RPM/RPD/TPM/TPD). Комбинирует:

1. **Sliding-window** — сумма зарезервированных токенов за последние 60
   секунд никогда не превышает `self._limit`.
2. **Leaky-bucket** — ограничивает равномерный темп расхода (`limit/60`
   токенов/сек) поверх sliding-window, +15% буфер гибкости.
3. **Adaptive safety margin** — `register_rate_limit_hit()` ужимает
   эффективный лимит сразу после РЕАЛЬНОГО 429, с плавным ("linear",
   дефолт) или ступенчатым ("step") восстановлением через
   `margin_recovery_seconds` секунд.

### 5.1. `__init__(self, tpm_limit, safety_margin=0.85, margin_penalty_factor=0.8, margin_min_penalty=0.5, margin_recovery_seconds=300.0, recovery_mode="linear")`

| Имя | Тип | Назначение |
|---|---|---|
| `tpm_limit` | `int` | Реальный TPM-лимит модели у провайдера. Из `settings.groq_tpm_limit`. |
| `safety_margin` | `float` | Множитель запаса от реального лимита (`0.85` = используем не более 85%). Из `settings.groq_limiter_safety_margin`. |
| `margin_penalty_factor` | `float` | На какую долю ужимается лимит за одно срабатывание 429 (`0.8` = минус 20%). Из `settings.groq_margin_penalty_factor`. |
| `margin_min_penalty` | `float` | Не даём штрафу уйти ниже этой доли от базового лимита даже при серии 429. Из `settings.groq_margin_min_penalty`. |
| `margin_recovery_seconds` | `float` | Через сколько секунд без новых 429 лимит полностью восстанавливается. Из `settings.groq_margin_recovery_seconds`. |
| `recovery_mode` | `Literal["step", "linear"]` | См. §5.3. Из `settings.groq_margin_recovery_mode` (дефолт `"linear"`). |

**Возвращаемое значение:** — (конструктор). `self._base_limit =
max(int(tpm_limit * safety_margin), 1)`, `self._limit = self._base_limit`,
`self._window: deque[tuple[float, int]] = deque()`, `self._lock =
threading.RLock()`, `self._margin_penalty = 1.0`.

### 5.2. `register_rate_limit_hit(self) -> None`

**Описание.** Вызывается `GroqClient` сразу после РЕАЛЬНОГО 429 от API (не
после локального throttle этого же лимитера). Ужимает эффективный
TPM-лимит: `self._margin_penalty = max(self._margin_penalty *
self._penalty_factor, self._min_penalty)`, пересчитывает `self._limit`,
фиксирует момент штрафа. Логирует `logger.warning(...)`. Потокобезопасен.

### 5.3. `_maybe_recover_margin(self, now: float) -> None` (приватный)

**Описание.** Пересчитывает `self._margin_penalty`/`self._limit` в
зависимости от `recovery_mode`:
- **`"step"`** — margin_penalty остаётся ужатым ПОЛНОСТЬЮ весь
  `margin_recovery_seconds`, затем мгновенно скачет на `1.0`.
- **`"linear"`** (дефолт) — линейно растёт от значения на момент штрафа к
  `1.0` пропорционально прошедшему времени. Устраняет "ступеньку" —
  исправление наблюдавшегося бага "система берёт 2000–3000 из 8000 токенов
  ещё 5 минут после одной 429".

Если `self._margin_penalty >= 1.0` или `self._last_penalty_at is None` —
выходит немедленно.

### 5.4. `_prune(self, now: float) -> int` (приватный)

Удаляет из `self._window` записи старше 60 секунд, возвращает сумму
токенов, оставшихся в окне.

### 5.5. `available_tokens(self) -> int`

**Описание.** Сколько токенов сейчас доступно с учётом sliding-window и
текущего margin-восстановления. Используется в
`GroqClient.available_prompt_budget_tokens(...)` (§6.3).

**Возвращаемое значение:** `int` — `max(self._limit - used, 0)`.
Потокобезопасен.

### 5.6. `wait_and_reserve(self, estimated_tokens: int) -> None`

**Описание.** Блокирует поток (`time.sleep`), пока не появится место в
sliding-window, резервирует место ОПТИМИСТИЧНО (по оценке, не по факту) —
реальный расход позже корректируется через `adjust_last_reservation`. В
цикле `while True` под `self._lock`: если `used + estimated_tokens <=
self._limit` — добавляет запись в окно и выходит; иначе спит порциями по
`min(sleep_for, 5.0)` секунд.

### 5.7. `adjust_last_reservation(self, actual_tokens: int) -> None`

Подменяет последний (оптимистичный) резерв в окне реальным эффективным
расходом — повышает точность бюджета со временем. Если окно пусто — ничего
не делает.

### 5.8. `force_wait(self, seconds: float) -> None`

Используется, когда Groq вернул 429 с явным `retry_after` — держит
блокировку (лок), чтобы никто другой не полез параллельно в это окно
(актуально при `groq_share_limiter_when_same_model=True`, см.
`../llm/core.md §3.3`). Спит не менее `0.1` секунды.

---

## 6. `class GroqClient`

**Описание.** Основная реализация `LLMClient` (`../llm/core.md §1`) для
провайдера Groq. Единственная точка входа к Groq API в проекте.

**Классовые константы:**

| Имя | Значение | Назначение |
|---|---|---|
| `DEFAULT_TPM_LIMIT` | `8000` | Fallback, если `settings.groq_tpm_limit` не задан. |
| `RESERVED_OUTPUT_TOKENS` | `1500` | Fallback class-level константа (реально используемое значение — из `settings.groq_reserved_output_tokens_default`). |

### 6.1. `__init__(self, settings, budget, *, shared_limiter=None, shared_calibrator=None, shared_cache_observability=None)`

**Описание.** Проверяет `FREE_ONLY` и наличие API-ключа, настраивает
коэффициенты оценки токенов из `Settings`, создаёт (или переиспользует
shared-версии) `TokenRateLimiter`/`TokenEstimateCalibrator`, определяет
поддержку strict JSON Schema для текущей модели, создаёт HTTP-клиент
`openai.OpenAI` поверх `httpx.Client` с keep-alive пулом.

| Имя | Тип | Назначение |
|---|---|---|
| `settings` | `Settings` | Источник всех конфигурационных значений. |
| `budget` | `LLMBudget` | Бюджет ЭТОГО клиента (`../orchestrator/budget.md §3`) — для основного и extraction-клиента это РАЗНЫЕ объекты, хотя оба пишут в один `TaskStatus`. |
| `shared_limiter` | `TokenRateLimiter \| None` (keyword-only) | Если передан — используется вместо создания нового (см. `../llm/core.md §3.3`). |
| `shared_calibrator` | `TokenEstimateCalibrator \| None` (keyword-only) | Аналогично, для калибратора. |
| `shared_cache_observability` | `CacheObservability \| None` (keyword-only) | Аналогично, для диагностики кэша. |

**Исключения:**
- `RuntimeError` — если `settings.free_only=False`.
- `RuntimeError` — если `settings.groq_api_key` пуст.

**Ключевые атрибуты после инициализации:** `self.settings`, `self.budget`,
`self._chars_per_token_kwargs` (три коэффициента из Settings),
`self._limiter`, `self._calibrator`, `self._reserved_output_default`,
`self._account_for_prompt_cache`, `self._cache_observability`,
`self._strict_schema_supported` (`bool`), `self._client`
(`openai.OpenAI`, `base_url="https://api.groq.com/openai/v1"`).

### 6.2. `_estimate_tokens(self, text: str) -> int` (приватный)

Тонкая обёртка над `llm.common.estimate_tokens` (`../llm/core.md §2.5`) с
коэффициентами из Settings.

### 6.3. `available_prompt_budget_tokens(self, system_instruction: str, response_model: type[BaseModel]) -> int`

**Описание.** Часть неявного расширенного контракта, которым пользуется
`llm/chunking.py::split_items_into_batches`
(`docs_llm_schemas_prompts_chunking.md §C.1`). Отвечает "сколько токенов
остаётся под сам текст промпта" — ДО того, как известна конкретная роль
батча в некоторых путях вызова, поэтому используется консервативный
дефолтный резерв (`self._reserved_output_default`), не заниженный
role-калиброванный.

**Возвращаемое значение:** `int` — `max(total_available - overhead -
reserved_output_default, 0)`, где `total_available =
self._limiter.available_tokens()`, `overhead = estimate_tokens(system_instruction)
+ estimate_tokens(schema_json)`.

### 6.4. `generate_structured(self, *, role, prompt, response_model, status, system_instruction=None) -> T`

**Описание.** Главный публичный метод — реализация `LLMClient`. Порядок
операций:

1. `self.budget.check_and_register_task_call(status)` — может поднять
   `LLMTaskBudgetExceeded`.
2. `self.budget.check_rpd_soft_limit()` — может поднять
   `LLMFreeLimitReached`.
3. `self.budget.wait_if_needed_for_rpm()` — блокирующий sleep при
   необходимости.
4. `self._build_response_format_and_system(...)` (§6.5) — строит
   `response_format` и полный текст системного промпта (+ fallback-вариант,
   если модель поддерживает strict-режим).
5. Считает `system_tokens`, получает `reserved_output =
   self._calibrator.reserved_output_tokens(role, default=...)`.
6. Вычисляет `max_prompt_tokens = self._limiter._limit - reserved_output -
   system_tokens`. Если `<= 200` — `GroqPromptTooLargeError`.
7. Если `estimate_tokens(prompt) > max_prompt_tokens` —
   `self._auto_truncate_prompt(...)` (§6.6).
8. `naive_estimate = system_tokens + prompt_tokens + reserved_output`;
   `calibrated_estimate = self._calibrator.correct(role, naive_estimate)`.
9. `self._call_with_retry(...)` (§6.8).
10. `self._parse_with_repair(raw_json, response_model)` (§6.7).
11. `self.budget.register_call(status, role=role, ok=True)`; если
    `completion_tokens` получен — `self._calibrator.observe_output(role,
    completion_tokens)`.
12. Возвращает распарсенный объект.

| Имя | Тип | Назначение |
|---|---|---|
| `role` | `str` (keyword-only) | Тег роли — влияет на калибровку резерва, передаётся в лог вызова. |
| `prompt` | `str` (keyword-only) | Динамический текст запроса. |
| `response_model` | `type[T]` (keyword-only) | Класс ожидаемой структуры ответа. |
| `status` | `TaskStatus` (keyword-only) | Объект статуса сессии. |
| `system_instruction` | `str \| None` (keyword-only, дефолт `None`) | Статичная инструкция роли. |

**Возвращаемое значение:** `T` — экземпляр `response_model`.

**Исключения:**
- `LLMTaskBudgetExceeded` — от `check_and_register_task_call`.
- `LLMFreeLimitReached` — от `check_rpd_soft_limit`, либо при перехвате
  `GroqRateLimitError` из `_call_with_retry` ("Свободный лимит Groq API
  исчерпан (устойчивая 429 после retry)").
- `GroqPromptTooLargeError` — если система+схема не влезают в бюджет.
- `GroqSchemaError` — пробрасывается как есть, после
  `self.budget.register_call(..., ok=False, ...)`.
- Любое другое `Exception` — пробрасывается как есть, предварительно
  фиксируется через `register_call(..., ok=False, error=str(exc))`.

### 6.5. `_build_response_format_and_system(self, response_model, system_instruction, *, force_json_object=False) -> tuple[dict, str]` (приватный)

**Описание.** Строит пару `(response_format, full_system_text)`. Два
режима:

- **Strict JSON Schema** (модель в `_STRICT_SCHEMA_SUPPORTED_MODELS` и
  `force_json_object=False`): `response_format = {"type": "json_schema",
  "json_schema": {"name": ..., "strict": True, "schema":
  _to_strict_json_schema(...)}}`. Схема НЕ дублируется текстом в промпте —
  Groq применяет её сам (constrained decoding).
- **JSON object + текстовая подсказка схемы** (остальные модели, либо
  `force_json_object=True` — fallback-путь после
  `_is_schema_unsupported_error`): `response_format = {"type":
  "json_object"}`, схема дописывается текстом в `full_system`.

**Исключения:** не поднимает.

### 6.6. `_auto_truncate_prompt(self, prompt: str, max_prompt_tokens: int, *, role: str) -> str` (приватный)

**Описание.** Safety-net на уровне клиента: автоматически укорачивает
`prompt` под доступный TPM-бюджет, режет С КОНЦА (инструкции/контекст
важнее хвоста текста — типичный паттерн промптов проекта: "тема + концепции
+ текст источника"), стараясь не рвать посреди слова/предложения (ищет
ближайший `\n` или `. ` в последних ~20% допустимой длины), добавляет
предупреждающий маркер в конец.

**Возвращаемое значение:** `str` — обрезанный текст + маркер, либо исходный
`prompt` без изменений, если он и так укладывается.

**Исключения:** не поднимает. Логирует `logger.warning(...)`.

**Важное замечание:** если это происходит часто — стоит добавить явный
чанкинг на уровне вызывающей роли (как в `roles/extractor_critic.py`,
`../docs_roles_part1.md §3`), чтобы не терять хвост текста автоматически.

### 6.7. `_parse_with_repair(self, raw_json: str, response_model: type[T]) -> T` (приватный)

**Описание.** Пытается распарсить `raw_json` как есть через
`response_model.model_validate_json(...)`; при неудаче применяет
`repair_json` (`../llm/core.md §2.3`) и пробует снова.

**Исключения:** `GroqSchemaError` — если ни первая попытка, ни попытка
после `repair_json` не увенчались успехом (в т.ч. если `repair_json`
вернул текст, идентичный исходному).

### 6.8. `_call_with_retry(self, *, prompt, system_instruction, estimated_tokens, response_format, fallback_format=None, fallback_system=None, role="", naive_estimate=0) -> tuple[str, int]` (приватный)

**Описание.** Собственно сетевой вызов Groq API, обёрнутый декоратором
`tenacity`:

```python
@retry(
    retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),  # GroqRateLimitError, APIConnectionError, APITimeoutError
    wait=wait_random_exponential(multiplier=1, max=15),
    stop=stop_after_attempt(4),
    reraise=True,
)
```

Внутри: `self._limiter.wait_and_reserve(estimated_tokens)` (блокирующий
throttle ДО сети), затем `self._client.chat.completions.create(...)`. После
успешного ответа: извлекает `usage` (если есть), считает `effective_tokens`
(с учётом или без учёта `cached_tokens`, в зависимости от
`self._account_for_prompt_cache`), вызывает
`self._limiter.adjust_last_reservation(effective_tokens)`,
`self._calibrator.observe(role, naive_estimate, effective_tokens)`,
`self._cache_observability.observe(role, cache_hit)`.

При исключении:
- Если это отказ схемы (`_is_schema_unsupported_error`) и есть
  `fallback_format` — рекурсивный вызов `_call_with_retry` с
  `fallback_format`/`fallback_system` (разовый откат на `json_object`).
- Если `is_rate_limit_error(exc)` (`../llm/core.md §2.4`) — при наличии
  `retry_after` вызывает `self._limiter.force_wait(retry_after)`, затем
  `self._limiter.register_rate_limit_hit()`, поднимает
  `GroqRateLimitError(str(exc), retry_after=retry_after)`.
- Если `is_request_too_large_error(exc)` — `GroqPromptTooLargeError`.
- Иначе — исходное исключение пробрасывается как есть (после
  `logger.exception(...)`).

**Возвращаемое значение:** `tuple[str, int]` — `(raw_json_content,
completion_tokens)`. `completion_tokens = 0`, если Groq не вернул `usage`.

**Исключения:**
- `GroqRateLimitError` — 429, после исчерпания retry `tenacity`.
- `GroqPromptTooLargeError` — реальный 413 от API.
- `RuntimeError` — если `content is None` (пустой ответ модели).
- Прочие сетевые исключения (`APIConnectionError`, `APITimeoutError`) —
  ретраятся `tenacity`, при исчерпании пробрасываются как есть.

---

## Сводная таблица параметров конфигурации, влияющих на `GroqClient`

| Поле `Settings` | Куда попадает | Назначение |
|---|---|---|
| `groq_api_key` | `__init__` (проверка) | Обязателен, иначе `RuntimeError`. |
| `groq_model` | `openai.chat.completions.create(model=...)` | Конкретная модель Groq. |
| `groq_timeout_seconds` | `_call_with_retry` (`timeout=...`) | HTTP-таймаут. |
| `groq_tpm_limit` | `TokenRateLimiter.__init__(tpm_limit=...)` | Реальный TPM-лимит модели. |
| `groq_limiter_safety_margin` | `TokenRateLimiter.__init__(safety_margin=...)` | Запас от реального лимита. |
| `groq_margin_penalty_factor` / `groq_margin_min_penalty` / `groq_margin_recovery_seconds` / `groq_margin_recovery_mode` | `TokenRateLimiter.__init__` | Параметры adaptive safety margin (§5.2–5.3). |
| `groq_calibration_ema_alpha` / `min_ratio` / `max_ratio` | `TokenEstimateCalibrator.__init__` | Калибровка prompt-ratio (§3.1–3.3). |
| `groq_output_calibration_ema_alpha` / `groq_reserved_output_min_tokens` | `TokenEstimateCalibrator.__init__` | Калибровка output-резерва (§3.4–3.5). |
| `groq_reserved_output_tokens_default` | `__init__` → `self._reserved_output_default` | Резерв "холодного старта" роли. |
| `groq_account_for_prompt_cache` | `__init__` → `self._account_for_prompt_cache` | Учитывать ли кэш-хиты в бюджете (по умолчанию — нет). |
| `groq_chars_per_token_cyrillic` / `_latin` / `groq_cyrillic_ratio_threshold` | `self._chars_per_token_kwargs` | Коэффициенты посимвольной оценки токенов. |
| `groq_share_limiter_when_same_model` | `../llm/core.md §3.3` | Шарить ли лимитер/калибратор между основным и extraction-клиентом. |
| `groq_extraction_model` / `groq_extraction_tpm_limit` / `groq_extraction_rpd_soft_limit` | `../llm/core.md §3.3` | Отдельная конфигурация extraction-клиента. |
| `max_llm_calls_per_task` | `LLMBudget.__init__` (не `GroqClient` напрямую) | Общий потолок вызовов на задачу. |
| `groq_rpm_soft_limit` / `groq_rpd_soft_limit` | `LLMBudget.__init__` | Soft-лимиты (`../orchestrator/budget.md §3`). |

Документация по `llm/groq_client.py` завершена. Путь одного вызова целиком
(`Orchestrator → роль → GroqClient → Groq API`) — см. `../flows/llm_cycle.md`.
