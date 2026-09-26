# Документация цикла LLM-вызова. Часть 3: `llm/groq_client.py` целиком

> Продолжение Частей 1–2 (`docs_llm_cycle_part1_orchestrator.md`,
> `docs_llm_cycle_part2_common.md`). Здесь — весь `llm/groq_client.py`: три
> исключения, две вспомогательные функции, три вспомогательных класса
> (`TokenEstimateCalibrator`, `CacheObservability`, `TokenRateLimiter`) и сам
> класс `GroqClient` (конструктор + все методы, публичные и приватные).

---

## 0. Место файла в цикле

```
Orchestrator.self.llm / self.extraction_client   ← экземпляры GroqClient
        │
роль (roles/*.py) вызывает:
        client.generate_structured(role=..., prompt=..., response_model=..., status=..., system_instruction=...)
        │
GroqClient.generate_structured(...)
        │  1) self.budget.check_and_register_task_call(status)   — бюджет задачи (Часть 1, §3.3)
        │  2) self.budget.check_rpd_soft_limit()
        │  3) self.budget.wait_if_needed_for_rpm()
        │  4) _build_response_format_and_system(...)             — строит JSON Schema / текстовую подсказку
        │  5) self._calibrator.reserved_output_tokens(role, ...)  — калиброванный резерв под ответ
        │  6) auto-truncate промпта, если он не влезает в TPM-бюджет
        │  7) self._calibrator.correct(role, naive_estimate)      — калиброванная оценка расхода
        │  8) self._call_with_retry(...)                          — реальный HTTP-запрос + retry
        │       │  self._limiter.wait_and_reserve(estimated_tokens)  — TPM-throttle ДО сети
        │       │  openai-SDK: self._client.chat.completions.create(...)
        │       │  self._limiter.adjust_last_reservation(effective_tokens)
        │       │  self._calibrator.observe(role, naive_estimate, effective_tokens)
        │       │  self._cache_observability.observe(role, cache_hit)
        │  9) self._parse_with_repair(raw_json, response_model)   — model_validate_json (+ repair_json fallback)
        │ 10) self.budget.register_call(status, role, ok, error)  — фиксация факта вызова
        ▼
Pydantic-объект response_model  →  вверх по цепочке до роли
```

---

## 1. Исключения `GroqClient`

### 1.1. `class GroqRateLimitError(LLMRateLimitError)`

**Описание.** Оборачивает 429 от Groq API для retry-логики `tenacity`
(см. декоратор `@retry` на `_call_with_retry`). Наследует `retry_after` от
`LLMRateLimitError` (Часть 2, §2.2).

**Конструктор:** не переопределён — используется `LLMRateLimitError.__init__(message, retry_after=None)`.

### 1.2. `class GroqSchemaError(LLMSchemaError)`

**Описание.** JSON от модели невалиден даже после repair-попыток
(`llm/common.py::repair_json`). Поднимается в `_parse_with_repair`.

**Конструктор:** не переопределён.

### 1.3. `class GroqPromptTooLargeError(LLMPromptTooLargeError)`

**Описание.** Промпт+система+ожидаемый output превышают доступный TPM-бюджет.
Может подниматься как проактивно (в `generate_structured`, до сетевого
вызова — если даже система+схема сами по себе не влезают), так и реактивно
(в `_call_with_retry`, если Groq вернул реальный 413).

**Конструктор:** не переопределён.

---

## 2. Вспомогательные функции модуля

### 2.1. `_STRICT_SCHEMA_SUPPORTED_MODELS: set[str]`

**Описание.** Константа-множество: `{"openai/gpt-oss-20b", "openai/gpt-oss-120b"}` —
модели Groq, поддерживающие строгий режим `json_schema` (constrained decoding).
Используется в `GroqClient.__init__` для выбора стратегии форматирования ответа.

### 2.2. `_to_strict_json_schema(schema: dict) -> dict`

**Описание.** Рекурсивно приводит JSON Schema из `response_model.model_json_schema()`
(Pydantic) к виду, требуемому Groq strict-режимом: у каждого object-узла
`additionalProperties=False`, и `required` содержит **все** ключи `properties`
(Groq strict не поддерживает частично опциональные объекты — поля с `default`
в Pydantic всё равно будут возвращены моделью явно, это не мешает валидации,
т.к. Pydantic просто примет присланное значение).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `schema` | `dict` | Результат `response_model.model_json_schema()` — сырая JSON Schema от Pydantic. |

**Возвращаемое значение:** `dict` — новая (не мутирует входной аргумент напрямую —
делает `schema = dict(schema)` в начале) схема, готовая для передачи в
`response_format.json_schema.schema`.

**Исключения:** не поднимает — чисто рекурсивное преобразование структуры.

**Что обходит рекурсивно:**
- `$defs`/`definitions` (вложенные Pydantic-модели, например `EvidenceItem`
  внутри `EvidenceBatchOutput`);
