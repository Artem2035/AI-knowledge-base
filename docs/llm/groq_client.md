# Документация: `llm/groq_client.py`

> Reference-док. `GroqClient` — единственный реализованный в MVP провайдер `LLMClient` (`core.md §1`). `llm/router.py` и `llm/openrouter_client.py` существуют как второй провайдер, но намеренно не документируются (`../CONTRIBUTING.md`). Путь одного вызова целиком — `../flows/llm_cycle.md`. Настройки — `../config/settings.md`.

**Что внутри файла:** исключения (§1), вспомогательные функции и константы (§2), `TokenEstimateCalibrator` (§3), `CacheObservability` (§4), `TokenRateLimiter` (§5), `GroqClient` (§6).

---

## 1. Исключения

| Класс | Родитель | Назначение |
|---|---|---|
| `GroqRateLimitError` | `LLMRateLimitError` | Реальная 429; ловится `tenacity` для повторов. Несёт `retry_after`. |
| `GroqSchemaError` | `LLMSchemaError` | JSON невалиден даже после `repair_json`. Роли реагируют уменьшением батча. |
| `GroqPromptTooLargeError` | `LLMPromptTooLargeError` | Проактивно (система+схема не влезают) или реактивно (413). |

Роли ловят только родителей из `llm/common.py` (`core.md §2.1`).

---

## 2. Вспомогательные функции и константы модуля

### 2.1. Множества моделей

| Имя | Значение | Для чего |
|---|---|---|
| `_STRICT_SCHEMA_SUPPORTED_MODELS` | `{"openai/gpt-oss-20b", "openai/gpt-oss-120b"}` | Строгий `json_schema` (constrained decoding). |
| `_REASONING_EFFORT_SUPPORTED_MODELS` | то же сейчас | Параметры `reasoning_effort`/`include_reasoning`. Отдельная возможность: множества могут разойтись. |
| `_TIKTOKEN_SUPPORTED_MODELS` | то же сейчас | Токенайзер `o200k` корректен только для gpt-oss. |

### 2.2. `_to_strict_json_schema(schema: dict) -> dict`
Рекурсивно приводит JSON Schema из Pydantic к виду strict-режима Groq: у каждого object-узла `additionalProperties=False` и `required` = **все** ключи `properties`. Обходит `$defs`/`definitions`, `properties`, `items`, `anyOf`/`oneOf`/`allOf`. Вход не мутирует. Не поднимает.

### 2.3. `_is_schema_unsupported_error(exc) -> bool`
`True`, если текст ошибки содержит один из маркеров: `unsupported_feature`, `invalid json schema`, `response_format`, `json_validate_failed`, `does not validate`, `missing properties`. На такую ошибку делается **один** откат на `json_object` в том же вызове.

### 2.4. Подсчёт токенов: `tiktoken`
- `_try_load_tiktoken_encoding()` — пробует `o200k_harmony`, затем `o200k_base`. Любой сбой (нет пакета, нет сети для первой загрузки словаря) не фатален: возвращает `None`, клиент считает по эвристике `llm/common.py`.
- `_REQUEST_OVERHEAD_TOKENS = 120` — служебные токены запроса (роли сообщений, разметка), добавляются к оценке system.

### 2.5. Параметры рассуждений
- `_REASONING_EFFORT_BY_ROLE`: `folder_assignment`, `vault_dedup`, `elaborator`, `annotator` → `"low"`. Роли вне словаря (`outline_planner`) идут с дефолтом модели: один вызов на задачу, качество плана важнее экономии.
- `_is_reasoning_param_error(exc) -> bool` — API отклонил `reasoning_effort`/`include_reasoning`/`reasoning_format`. Оптимизация не должна ронять вызов: делается разовый повтор без `extra_body`.

---

## 3. `class TokenEstimateCalibrator`

Адаптивная калибровка оценки токенов **по роли**. Две независимые калибровки с разными единицами (поэтому два словаря и два набора методов):
- **prompt-ratio** (`_ratio_by_role`) — безразмерный множитель «во сколько раз наивная оценка **входа** ошиблась»; применяется как коэффициент к новой оценке.
- **output-EMA** (`_output_ema_by_role`) — абсолютное число токенов вывода (`completion_tokens`), скользящее среднее по роли; заменяет единый статичный резерв.

### 3.1. `__init__(self, ema_alpha=0.3, min_ratio=0.05, max_ratio=1.5, *, output_ema_alpha=0.3, output_min_floor=300)`
Значения читаются `GroqClient` из `groq_calibration_*`, `groq_output_calibration_ema_alpha`, `groq_reserved_output_min_tokens`. Потокобезопасен (`threading.Lock`).

