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

**Описание.** Клиентский лимитер по токенам в минуту (TPM), центральный механизм `GroqClient`. Реальный лимит Groq free tier для `openai/gpt-oss-120b` — 8000 TPM, самое узкое место среди RPM/RPD/TPM/TPD.

Окно **фиксированное, привязанное к календарной минуте** (как считает Groq Console): лимит действует в интервале `[hh:mm:00, hh:mm+1:00)` и полностью сбрасывается на границе минуты. Расход прошлой минуты в новую не переносится. Окно определяется настенными часами (`time.time()`), а не `time.monotonic()`, потому что монотонные часы не связаны с границами минут.

Два механизма:

1. **Фиксированное минутное окно.** Сумма токенов, зарезервированных в текущей календарной минуте, не превышает `self._limit`. Если места нет, поток ждёт начала следующей минуты плюс `boundary_margin_seconds`.
2. **Adaptive safety margin.** `register_rate_limit_hit()` ужимает `self._limit` после РЕАЛЬНОГО 429, затем лимит восстанавливается линейно (`"linear"`, дефолт) или ступенькой (`"step"`) за `margin_recovery_seconds`. Восстановление считается по `time.monotonic()`.

> **История.** До этой правки окно было скользящим (60 секунд от самой старой записи), из-за чего лимитер ждал дольше, чем нужно: если бюджет уходил в 13:01:05, окно освобождалось в 13:02:05, а у Groq лимит обнулялся в 13:02:00. Leaky-bucket в коде никогда не был реализован.

### 5.1. `__init__(self, tpm_limit, safety_margin=0.85, margin_penalty_factor=0.8, margin_min_penalty=0.5, margin_recovery_seconds=300.0, recovery_mode="linear", boundary_margin_seconds=0.5)`

| Имя | Тип | Назначение |
|---|---|---|
| `tpm_limit` | `int` | Реальный TPM-лимит модели. Из `settings.groq_tpm_limit`. |
| `safety_margin` | `float` | Множитель запаса (`0.85` = не более 85%). Из `settings.groq_limiter_safety_margin`. |
| `margin_penalty_factor` | `float` | Во сколько раз умножается `_margin_penalty` за одно срабатывание 429. |
| `margin_min_penalty` | `float` | Нижняя граница `_margin_penalty` при серии 429. |
| `margin_recovery_seconds` | `float` | Длительность восстановления после последнего 429. |
| `recovery_mode` | `str` | `"step"` или `"linear"`; любое другое значение молча заменяется на `"linear"`. |
| `boundary_margin_seconds` | `float` | Запас после границы минуты на рассинхрон часов с серверами Groq. **Не вынесен в `Settings`**: `GroqClient` его не передаёт, действует дефолт `0.5`. |

**Атрибуты:**
- `_base_limit = max(int(tpm_limit * safety_margin), 1)`, `_limit = _base_limit`;
- `_lock = threading.RLock()`;
- `_window_idx: int` — номер текущей календарной минуты (`int(time.time() // 60)`);
- `_used: int` — сколько токенов уже зарезервировано в этой минуте;
- `_last_reservation: tuple[int, int] | None` — `(номер минуты, размер резерва)` последней резервации;
- `_boundary_margin`;
- `_margin_penalty = 1.0`, `_penalty_value_at_last_hit = 1.0` (точка отсчёта линейного восстановления), `_last_penalty_at = None`.

**Исключения:** не поднимает.

### 5.2. `register_rate_limit_hit(self) -> None`

Без изменений. Вызывается `GroqClient` после РЕАЛЬНОГО 429 от API. Под `_lock`:
- `_margin_penalty = max(_margin_penalty * penalty_factor, min_penalty)`. Множитель применяется к ТЕКУЩЕМУ (возможно, частично восстановленному) значению, поэтому серия 429 накапливается вплоть до `min_penalty`;
- `_penalty_value_at_last_hit = _margin_penalty`, `_last_penalty_at = time.monotonic()`. Таймер восстановления стартует заново при каждом 429;
- `_limit = max(int(_base_limit * _margin_penalty), 1)`;
- `logger.warning(...)`.