- `properties` объектных узлов;
- `items` (элементы массивов, например `list[EvidenceItem]`);
- `anyOf`/`oneOf`/`allOf` (ветки — Pydantic v2 компилирует `Optional[X]` в
  `anyOf` с веткой `{"type": "null"}`).

### 2.3. `_is_schema_unsupported_error(exc: Exception) -> bool`

**Описание.** Отличает "схема отклонена API" (известные проблемы `gpt-oss-120b`
с несовместимыми конструкциями JSON Schema, например `regex`/`format: e164`)
от прочих ошибок. На эту категорию имеет смысл ОДНОКРАТНО откатиться на
`json_object` (без constrained decoding) в рамках того же вызова, а не ронять
всю задачу.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `exc` | `Exception` | Пойманное исключение из `self._client.chat.completions.create(...)`. |

**Возвращаемое значение:** `bool` — `True`, если `str(exc).lower()` содержит
один из маркеров: `"unsupported_feature"`, `"invalid json schema"`,
`"response_format"`, `"json_validate_failed"`, `"does not validate"`,
`"missing properties"`.

**Исключения:** не поднимает.

**Где используется:** `_call_with_retry` — при `except Exception as exc:`, если
исходный `response_format.get("type") == "json_schema"` и `fallback_format`
передан — рекурсивный вызов `_call_with_retry` с `fallback_format`/`fallback_system`.

---

## 3. `class TokenEstimateCalibrator`

**Описание.** Адаптивная калибровка "наивной" (посимвольной, см. Часть 2, §5)
оценки токенов **отдельно по каждой роли** (`role`). Совмещает две независимые
по природе калибровки в одном объекте:

- **prompt-ratio** (`_ratio_by_role`) — безразмерный множитель "во сколько раз
  наивная оценка ошиблась относительно реального расхода промпта". Применяется
  как коэффициент к новой наивной оценке.
- **output-EMA** (`_output_ema_by_role`) — абсолютное число токенов
  (`completion_tokens`), скользящее среднее реальной длины ответа модели по роли.
  Заменяет статичную константу `RESERVED_OUTPUT_TOKENS=1500` на всех ролях.

Это два разных словаря и два разных публичных метода намеренно — умножение
константы-резерва на "во сколько раз ошиблись во входе" было бы концептуальной
ошибкой (разные единицы измерения).

### 3.1. `__init__(self, ema_alpha=0.3, min_ratio=0.05, max_ratio=1.5, *, output_ema_alpha=0.3, output_min_floor=300)`

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `ema_alpha` | `float` | Вес нового наблюдения в EMA для prompt-ratio. Выше — быстрее адаптация, но шумнее. Из `settings.groq_calibration_ema_alpha`. |
| `min_ratio` | `float` | Нижняя граница, до которой может опускаться калиброванный prompt-ratio. Из `settings.groq_calibration_min_ratio`. |
| `max_ratio` | `float` | Верхняя граница prompt-ratio. Из `settings.groq_calibration_max_ratio`. |
| `output_ema_alpha` | `float` (keyword-only) | Вес нового наблюдения в EMA для output-резерва. Из `settings.groq_output_calibration_ema_alpha`. |
| `output_min_floor` | `int` (keyword-only) | Минимальный калиброванный output-резерв в токенах, даже после серии коротких ответов. Из `settings.groq_reserved_output_min_tokens`. |

**Возвращаемое значение:** — (конструктор). Создаёт `self._ratio_by_role: dict[str, float] = {}`,
`self._output_ema_by_role: dict[str, float] = {}`, `self._lock = threading.Lock()`.

**Исключения:** не поднимает.

### 3.2. `correct(self, role: str, naive_estimate: int) -> int`

**Описание.** Возвращает скорректированную оценку общего расхода (system+prompt+reserved)
для роли. Пока нет ни одного наблюдения по роли — возвращает `naive_estimate`
как есть (безопасный дефолт, совпадает с поведением "до калибровки").

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `role` | `str` | Тег роли, для которой ищется калиброванный коэффициент. |
| `naive_estimate` | `int` | Посимвольная оценка (`system_tokens + prompt_tokens + reserved_output`). |

**Возвращаемое значение:** `int` — `max(int(naive_estimate * ratio), 1)`, либо
`naive_estimate` без изменений, если наблюдений по роли ещё не было.

**Исключения:** не поднимает. Потокобезопасен (`self._lock`).

### 3.3. `observe(self, role: str, naive_estimate: int, actual_effective_tokens: int) -> None`

**Описание.** Обновляет EMA prompt-ratio для роли по факту реального вызова.
`actual_effective_tokens` — уже **после** вычета закэшированных Groq токенов
(если `settings.groq_account_for_prompt_cache=True`) — то есть то, что реально
стоило роли по TPM-бюджету, а не формальный `prompt_tokens` из ответа API.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `role` | `str` | Тег роли. |
| `naive_estimate` | `int` | Та же посимвольная оценка, что была передана в `correct(...)` для этого вызова. |
| `actual_effective_tokens` | `int` | Реальный расход по TPM (см. `_call_with_retry`, переменная `effective_tokens`). |