### 3.2. `correct(self, role, naive_estimate) -> int`
`max(int(naive_estimate * ratio), 1)`; без наблюдений по роли — `naive_estimate` как есть.

### 3.3. `observe(self, role, naive_estimate, actual_effective_tokens) -> None`
Обновляет EMA prompt-ratio. `naive_estimate <= 0` — выход. Отношение зажимается в `[min_ratio, max_ratio]`. **Важно:** вызывается с оценкой и фактом **только входа** (`GroqClient` передаёт `naive_input` и реальный `prompt_tokens`, минус кэш при включённом учёте). Резерв вывода в калибровку входа не входит: иначе коэффициент, выученный при одном резерве, применялся бы к оценке с другим.

### 3.4. `reserved_output_tokens(self, role, default) -> int`
Без наблюдений — `default`. После первого — `max(int(EMA), output_min_floor)`. Пол защищает от серии аномально коротких ответов.

### 3.5. `observe_output(self, role, actual_completion_tokens) -> None`
Обновляет output-EMA (первое наблюдение становится значением). `<= 0` — выход.

---

## 4. `class CacheObservability`

Бинарный факт (был кэш-хит Groq prompt cache или нет) по роли — **только для диагностики и логов**, в бюджет не входит.
- `observe(role, cache_hit)`; `hit_rate(role) -> float | None`; `summary() -> dict[str, tuple[int, int]]` (`(hits, calls)` по ролям). Потокобезопасен.

---

## 5. `class TokenRateLimiter`

Клиентский лимитер TPM. Реальный лимит free tier `openai/gpt-oss-120b` — 8000 TPM, самое узкое место среди RPM/RPD/TPM/TPD.

Окно **фиксированное, по календарной минуте** (как считает Groq Console): `[hh:mm:00, hh:mm+1:00)`, на границе минуты расход сбрасывается и не переносится. Окно определяется настенными часами (`time.time()`); восстановление margin — `time.monotonic()`.

Два механизма: (1) фиксированное минутное окно с ожиданием следующей минуты (+ `boundary_margin_seconds`); (2) adaptive safety margin после реального 429.

### 5.1. `__init__(self, tpm_limit, safety_margin=0.85, margin_penalty_factor=0.8, margin_min_penalty=0.5, margin_recovery_seconds=300.0, recovery_mode="linear", boundary_margin_seconds=0.5)`

| Имя | Назначение |
|---|---|
| `tpm_limit` | Реальный TPM модели (`groq_tpm_limit`). |
| `safety_margin` | Доля лимита. Дефолт аргумента 0.85, **в проекте через Settings передаётся 0.95** (`groq_limiter_safety_margin`). |
| `margin_penalty_factor`, `margin_min_penalty`, `margin_recovery_seconds`, `recovery_mode` | Параметры adaptive margin; неизвестный `recovery_mode` заменяется на `"linear"`. |
| `boundary_margin_seconds` | Запас после границы минуты. Не вынесен в Settings, `GroqClient` его не передаёт: действует `0.5`. |

Состояние: `_base_limit = max(int(tpm_limit * safety_margin), 1)`, `_limit`, `_lock` (`RLock`), `_window_idx`, `_used`, `_last_reservation: tuple[int, int] | None`, `_margin_penalty`, `_penalty_value_at_last_hit`, `_last_penalty_at`.

### 5.2. `register_rate_limit_hit(self) -> None`
После **реального** 429: `_margin_penalty = max(penalty * factor, min_penalty)` (применяется к текущему, возможно частично восстановленному значению, поэтому серия 429 накапливается), запоминает точку отсчёта восстановления, пересчитывает `_limit`, пишет `warning`.

### 5.3. `_maybe_recover_margin(self, now) -> None` (приватный)
- `"step"`: штраф держится ровно `recovery_seconds`, затем мгновенный сброс.
- `"linear"` (дефолт): `penalty = start + (1 - start) * progress`, `progress = min(elapsed / recovery_seconds, 1)`; `_limit` пересчитывается при каждом вызове; при `progress >= 1` — полное восстановление.

### 5.4. `_roll_window(self, wall_now) -> None` (приватный)
Новая календарная минута → `_window_idx` обновляется, `_used = 0`. Только под `_lock`.

### 5.5. `available_tokens(self) -> int`
`max(_limit - _used, 0)` — остаток **текущей** минуты. К концу минуты близок к нулю, поэтому **не использовать для планирования батчей**.