### 5.3. `_maybe_recover_margin(self, now: float) -> None` (приватный)

Без изменений. `now` — значение `time.monotonic()`.
- **`"step"`**: пока `elapsed < recovery_seconds`, `_limit` не меняется; по истечении — полное восстановление.
- **`"linear"`**: `progress = min(elapsed / recovery_seconds, 1.0)`, `_margin_penalty = start + (1 - start) * progress`, `_limit` пересчитывается при каждом вызове.

### 5.4. `_roll_window(self, wall_now: float) -> None` (приватный)

Заменяет прежний `_prune`. Вычисляет `idx = int(wall_now // 60)`. Если `idx != _window_idx`, началась новая календарная минута: `_window_idx = idx`, `_used = 0`. Вызывать только под `_lock`.

### 5.5. `available_tokens(self) -> int`

Под `_lock`: `_maybe_recover_margin(time.monotonic())`, `_roll_window(time.time())`, возвращает `max(_limit - _used, 0)`, то есть **остаток текущей календарной минуты**. К концу минуты он может быть близок к нулю. **Не используйте его для планирования размера батчей**, для этого есть `capacity_tokens()` (§5.6). Блокируется только на время короткой проверки в другом потоке (сон в `wait_and_reserve` идёт вне блокировки), но по-прежнему ждёт, пока `force_wait` (§5.9) держит `_lock`.

### 5.6. `capacity_tokens(self) -> int`

**Описание.** Полная ёмкость календарной минуты с учётом текущего штрафа margin, **без вычета** уже потраченного. Под `_lock`: `_maybe_recover_margin(time.monotonic())`, возвращает `_limit`.

Нужна для планирования размера батчей. Остаток текущей минуты в её конце близок к нулю, хотя `wait_and_reserve()` дождётся следующей минуты и полный лимит снова станет доступен, поэтому размер батча должен зависеть от того, что влезет в ПУСТУЮ минуту. После реального 429 ёмкость уменьшается (возвращается `_limit`, а не `_base_limit`), так что батчи автоматически сужаются.

**Кто вызывает:** `GroqClient.available_prompt_budget_tokens` (§6.3).

### 5.7. `wait_and_reserve(self, estimated_tokens: int) -> None`

Блокирует поток, пока в ТЕКУЩЕЙ календарной минуте не появится место, и резервирует его ОПТИМИСТИЧНО (по оценке); фактический расход потом подменяет `adjust_last_reservation`.

Цикл `while True`; проверка выполняется под `with self._lock:`, **сон — вне блокировки**:
1. `_maybe_recover_margin`, `_roll_window`;
2. `fits = _used + estimated_tokens <= _limit`;
3. `oversized_but_window_empty = _used == 0 and estimated_tokens > _limit`. Запрос больше всего лимита не поместится даже в пустую минуту, поэтому он пропускается в пустое окно с `logger.warning`, иначе ожидание было бы вечным. Реальный отказ, если он будет, придёт от API как 413/429 и обработается клиентом;
4. если `fits` или `oversized_but_window_empty` — `_used += estimated_tokens`, `_last_reservation = (_window_idx, estimated_tokens)`, выход;
5. иначе `sleep_for = max(next_minute_start - wall_now + boundary_margin, 0.2)`, где `next_minute_start = (_window_idx + 1) * 60`. После выхода из блока `with` вызывается `time.sleep(min(sleep_for, 5.0))`, затем новая итерация. Кусками по 5 с спим, чтобы заново оценивать `_limit`: он может вырасти за счёт восстановления margin.

Поскольку сон идёт вне `_lock`, при общем лимитере основного и extraction-клиентов (`groq_share_limiter_when_same_model`) второй клиент и вызовы `available_tokens`, `capacity_tokens`, `adjust_last_reservation`, `register_rate_limit_hit` не блокируются на время ожидания.

**Исключения:** не поднимает.

### 5.8. `adjust_last_reservation(self, actual_tokens: int) -> None`