**Возвращаемое значение:** `None`. Если `naive_estimate <= 0` — выходит без изменений
(защита от деления на ноль/бессмысленного наблюдения).

**Исключения:** не поднимает.

**Логика:** `sample_ratio = actual_effective_tokens / naive_estimate`, зажимается в
`[min_ratio, max_ratio]`, затем EMA: `new_ratio = ema_alpha * sample_ratio + (1 - ema_alpha) * prev`
(либо `sample_ratio`, если `prev is None` — первое наблюдение).

### 3.4. `reserved_output_tokens(self, role: str, default: int) -> int`

**Описание.** Сколько токенов резервировать под ответ модели для данной роли.
Без наблюдений — возвращает `default` неизменным (тот же уровень безопасности,
что был у прежнего статичного `RESERVED_OUTPUT_TOKENS`). После первого
наблюдения — EMA реального `completion_tokens`, но не ниже `output_min_floor`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `role` | `str` | Тег роли. |
| `default` | `int` | Резерв "холодного старта" — обычно `self._reserved_output_default` из `GroqClient`. |

**Возвращаемое значение:** `int`.

**Исключения:** не поднимает.

### 3.5. `observe_output(self, role: str, actual_completion_tokens: int) -> None`

**Описание.** Обновляет EMA реального количества `completion_tokens` для роли.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `role` | `str` | Тег роли. |
| `actual_completion_tokens` | `int` | Реальное число токенов ответа из `usage.completion_tokens`. |

**Возвращаемое значение:** `None`. Если `actual_completion_tokens <= 0` — выходит без изменений.

**Исключения:** не поднимает.

---

## 4. `class CacheObservability`

**Описание.** Отслеживает **только бинарный факт** (был кэш-хит Groq prompt cache
или нет) по каждой роли — исключительно для диагностики/логов. **Никогда** не
участвует в расчёте бюджета токенов (это явно прописано в докстринге исходного
кода) — в отличие от `TokenEstimateCalibrator`, который реально влияет на
резервируемый бюджет.

### 4.1. `__init__(self) -> None`

**Возвращаемое значение:** — (конструктор). Создаёт `self._calls_by_role: dict[str, int] = {}`,
`self._hits_by_role: dict[str, int] = {}`, `self._lock = threading.Lock()`.

### 4.2. `observe(self, role: str, cache_hit: bool) -> None`

**Описание.** Регистрирует один вызов роли и был ли он кэш-хитом.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `role` | `str` | Тег роли. |
| `cache_hit` | `bool` | `True`, если `usage.prompt_tokens_details.cached_tokens > 0`. |

**Возвращаемое значение:** `None`. Потокобезопасен.

### 4.3. `hit_rate(self, role: str) -> float | None`

**Описание.** Доля кэш-хитов по роли.

**Параметры:** `role: str`.
**Возвращаемое значение:** `float | None` — `hits/calls`, либо `None`, если по роли вообще не было вызовов.

### 4.4. `summary(self) -> dict[str, tuple[int, int]]`

**Описание.** Полная сводка: для каждой роли — `(hits, calls)`.

**Возвращаемое значение:** `dict[str, tuple[int, int]]`.

---

## 5. `class TokenRateLimiter`

**Описание.** Клиентский лимитер по токенам в минуту (TPM) — центральный механизм
`GroqClient`, а не опциональная деталь (реальный лимит Groq free tier — 8000 TPM,
самое узкое место среди RPM/RPD/TPM/TPD, см. `docs/architecture.md §5.1`).
Комбинирует три механизма:

1. **Sliding-window** — сумма зарезервированных токенов за последние 60 секунд
   никогда не превышает `self._limit`. Сам по себе не запрещает потратить весь
   лимит одним всплеском в начале окна.
2. **Leaky-bucket** — ограничивает равномерный темп расхода
   (`limit/60` токенов/сек) поверх sliding-window, +15% буфер гибкости.
   *(Согласно докстрингу исходника — упоминается как "вариант 4"; фактически
   реализация ниже опирается в первую очередь на sliding-window в
   `wait_and_reserve`/`_prune`, leaky-bucket описан в докстринге модуля как
   концептуальный дизайн этого же метода.)*
3. **Adaptive safety margin** — `register_rate_limit_hit()` ужимает эффективный
   лимит сразу после РЕАЛЬНОГО 429, с плавным ("linear", дефолт) или
   ступенчатым ("step") восстановлением через `margin_recovery_seconds` секунд.