### 5.6. `capacity_tokens(self) -> int`
Полная ёмкость минуты с учётом штрафа (`_limit`), без вычета потраченного. Нужна для батчинга: `wait_and_reserve` дождётся следующей минуты, и батч должен влезать в пустую минуту. После 429 ёмкость уменьшается, батчи сужаются автоматически. Вызывает `GroqClient.available_prompt_budget_tokens`.

### 5.7. `wait_and_reserve(self, estimated_tokens) -> None`
Цикл: под `_lock` — восстановление margin, смена окна, проверка `fits = _used + estimated <= _limit`. Запрос больше всего лимита при пустом окне пропускается с `warning` (иначе вечное ожидание; реальный отказ придёт от API). При успехе `_used += estimated`, `_last_reservation = (idx, estimated)`. Иначе сон **вне блокировки** до следующей минуты (`min(sleep, 5.0)` кусками: `_limit` может вырасти от восстановления). Общий лимитер основного и extraction-клиента при этом не блокируется на время ожидания.

### 5.8. `adjust_last_reservation(self, actual_tokens) -> None`
Заменяет оценочный резерв последней резервации фактическим расходом: `_used = max(_used - reserved + actual, 0)`. Если минута резервации уже закончилась или резервации нет — ничего не делает.

### 5.9. `force_wait(self, seconds) -> None` — ⚠ требуется добавить
`_call_with_retry` вызывает этот метод при 429 с `retry_after`, но в присланной версии файла он **отсутствует**: вызов даёт `AttributeError`. Предполагаемая реализация (намеренно сон под блокировкой: после реального 429 никто не должен резервировать окно):

```python
    def force_wait(self, seconds: float) -> None:
        with self._lock:
            time.sleep(max(seconds, 0.1))
```

После добавления убрать эту пометку.

### 5.10. Известные ограничения
- **Граница минуты.** Запрос в конце минуты Groq может засчитать в следующую (сетевая задержка). `boundary_margin_seconds` и `safety_margin` риск снижают, но не устраняют. При повторных 429 на границах — увеличить запас до 1–2 с (потребует вынести параметр в Settings).
- **Предположение о часах.** Выравнивание по UTC-минутам; если Groq считает окно иначе, оно сместится.
- **Резервация без корректировки.** Если после `wait_and_reserve` запрос упал, оценочный резерв остаётся в `_used` до конца минуты. При откате `json_schema` → `json_object` резервация делается повторно, первая остаётся.
- **Параллельные клиенты.** `_last_reservation` одна на лимитер: при общем лимитере `adjust_last_reservation` может поправить чужую резервацию.
- **Приватный доступ.** `GroqClient.generate_structured` читает `self._limiter._limit` напрямую.

---

## 6. `class GroqClient`

Реализация `LLMClient` для Groq. Единственная точка входа к Groq API в проекте.

| Константа | Значение | Назначение |
|---|---|---|
| `DEFAULT_TPM_LIMIT` | `8000` | Fallback, если `settings.groq_tpm_limit` не задан. |
| `RESERVED_OUTPUT_TOKENS` | `1500` | Class-level fallback для обратной совместимости; реально используется `groq_reserved_output_tokens_default`. |

### 6.1. `__init__(self, settings, budget, *, shared_limiter=None, shared_calibrator=None, shared_cache_observability=None)`

Проверяет `FREE_ONLY` и ключ, читает коэффициенты оценки токенов, загружает `tiktoken` (если `getattr(settings, "groq_use_tiktoken", True)` и модель в `_TIKTOKEN_SUPPORTED_MODELS`), создаёт или переиспользует лимитер и калибратор, определяет поддержку strict-схемы и рассуждений, создаёт `openai.OpenAI` поверх `httpx.Client` (keep-alive пул, HTTP/1.1) с `base_url="https://api.groq.com/openai/v1"`.

При переданных `shared_*` параметры лимитера (`tpm_limit`, `safety_margin` и др.) игнорируются: объект уже сконструирован клиентом, создавшим его первым (`core.md §3.3`).

**Исключения:** `RuntimeError` — `free_only=False` или пустой `groq_api_key`.

**Атрибуты:** `settings`, `budget`, `_chars_per_token_kwargs`, `_encoding`, `_limiter`, `_calibrator`, `_reserved_output_default`, `_account_for_prompt_cache`, `_cache_observability`, `_strict_schema_supported`, `_reasoning_supported`, `_client`.

### 6.2. `_estimate_tokens(self, text) -> int`
Для gpt-oss — `len(encoding.encode_ordinary(text))` (спецтокены в тексте не ломают подсчёт); иначе эвристика `llm.common.estimate_tokens` с коэффициентами из Settings. Пустой текст → `0`.