Под `_lock` заменяет оценочный резерв ПОСЛЕДНЕЙ резервации фактическим расходом (за вычетом закэшированных токенов, если включён `groq_account_for_prompt_cache`): `_used = max(_used - reserved + actual_tokens, 0)`, `_last_reservation = (idx, actual_tokens)`.

Если `_last_reservation is None` или минута резервации уже закончилась (`idx != _window_idx` после `_roll_window`), поправка не применяется: расход той минуты обнулён. Функция не знает, чья это резервация, поэтому при параллельных вызовах из двух клиентов последней может оказаться чужая (см. §5.10).

### 5.9. `force_wait(self, seconds: float) -> None`

Без изменений: под `_lock` делает `time.sleep(max(seconds, 0.1))`. Весь лимитер блокируется на время `retry_after`, пока ждёт этот поток. В отличие от `wait_and_reserve`, сон здесь остаётся ПОД блокировкой (намеренно: после реального 429 никто не должен резервировать окно).

### 5.10. Известные ограничения (по коду, не по докстрингам)

- **Граница минуты.** Запрос, зарезервированный в конце минуты, Groq может засчитать уже в следующую (сетевая задержка). `boundary_margin_seconds` и `safety_margin=0.85` риск снижают, но не устраняют. Если 429 на границах минут всё же повторяются, увеличьте `boundary_margin_seconds` до 1–2 с (потребует вынести параметр в `Settings` и `GroqClient.__init__`).
- **Предположение о часах.** Выравнивание сделано по `time.time()` (UTC-границы минут). Если Groq считает минуту иначе (например, от первого запроса), окно будет смещено.
- **Резервация без корректировки.** Если после `wait_and_reserve` запрос завершился исключением, запись остаётся в `_used` с оценочным значением до конца текущей минуты (раньше — до истечения 60 секунд). При откате на `json_object` (`_call_with_retry`) резервация делается повторно, первая остаётся.
- **Параллельные клиенты.** `_last_reservation` одна на лимитер, поэтому при общем лимитере двух клиентов `adjust_last_reservation` может поправить чужую резервацию.
- **Приватный доступ.** `GroqClient.generate_structured` читает `self._limiter._limit` напрямую.

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

**Описание.** Часть неявного расширенного контракта, которым пользуется `llm/chunking.py::split_items_into_batches` (`chunking.md §1`). Отвечает «сколько токенов остаётся под сам текст промпта».

Бюджет считается от **полной ёмкости** минутного окна (`TokenRateLimiter.capacity_tokens()`, §5.6), а **не от остатка текущей минуты** (`available_tokens()`). Причина: окно фиксированное и сбрасывается на границе минуты, а `wait_and_reserve()` при необходимости дождётся её. Если считать от остатка, то в конце минуты бюджет близок к нулю, `split_items_into_batches` уходит в ветку `text_budget_tokens <= 0` и режет список по одному элементу на батч, что тратит лишние вызовы (`MAX_LLM_CALLS_PER_TASK`). Цена решения: в худшем случае батч ждёт до ~60 с вместо лишних API-вызовов.

Метод вызывается ДО того, как известна конкретная роль батча, поэтому используется консервативный дефолтный резерв под output (`self._reserved_output_default`), а не role-калиброванный.

**Возвращаемое значение:** `int` — `max(capacity - overhead - reserved_output_default, 0)`, где `capacity = self._limiter.capacity_tokens()`, `overhead = estimate_tokens(system_instruction) + estimate_tokens(schema_json)`.

**Исключения:** не поднимает.

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
   system_tokens` (от ёмкости окна, не от остатка текущей минуты). Если `<= 200` — `GroqPromptTooLargeError`.
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
| — (не в `Settings`) | `TokenRateLimiter.__init__(boundary_margin_seconds=...)` | Запас после границы минуты, дефолт `0.5` с; `GroqClient` его не передаёт (см. §5.1, §5.10). |

Документация по `llm/groq_client.py` завершена. Путь одного вызова целиком
(`Orchestrator → роль → GroqClient → Groq API`) — см. `../flows/llm_cycle.md`.