### 5.1. `__init__(self, tpm_limit, safety_margin=0.85, margin_penalty_factor=0.8, margin_min_penalty=0.5, margin_recovery_seconds=300.0, recovery_mode="linear")`

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `tpm_limit` | `int` | Реальный TPM-лимит модели у провайдера (например, `8000`). Из `settings.groq_tpm_limit`. |
| `safety_margin` | `float` | Множитель запаса от реального лимита (`0.85` = используем не более 85%). Из `settings.groq_limiter_safety_margin`. |
| `margin_penalty_factor` | `float` | На какую долю ужимается эффективный лимит за одно срабатывание 429 (`0.8` = минус 20%). Из `settings.groq_margin_penalty_factor`. |
| `margin_min_penalty` | `float` | Не даём множителю штрафа уйти ниже этой доли от базового лимита даже при серии 429. Из `settings.groq_margin_min_penalty`. |
| `margin_recovery_seconds` | `float` | Через сколько секунд без новых 429 лимит полностью восстанавливается. Из `settings.groq_margin_recovery_seconds`. |
| `recovery_mode` | `Literal["step", "linear"]` | Режим восстановления после штрафа (см. §5.3). Из `settings.groq_margin_recovery_mode` (дефолт `"linear"`). |

**Возвращаемое значение:** — (конструктор). Ключевые атрибуты:
`self._base_limit = max(int(tpm_limit * safety_margin), 1)`, `self._limit = self._base_limit`,
`self._window: deque[tuple[float, int]] = deque()`, `self._lock = threading.RLock()`,
`self._margin_penalty = 1.0`.

**Исключения:** не поднимает.

### 5.2. `register_rate_limit_hit(self) -> None`

**Описание.** Вызывается `GroqClient` сразу после **реального** 429 от API
(не после локального throttle этого же лимитера). Ужимает эффективный
TPM-лимит: `self._margin_penalty = max(self._margin_penalty * self._penalty_factor, self._min_penalty)`,
пересчитывает `self._limit = max(int(self._base_limit * self._margin_penalty), 1)`,
фиксирует момент штрафа (`self._last_penalty_at`, `self._penalty_value_at_last_hit`).

**Параметры:** нет.
**Возвращаемое значение:** `None`. Логирует `logger.warning(...)`.
**Исключения:** не поднимает. Потокобезопасен.

### 5.3. `_maybe_recover_margin(self, now: float) -> None` (приватный)

**Описание.** Пересчитывает `self._margin_penalty`/`self._limit` в зависимости
от `recovery_mode`:
- **`"step"`** — margin_penalty остаётся ужатым полностью весь `margin_recovery_seconds`,
  затем мгновенно скачет на `1.0`.
- **`"linear"`** (дефолт) — линейно растёт от значения на момент штрафа
  (`self._penalty_value_at_last_hit`) к `1.0` пропорционально прошедшему времени.
  Устраняет "ступеньку" эффекта — именно это и есть исправление наблюдавшегося
  бага "система берёт 2000–3000 из 8000 токенов ещё 5 минут после одной 429".

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `now` | `float` | Текущее время `time.monotonic()`. |

**Возвращаемое значение:** `None`. Если `self._margin_penalty >= 1.0` или
`self._last_penalty_at is None` — выходит немедленно (нечего восстанавливать).

**Исключения:** не поднимает.

### 5.4. `_prune(self, now: float) -> int` (приватный)

**Описание.** Удаляет из `self._window` записи старше 60 секунд, возвращает
сумму токенов, оставшихся в окне (уже потраченный/зарезервированный расход
за последнюю "минуту").

**Параметры:** `now: float` — `time.monotonic()`.
**Возвращаемое значение:** `int` — сумма `tokens` по оставшимся записям окна.
**Исключения:** не поднимает.

### 5.5. `available_tokens(self) -> int`

**Описание.** Сколько токенов сейчас доступно с учётом sliding-window и
текущего margin-восстановления. Используется, например, в
`GroqClient.available_prompt_budget_tokens(...)`.

**Параметры:** нет.
**Возвращаемое значение:** `int` — `max(self._limit - used, 0)`.
**Исключения:** не поднимает. Потокобезопасен.

### 5.6. `wait_and_reserve(self, estimated_tokens: int) -> None`

**Описание.** Блокирует поток (`time.sleep`), пока не появится место в
sliding-window, затем резервирует место **оптимистично** (по оценке, не по
факту) — реальный расход позже корректируется через `adjust_last_reservation`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `estimated_tokens` | `int` | Калиброванная оценка полного расхода вызова (`GroqClient._calibrator.correct(...)`). |

**Возвращаемое значение:** `None`.

**Исключения:** не поднимает напрямую (сам по себе не связан с сетью — только сон).

**Логика цикла:** в бесконечном `while True` под `self._lock`: пересчитывает
`used = self._prune(now)`; если `used + estimated_tokens <= self._limit` —
добавляет запись в окно и выходит; иначе — вычисляет `sleep_for` до
освобождения места (по самой старой записи окна) и спит `min(sleep_for, 5.0)`
секунд (сон порциями по 5 сек, чтобы не держать лок слишком долго за раз —
хотя фактически лок держится на всём протяжении цикла в текущей реализации).

### 5.7. `adjust_last_reservation(self, actual_tokens: int) -> None`

**Описание.** Подменяет последний (оптимистичный) резерв в окне реальным
эффективным расходом (уже за вычетом закэшированных Groq-токенов, если это
включено настройкой) — повышает точность и sliding-window, и leaky-bucket
бюджета со временем.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `actual_tokens` | `int` | Реальный (эффективный) расход последнего вызова. |