### 6.3. `available_prompt_budget_tokens(self, system_instruction, response_model) -> int`
Контракт для `llm/chunking.py` (`chunking.md §1`): сколько токенов остаётся под текст промпта. Бюджет от **полной ёмкости** окна (`capacity_tokens()`), не от остатка минуты. Накладные: оценка system + оценка strict-схемы в компактном JSON + `_REQUEST_OVERHEAD_TOKENS`, плюс консервативный `_reserved_output_default` (роль неизвестна). **Возвращает** `max(capacity - overhead - reserved, 0)`. Не поднимает.

### 6.4. `generate_structured(self, *, role, prompt, response_model, status, system_instruction=None) -> T`

Порядок:
1. `budget.check_and_register_task_call(status)`, `check_rpd_soft_limit()`, `wait_if_needed_for_rpm()`.
2. `_build_response_format_and_system(...)` (§6.5); для strict-моделей строится и fallback-вариант `json_object`.
3. `system_tokens = оценка(full_system) + 120`; в strict-режиме схема в `full_system` не входит, но занимает место в промпте Groq, поэтому её токены добавляются отдельно.
4. Резерв вывода: `calibrator.reserved_output_tokens(role, default=settings.groq_reserved_output_by_role.get(role, self._reserved_output_default))`. Приоритет: EMA роли → словарь по ролям → общий дефолт; не ниже `groq_reserved_output_min_tokens`.
5. `max_prompt_tokens = limiter._limit - reserved_output - system_tokens`. `<= 200` → `GroqPromptTooLargeError`. Промпт больше → `_auto_truncate_prompt` (§6.6).
6. **Калибруется только вход:** `naive_input = system_tokens + prompt_tokens`; `estimated_tokens = calibrator.correct(role, naive_input) + reserved_output`.
7. `extra_body = _build_reasoning_extra_body(role)` (§6.9).
8. `_call_with_retry(...)` (§6.8) → `(raw_json, completion_tokens)`.
9. `_parse_with_repair` (§6.7); `budget.register_call(ok=True)`; при `completion_tokens` — `calibrator.observe_output(role, completion_tokens)`.

| Параметр | Назначение |
|---|---|
| `role` | Тег роли: калибровка, резерв, effort, лог. |
| `prompt` | Динамический текст. |
| `response_model` | Класс ответа (`schemas.md`). |
| `status` | `TaskStatus` сессии. |
| `system_instruction` | Статичная инструкция роли (`prompts.md`). |

**Исключения:** `LLMTaskBudgetExceeded`; `LLMFreeLimitReached` (от `check_rpd_soft_limit` или при `GroqRateLimitError` после исчерпания retry); `GroqPromptTooLargeError`; `GroqSchemaError` (после `register_call(ok=False)`); прочие — как есть, после `register_call(ok=False)`.

### 6.5. `_build_response_format_and_system(self, response_model, system_instruction, *, force_json_object=False) -> tuple[dict, str]` (приватный)
- **Strict** (модель в `_STRICT_SCHEMA_SUPPORTED_MODELS`, `force_json_object=False`): `response_format={"type": "json_schema", "json_schema": {"name", "strict": True, "schema": _to_strict_json_schema(...)}}`. Схема текстом не дублируется. В system дописан английский суффикс: формат обеспечивает API, не добавлять markdown-обёртки и лишние поля, имена полей и enum не переводить, **всегда** включать все поля (`""`/`[]` вместо пропуска).
- **`json_object` + текстовая схема** (прочие модели или fallback): схема дописывается в system, числа только цифрами, без текста вокруг JSON.

### 6.6. `_auto_truncate_prompt(self, prompt, max_prompt_tokens, *, role) -> str` (приватный)
Safety-net: режет с конца по границе строки или предложения (последние ~30% допустимой длины), коэффициент символов на токен берётся из реального промпта (`len/оценка`) с запасом 0.92, добавляет маркер обрезки. `logger.warning`. Если это происходит часто, нужен батчинг на стороне роли (`chunking.md`).

### 6.7. `_parse_with_repair(self, raw_json, response_model) -> T` (приватный)
`model_validate_json`; при неудаче `repair_json` (`core.md §2.3`) и вторая попытка. **Исключение:** `GroqSchemaError`, если repair не применим или не помог.

### 6.8. `_call_with_retry(self, *, prompt, system_instruction, estimated_tokens, response_format, fallback_format=None, fallback_system=None, role="", naive_estimate=0, extra_body=None) -> tuple[str, int]` (приватный)

Декоратор `tenacity`: повтор при `GroqRateLimitError`, `APIConnectionError`, `APITimeoutError`; `wait_random_exponential(multiplier=1, max=15)`; `stop_after_attempt(4)`; `reraise=True`.

Внутри: `limiter.wait_and_reserve(estimated_tokens)` → `chat.completions.create(model, messages, response_format, timeout[, extra_body])`. После ответа (если есть `usage.total_tokens`):
- считает `cached` (`prompt_tokens_details.cached_tokens`) и `effective_tokens` (минус кэш только при `groq_account_for_prompt_cache`);
- `limiter.adjust_last_reservation(effective_tokens)`;
- `calibrator.observe(role, naive_estimate, actual_input)`, где `actual_input = prompt_tokens - cached` (если учёт кэша включён);
- `cache_observability.observe(role, cache_hit)`;
- подробный лог `Groq usage [роль]`: input (кэш), output (reasoning), total, резерв (naive, откалиброванный, факт/резерв), `finish_reason`, тайминги Groq; при `finish_reason == "length"` предупреждение (ответ обрезан, ожидайте `GroqSchemaError`).

Обработка исключений (по порядку):
1. отказ параметров рассуждений при заданном `extra_body` → разовый повтор без `extra_body`;
2. отказ strict-схемы (`_is_schema_unsupported_error`) при наличии `fallback_format` → разовый повтор на `json_object`. ⚠ Рекурсивный вызов не передаёт `extra_body`: параметры рассуждений в нём теряются (влияет на расход токенов, не на корректность);
3. `is_rate_limit_error` → `retry_after = parse_retry_after(...)`; при наличии `limiter.force_wait(retry_after)` (⚠ §5.9); затем `limiter.register_rate_limit_hit()` и `GroqRateLimitError`;
4. `is_request_too_large_error` → `GroqPromptTooLargeError`;
5. иначе исходное исключение (после `logger.exception`).

**Возвращает:** `(raw_json_content, completion_tokens)`; `completion_tokens = 0`, если `usage` нет. `content is None` → `RuntimeError`.

### 6.9. `_build_reasoning_extra_body(self, role) -> dict | None` (приватный)
`None`, если модель не поддерживает рассуждения. Иначе `{"include_reasoning": False}` и, если для роли задан effort, `"reasoning_effort"`. Передаётся через `extra_body`, а не именованными аргументами: `include_reasoning` нет в типизации SDK, а `reasoning_effort` есть только в поздних версиях (`requirements.txt` допускает `openai>=1.30`).

---

## Сводная таблица параметров конфигурации

| Поле `Settings` | Куда попадает | Назначение |
|---|---|---|
| `groq_api_key` | `__init__` | Обязателен. |
| `groq_model` | `chat.completions.create` | Модель Groq. |
| `groq_timeout_seconds` | `_call_with_retry` | HTTP-таймаут. |
| `groq_tpm_limit` | `TokenRateLimiter` | Реальный TPM. |
| `groq_limiter_safety_margin` | `TokenRateLimiter` | Запас (0.95). |
| `groq_margin_penalty_factor`, `groq_margin_min_penalty`, `groq_margin_recovery_seconds`, `groq_margin_recovery_mode` | `TokenRateLimiter` | Adaptive margin (§5.2–5.3). |
| `groq_calibration_ema_alpha`, `_min_ratio`, `_max_ratio` | `TokenEstimateCalibrator` | Калибровка входа. |
| `groq_output_calibration_ema_alpha`, `groq_reserved_output_min_tokens` | `TokenEstimateCalibrator` | Калибровка вывода. |
| `groq_reserved_output_tokens_default` | `self._reserved_output_default` | Резерв холодного старта. |
| `groq_reserved_output_by_role` | `generate_structured` (через `getattr`) | Стартовый резерв по роли. |
| `groq_account_for_prompt_cache` | `self._account_for_prompt_cache` | Учитывать ли кэш в бюджете (по умолчанию нет). |
| `groq_chars_per_token_cyrillic`, `_latin`, `groq_cyrillic_ratio_threshold` | `_chars_per_token_kwargs` | Эвристика, когда нет `tiktoken`. |
| `groq_use_tiktoken` (**не объявлено в `Settings`**) | `__init__` через `getattr`, дефолт `True` | Подсчёт токенов `tiktoken`. |
| `groq_share_limiter_when_same_model`, `groq_extraction_*` | `llm/factory.py` (`core.md §3.3`) | Общий лимитер и отдельный клиент extraction. |
| `max_llm_calls_per_task`, `groq_rpm_soft_limit`, `groq_rpd_soft_limit` | `LLMBudget` | Потолки вызовов (`../orchestrator/budget.md`). |

Документация по `llm/groq_client.py` завершена. Путь одного вызова целиком — `../flows/llm_cycle.md`.