**Возвращаемое значение:** `None`. Если окно пусто — ничего не делает.
**Исключения:** не поднимает.

### 5.8. `force_wait(self, seconds: float) -> None`

**Описание.** Используется, когда Groq всё же вернул 429 с явным `retry_after` —
держит блокировку (лок), чтобы никто другой не полез параллельно в это окно
времени (актуально при многопоточном использовании одного лимитера, например
основного и extraction-клиента при `groq_share_limiter_when_same_model=True`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `seconds` | `float` | Сколько секунд ждать (обычно значение `retry_after` из `GroqRateLimitError`). |

**Возвращаемое значение:** `None`. Спит не менее `0.1` секунды (`max(seconds, 0.1)`).
**Исключения:** не поднимает.

---

## 6. `class GroqClient`

**Описание.** Основная реализация `llm/base.py::LLMClient` для провайдера Groq.
Единственная точка входа к Groq API в проекте — все вызовы идут через
`generate_structured(...)`.

**Классовые константы:**

| Имя | Значение | Назначение |
|---|---|---|
| `DEFAULT_TPM_LIMIT` | `8000` | Fallback, если `settings.groq_tpm_limit` не задан (`getattr(settings, "groq_tpm_limit", None)`). |
| `RESERVED_OUTPUT_TOKENS` | `1500` | Fallback class-level константа для обратной совместимости (реально используемое значение приходит из `settings.groq_reserved_output_tokens_default`). |

### 6.1. `__init__(self, settings, budget, *, shared_limiter=None, shared_calibrator=None, shared_cache_observability=None)`

**Описание.** Создаёт клиента: проверяет `FREE_ONLY` и наличие API-ключа,
настраивает коэффициенты оценки токенов из `Settings`, создаёт (или
переиспользует shared-версии) `TokenRateLimiter`/`TokenEstimateCalibrator`,
определяет поддержку strict JSON Schema для текущей модели, создаёт HTTP-клиент
`openai.OpenAI` поверх `httpx.Client` с настроенным keep-alive пулом.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `settings` | `Settings` | Источник всех конфигурационных значений (модель, TPM-лимит, коэффициенты калибровки и т.д.). |
| `budget` | `LLMBudget` | Бюджет вызовов ЭТОГО клиента (Часть 1, §3.3) — для основного клиента и extraction-клиента это РАЗНЫЕ объекты `LLMBudget`, хотя оба пишут в один и тот же `TaskStatus`. |
| `shared_limiter` | `TokenRateLimiter \| None` (keyword-only) | Если передан — используется вместо создания нового (см. `llm/factory.py::create_extraction_llm_client`, Часть 1, §4.2). |
| `shared_calibrator` | `TokenEstimateCalibrator \| None` (keyword-only) | Аналогично, для калибратора. |
| `shared_cache_observability` | `CacheObservability \| None` (keyword-only) | Аналогично, для диагностики кэша. |

**Возвращаемое значение:** — (конструктор).

**Исключения:**
- `RuntimeError` — если `settings.free_only=False` (через `settings.validate_free_only()`).
- `RuntimeError` — если `settings.groq_api_key` пуст.

**Ключевые атрибуты после инициализации:**

| Атрибут | Назначение |
|---|---|
| `self.settings`, `self.budget` | Сохранённые входные параметры. |
| `self._chars_per_token_kwargs` | `dict` с тремя коэффициентами из Settings — передаются в `estimate_tokens`/`chars_per_token`. |
| `self._limiter` | `TokenRateLimiter` (новый или shared). |
| `self._calibrator` | `TokenEstimateCalibrator` (новый или shared). |
| `self._reserved_output_default` | `int`, из `settings.groq_reserved_output_tokens_default`. |
| `self._account_for_prompt_cache` | `bool`, из `settings.groq_account_for_prompt_cache`. |
| `self._cache_observability` | `CacheObservability` (новый или shared). |
| `self._strict_schema_supported` | `bool` — `settings.groq_model in _STRICT_SCHEMA_SUPPORTED_MODELS`. |
| `self._client` | `openai.OpenAI`, `base_url="https://api.groq.com/openai/v1"`. |

### 6.2. `_estimate_tokens(self, text: str) -> int` (приватный)

**Описание.** Тонкая обёртка над `llm.common.estimate_tokens` с коэффициентами,
настроенными из `Settings` (см. §5.2 Части 2).

**Параметры:** `text: str`.
**Возвращаемое значение:** `int`.
**Исключения:** не поднимает.

### 6.3. `available_prompt_budget_tokens(self, system_instruction: str, response_model: type[BaseModel]) -> int`

**Описание.** Публичный метод (часть неявного расширенного контракта, которым
пользуется `llm/chunking.py::split_items_into_batches`, см. Часть 1, §2.2 —
"как минимизировать вызовы"). Отвечает на вопрос "сколько токенов остаётся
под сам текст промпта", **до** того, как известна конкретная роль батча в
некоторых путях вызова — поэтому используется консервативный ДЕФОЛТНЫЙ резерв
(`self._reserved_output_default`), не заниженный role-калиброванный.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `system_instruction` | `str` | Статичная системная инструкция, для которой нужно оценить накладные расходы. |
| `response_model` | `type[BaseModel]` | Класс схемы ответа — его JSON Schema тоже занимает токены в промпте (как текстовая подсказка). |

**Возвращаемое значение:** `int` — `max(total_available - overhead - reserved_output_default, 0)`,
где `total_available = self._limiter.available_tokens()`,
`overhead = estimate_tokens(system_instruction) + estimate_tokens(schema_json)`.

**Исключения:** не поднимает.

### 6.4. `generate_structured(self, *, role, prompt, response_model, status, system_instruction=None) -> T`

**Описание.** Главный публичный метод — реализация `llm/base.py::LLMClient`.
Полный порядок операций:

1. `self.budget.check_and_register_task_call(status)` — может поднять `LLMTaskBudgetExceeded`.
2. `self.budget.check_rpd_soft_limit()` — может поднять `LLMFreeLimitReached`.
3. `self.budget.wait_if_needed_for_rpm()` — блокирующий sleep при необходимости.
4. `self._build_response_format_and_system(response_model, system_instruction)` —
   строит `response_format` и полный текст системного промпта (+ fallback-вариант,
   если модель поддерживает strict-режим).
5. Считает `system_tokens`, получает `reserved_output = self._calibrator.reserved_output_tokens(role, default=...)`.
6. Вычисляет `max_prompt_tokens = self._limiter._limit - reserved_output - system_tokens`.
   Если `<= 200` — поднимает `GroqPromptTooLargeError` (система+схема сами по
   себе не влезают).
7. Если `estimate_tokens(prompt) > max_prompt_tokens` — вызывает
   `self._auto_truncate_prompt(...)` (режет с конца).
8. `naive_estimate = system_tokens + prompt_tokens + reserved_output`;
   `calibrated_estimate = self._calibrator.correct(role, naive_estimate)`.
9. Вызывает `self._call_with_retry(...)` с полным набором параметров.
10. `self._parse_with_repair(raw_json, response_model)`.
11. `self.budget.register_call(status, role=role, ok=True)`, и если
    `completion_tokens` получен — `self._calibrator.observe_output(role, completion_tokens)`.
12. Возвращает распарсенный объект.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `role` | `str` (keyword-only) | Тег роли — влияет на калибровку резерва (§3.4), передаётся в лог вызова. |
| `prompt` | `str` (keyword-only) | Динамический текст запроса. |
| `response_model` | `type[T]`, `T: BaseModel` (keyword-only) | Класс ожидаемой структуры ответа. |
| `status` | `TaskStatus` (keyword-only) | Объект статуса сессии (Часть 1, §6). |
| `system_instruction` | `str \| None` (keyword-only, дефолт `None`) | Статичная инструкция роли. |

**Возвращаемое значение:** `T` — экземпляр `response_model`.

**Исключения:**
- `LLMTaskBudgetExceeded` — от `self.budget.check_and_register_task_call`.
- `LLMFreeLimitReached` — от `self.budget.check_rpd_soft_limit`, **или** поднимается
  здесь же при перехвате `GroqRateLimitError` из `_call_with_retry` ("Свободный
  лимит Groq API исчерпан (устойчивая 429 после retry). Задача остановлена.").
- `GroqPromptTooLargeError` — если система+схема не влезают в бюджет.
- `GroqSchemaError` — пробрасывается как есть из `_call_with_retry`/`_parse_with_repair`,
  после `self.budget.register_call(..., ok=False, ...)`.
- Любое другое `Exception` — пробрасывается как есть, но предварительно
  фиксируется через `self.budget.register_call(..., ok=False, error=str(exc))`.

### 6.5. `_build_response_format_and_system(self, response_model, system_instruction, *, force_json_object=False) -> tuple[dict, str]` (приватный)

**Описание.** Строит пару `(response_format, full_system_text)`. Два режима:

- **Strict JSON Schema** (модель в `_STRICT_SCHEMA_SUPPORTED_MODELS` и
  `force_json_object=False`): `response_format = {"type": "json_schema", "json_schema": {"name": ..., "strict": True, "schema": _to_strict_json_schema(...)}}`.
  Схема НЕ дублируется текстом в промпте — Groq применяет её сам
  (constrained decoding).
- **JSON object + текстовая подсказка схемы** (остальные модели, либо
  `force_json_object=True` — fallback-путь после `_is_schema_unsupported_error`):
  `response_format = {"type": "json_object"}`, схема дописывается текстом в
  `full_system`.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `response_model` | `type[BaseModel]` | Класс схемы ответа. |
| `system_instruction` | `str \| None` | Базовая инструкция роли. |
| `force_json_object` | `bool` (keyword-only, дефолт `False`) | Принудительно использовать не-strict режим — используется для fallback-вызова после отказа API от строгой схемы. |

**Возвращаемое значение:** `tuple[dict, str]` — `(response_format, full_system)`.

**Исключения:** не поднимает.

### 6.6. `_auto_truncate_prompt(self, prompt: str, max_prompt_tokens: int, *, role: str) -> str` (приватный)

**Описание.** Safety-net на уровне клиента: автоматически укорачивает `prompt`
под доступный TPM-бюджет, режет **с конца** (предполагая, что инструкции/контекст
важнее хвоста текста — типичный паттерн промптов проекта: "тема + концепции +
текст источника"), стараясь не рвать посреди слова/предложения (ищет ближайший
`\n` или `. ` в последних ~20% допустимой длины), добавляет предупреждающий
маркер в конец.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `prompt` | `str` | Исходный промпт, не влезающий в бюджет. |
| `max_prompt_tokens` | `int` | Доступный бюджет токенов под сам текст. |
| `role` | `str` (keyword-only) | Только для предупреждающего лога. |

**Возвращаемое значение:** `str` — обрезанный текст + маркер
`"\n\n[…текст автоматически обрезан из-за лимита токенов Groq API…]"`,
либо исходный `prompt` без изменений, если он и так укладывается.

**Исключения:** не поднимает. Логирует `logger.warning(...)`.

**Важное замечание из докстринга модуля:** если это происходит часто — стоит
добавить явный чанкинг на уровне вызывающей роли (как в `roles/extractor_critic.py`),
чтобы не терять хвост текста автоматически.

### 6.7. `_parse_with_repair(self, raw_json: str, response_model: type[T]) -> T` (приватный)

**Описание.** Пытается распарсить `raw_json` как есть через
`response_model.model_validate_json(...)`; при неудаче применяет
`llm/common.py::repair_json` и пробует снова.

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `raw_json` | `str` | Сырой текстовый ответ модели (`choices[0].message.content`). |
| `response_model` | `type[T]` | Целевой класс Pydantic. |

**Возвращаемое значение:** `T`.

**Исключения:** `GroqSchemaError` — если ни первая попытка, ни попытка после
`repair_json` не увенчались успехом (в т.ч. если `repair_json` вернул текст,
идентичный исходному — значит, чинить было нечего).

### 6.8. `_call_with_retry(self, *, prompt, system_instruction, estimated_tokens, response_format, fallback_format=None, fallback_system=None, role="", naive_estimate=0) -> tuple[str, int]` (приватный)

**Описание.** Собственно сетевой вызов Groq API, обёрнутый декоратором `tenacity`:

```python
@retry(
    retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),  # GroqRateLimitError, APIConnectionError, APITimeoutError
    wait=wait_random_exponential(multiplier=1, max=15),
    stop=stop_after_attempt(4),
    reraise=True,
)
```

Внутри: `self._limiter.wait_and_reserve(estimated_tokens)` (блокирующий throttle
ДО сети), затем `self._client.chat.completions.create(...)`. После успешного
ответа: извлекает `usage` (если есть), считает `effective_tokens` (с учётом или
без учёта `cached_tokens`, в зависимости от `self._account_for_prompt_cache`),
вызывает `self._limiter.adjust_last_reservation(effective_tokens)`,
`self._calibrator.observe(role, naive_estimate, effective_tokens)`,
`self._cache_observability.observe(role, cache_hit)`.

При исключении:
- Если это отказ schema (`_is_schema_unsupported_error`) и есть `fallback_format` —
  **рекурсивный** вызов `_call_with_retry` с `fallback_format`/`fallback_system`
  (разовый откат на `json_object`).
- Если `is_rate_limit_error(exc)` — при наличии `retry_after` вызывает
  `self._limiter.force_wait(retry_after)`, затем `self._limiter.register_rate_limit_hit()`,
  поднимает `GroqRateLimitError(str(exc), retry_after=retry_after)`.
- Если `is_request_too_large_error(exc)` — поднимает `GroqPromptTooLargeError`.
- Иначе — исходное исключение пробрасывается как есть (после `logger.exception(...)`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `prompt` | `str` (keyword-only) | Текст пользовательского сообщения. |
| `system_instruction` | `str` (keyword-only) | Полный текст системного промпта (уже с JSON Schema подсказкой, если применимо). |
| `estimated_tokens` | `int` (keyword-only) | Калиброванная оценка для резервирования в `TokenRateLimiter`. |
| `response_format` | `dict` (keyword-only) | `{"type": "json_schema", ...}` или `{"type": "json_object"}`. |
| `fallback_format` | `dict \| None` (keyword-only) | Резервный `response_format` на случай отказа API от строгой схемы. |
| `fallback_system` | `str \| None` (keyword-only) | Резервный системный промпт (с текстовой JSON Schema) для fallback-пути. |
| `role` | `str` (keyword-only, дефолт `""`) | Тег роли — для калибратора и наблюдаемости кэша. |
| `naive_estimate` | `int` (keyword-only, дефолт `0`) | Посимвольная оценка — для обучения калибратора. |

**Возвращаемое значение:** `tuple[str, int]` — `(raw_json_content, completion_tokens)`.
`completion_tokens = 0`, если Groq не вернул `usage` (не должно происходить
в норме, но не считается ошибкой).

**Исключения:**
- `GroqRateLimitError` — 429, после исчерпания retry `tenacity` (`reraise=True`).
- `GroqPromptTooLargeError` — реальный 413 от API.
- `RuntimeError` — если `content is None` (пустой ответ модели).
- Прочие сетевые исключения (`APIConnectionError`, `APITimeoutError`) —
  ретраятся `tenacity`, при исчерпании попыток пробрасываются как есть.

---

## 7. Сводная таблица параметров конфигурации, влияющих на `GroqClient`

| Поле `Settings` | Куда попадает | Назначение |
|---|---|---|
| `groq_api_key` | `GroqClient.__init__` (проверка) | Обязателен, иначе `RuntimeError`. |
| `groq_model` | `openai.chat.completions.create(model=...)` | Конкретная модель Groq. |
| `groq_timeout_seconds` | `_call_with_retry` (`timeout=...`) | HTTP-таймаут одного запроса. |
| `groq_tpm_limit` | `TokenRateLimiter.__init__(tpm_limit=...)` | Реальный TPM-лимит модели. |
| `groq_limiter_safety_margin` | `TokenRateLimiter.__init__(safety_margin=...)` | Запас от реального лимита. |
| `groq_margin_penalty_factor` / `groq_margin_min_penalty` / `groq_margin_recovery_seconds` / `groq_margin_recovery_mode` | `TokenRateLimiter.__init__` | Параметры adaptive safety margin (§5.2–5.3). |
| `groq_calibration_ema_alpha` / `min_ratio` / `max_ratio` | `TokenEstimateCalibrator.__init__` | Калибровка prompt-ratio (§3.1–3.3). |
| `groq_output_calibration_ema_alpha` / `groq_reserved_output_min_tokens` | `TokenEstimateCalibrator.__init__` | Калибровка output-резерва (§3.4–3.5). |
| `groq_reserved_output_tokens_default` | `GroqClient.__init__` → `self._reserved_output_default` | Резерв "холодного старта" роли. |
| `groq_account_for_prompt_cache` | `GroqClient.__init__` → `self._account_for_prompt_cache` | Учитывать ли кэш-хиты в бюджете (по умолчанию — нет). |
| `groq_chars_per_token_cyrillic` / `_latin` / `groq_cyrillic_ratio_threshold` | `GroqClient._chars_per_token_kwargs` | Коэффициенты посимвольной оценки токенов. |
| `groq_share_limiter_when_same_model` | `llm/factory.py::create_extraction_llm_client` | Шарить ли лимитер/калибратор между основным и extraction-клиентом. |
| `groq_extraction_model` / `groq_extraction_tpm_limit` / `groq_extraction_rpd_soft_limit` | `llm/factory.py::create_extraction_llm_client` | Отдельная конфигурация extraction-клиента. |
| `max_llm_calls_per_task` | `LLMBudget.__init__` (не `GroqClient` напрямую) | Общий потолок вызовов на задачу. |
| `groq_rpm_soft_limit` / `groq_rpd_soft_limit` | `LLMBudget.__init__` | Soft-лимиты (см. Часть 1, §3.3). |

---

## Итоговая карта всего цикла (все три части вместе)

```
cli/main.py::ask/resume
  → Orchestrator.__init__            (Часть 1, §2.1)  — создаёт self.llm/self.extraction_client через llm/factory.py
  → Orchestrator.run(...)            (Часть 1, §2.1)  — держит TaskStatus, вызывает роли по порядку
      → roles/outline_planner.py::build_plan            → self.llm.generate_structured(role="outline_planner", ...)
      → roles/elaborator.py::elaborate_outline           → self.extraction_client.generate_structured(role="elaborator", ...)
      → roles/vault_analyst.py::resolve_notes_against_vault → self.llm.generate_structured(role="vault_dedup"/"folder_assignment", ...)
      → roles/critic.py::run_critic_cycle
          → roles/synthesizer_writer.py::write_note       → self.llm.generate_structured(role="synthesizer_write", ...)
          → roles/critic.py::review_draft                 → self.llm.generate_structured(role="critic", ...)
  → GroqClient.generate_structured(...)   (Часть 3, §6.4) — бюджет → формат ответа → калибровка → retry-вызов → парсинг
      → LLMBudget (Часть 1, §3)            — учёт лимитов
      → TokenRateLimiter (Часть 3, §5)     — TPM-throttle
      → TokenEstimateCalibrator (Часть 3, §3) — точность оценки токенов
      → llm/common.py (Часть 2)            — исключения + текстовые утилиты + repair_json
      → openai.OpenAI → Groq API           — реальный HTTP-запрос
```

Документация цикла `запуск → Orchestrator → роль → GroqClient` завершена.
