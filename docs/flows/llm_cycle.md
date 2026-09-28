# Цикл одного запроса: от CLI до ответа Groq и обратно

> Это **не reference** — здесь нет таблиц параметров и сигнатур. Это рассказ
> о том, что происходит, в каком порядке, и что через что течёт, когда
> пользователь запускает `python -m cli.main ask "..."`. За точным контрактом
> каждой функции — переходите по ссылкам в `../orchestrator/state_machine.md`
> и `../llm/core.md`/`../llm/groq_client.md`.

---

## 1. Общая схема

```
cli/main.py (ask/resume)
    │  raw_query: str | None, resume_task_id: str | None
    ▼
Orchestrator.__init__(settings)     ── создаёт self.llm, self.extraction_client, self.budget
    │
Orchestrator.run(...)               ── держит TaskStatus, вызывает роли по порядку
    │
roles/*.py                          ── outline_planner, elaborator, vault_analyst, critic→synthesizer_writer
    │  client.generate_structured(role=<тег>, prompt=<str>, response_model=<BaseModel>,
    │                              status=status, system_instruction=<str|None>)
    ▼
LLMClient Protocol                  ── общий контракт, см. ../llm/core.md §1
    ▼
GroqClient.generate_structured(...) ── см. ../llm/groq_client.md §6.4
    │  бюджет → формат ответа → калибровка → retry-вызов → парсинг
    ▼
Pydantic-объект response_model  →  вверх по цепочке до роли  →  Orchestrator накапливает результат
```

Ключевой принцип: между слоями передаются либо примитивы (`str`, `bool`),
либо Pydantic-модели (`TaskStatus`, `response_model`, `Plan`, `DraftNote`).
Сырые объекты HTTP-ответов никогда не всплывают выше `GroqClient`.

---

## 2. CLI-обвязка

`cli/main.py::_run_and_report` — общая точка для команд `ask` и `resume`:
создаёт `Orchestrator`, вызывает `orch.run(...)` с тремя callback'ами и
печатает результат. Сами callback'и:

- `progress_cb(stage: str)` — печатает строку прогресса.
- `plan_confirm_cb(plan) -> bool` — приостанавливает spinner, показывает
  план (`cli/plan_editor.py::confirm_plan`), возвращает решение пользователя.
- `merge_confirm_cb(drafts) -> list[DraftNote]` — то же для объединения
  заметок (`cli/draft_merge_editor.py::confirm_merges`).

Полное описание команд CLI — `../cli/_index.md`. Здесь важно только то, что
именно эти три callback'а `Orchestrator.run()` дёргает в нужный момент (см.
§4 ниже).

---

## 3. Что живёт в памяти всю сессию

Два объекта путешествуют через весь цикл ПО ССЫЛКЕ, не копируются:

- **`TaskStatus`** (`../storage/models.md §7.2`) — общий счётчик
  `llm_calls_used`. Оба клиента (`self.llm` и `self.extraction_client`)
  получают ОДИН и тот же объект от `Orchestrator.run()`, поэтому
  `MAX_LLM_CALLS_PER_TASK` (`../orchestrator/budget.md §1.3`) остаётся
  единым потолком независимо от того, какой из двух клиентов тратит бюджет.
- **`TaskCheckpoint`** (`../staging/checkpoint.md §2`) — персистентное состояние
  задачи на диске. `Orchestrator.run()` вызывает `persist(stage_label)`
  сразу после каждого успешно завершённого шага (и даже чаще — после
  каждого батча elaboration, после каждой написанной заметки). Это то, что
  делает `resume` возможным без повторной траты бюджета.

Важно: `status.llm_calls_used` обнуляется в начале КАЖДОГО вызова `run()`,
даже при resume — лимит на сессию/попытку, не на задачу за всё время жизни.
Накопительный расход по всем попыткам — отдельно, в
`checkpoint.total_llm_calls_used`, только для отчёта пользователю.

---

## 4. Шаги `Orchestrator.run()` и какие вызовы LLM за ними стоят

| # | Шаг | Роль / функция | Клиент | `role=` (тег) | LLM? |
|---|---|---|---|---|---|
| 0 | Индексация Vault | `sync_vault_index()` | — | — | Нет |
| 1 | Planning | `outline_planner.build_plan` | `self.llm` | `"outline_planner"` | Да, 1 вызов |
| 1.5 | Утверждение плана | `plan_confirm_cb(plan)` | — | — | Нет (callback пользователя) |
| 2 | Elaborating | `elaborator.elaborate_outline` | `self.extraction_client` | `"elaborator"` | Да, батчами |
| 3 | Vault analysis | `vault_analyst.resolve_notes_against_vault` | `self.llm` | `"vault_dedup"`, `"folder_assignment"` | Только для «серой зоны» схожести |
| 4 | Synthesis (на каждую заметку) | `critic.run_critic_cycle` → `synthesizer_writer.write_note` | `self.llm` | `"synthesizer_write"`, `"critic"` | Да, 1+ вызовов на заметку |
| 4.5 | Объединение черновиков | `merge_confirm_cb(drafts)` | — | — | Нет, чистый код (`staging/draft_merge.py`) |
| 5 | Валидация | `validation.run_validation` | — | — | Нет, чистый код |
| 6 | Staging | `staging/changeset.py::save_changeset` | — | — | Нет |

Полные сигнатуры каждой роли — `../roles/_index.md`.
Полная сигнатура самого `run()` и его внутренних `persist`/`report` —
`../orchestrator/state_machine.md §2.4`.

**Почему planning и elaboration используют РАЗНЫХ клиентов:** `elaborator`
— самый частый по числу вызовов шаг (батчами по подтемам плана), поэтому у
него отдельный `LLMBudget`/`GroqClient` с потенциально другой моделью
(`groq_extraction_model`) и, при совпадении моделей, разделяемым
`TokenRateLimiter` с основным клиентом — см. `../llm/core.md §3.3`.

**RESEARCH_MODE=web** (веб-поиск → Extractor+Critic извлекает факты из
реального текста) в этой версии `Orchestrator.run()` явно блокируется —
миграция на новую структуру плана (`OutlineNote`/`subpoints`) не завершена.
Шаги 1–2 в таблице выше относятся к дефолтному `RESEARCH_MODE=knowledge`.

---

## 5. Путь одного вызова `generate_structured` вглубь

Когда роль вызывает `client.generate_structured(role=..., prompt=...,
response_model=..., status=..., system_instruction=...)`:

1. `LLMBudget.check_and_register_task_call` / `check_rpd_soft_limit` /
   `wait_if_needed_for_rpm` — три проверки бюджета ДО сети
   (`../orchestrator/budget.md §1.3`).
2. `GroqClient._build_response_format_and_system` — решает, использовать ли
   строгий `json_schema` (constrained decoding) или `json_object` + текстовая
   подсказка схемы (`../llm/groq_client.md §6.5`).
3. `TokenEstimateCalibrator.reserved_output_tokens(role, ...)` — калиброванный
   по роли резерв под ответ (`../llm/groq_client.md §3.4`).
4. Если промпт не влезает в TPM-бюджет — `_auto_truncate_prompt` режет его
   с конца (`../llm/groq_client.md §6.6`).
5. `TokenRateLimiter.wait_and_reserve` — блокирующий throttle по TPM
   непосредственно перед сетевым вызовом (`../llm/groq_client.md §5.6`).
6. Реальный HTTP-запрос к Groq API (`_call_with_retry`, с retry через
   `tenacity` на rate limit/сетевые сбои — `../llm/groq_client.md §6.8`).
7. `_parse_with_repair` — парсинг JSON-ответа, с fallback-эвристиками
   починки при невалидном JSON (`../llm/groq_client.md §6.7`).
8. `LLMBudget.register_call` — фиксация факта вызова (успех/сбой) в
   `TaskStatus.llm_calls_log`.
9. `TokenEstimateCalibrator.observe`/`observe_output` — обучение
   калибратора на реальном расходе токенов этого вызова.

Каждый из этих шагов подробно расписан по параметрам/исключениям в
`../llm/groq_client.md` — здесь важен только порядок и то, что вызывает что.

---

## 6. Что происходит при исчерпании лимита

Если Groq возвращает устойчивую 429 после исчерпания retry, либо достигнут
`MAX_LLM_CALLS_PER_TASK`/дневной soft-лимит — исключение (`LLMFreeLimitReached`
или `LLMTaskBudgetExceeded`, `../orchestrator/budget.md §1.1–1.2`)
поднимается внутри `GroqClient.generate_structured` и всплывает вплоть до
`Orchestrator.run()`, где перехватывается:

```
except (LLMFreeLimitReached, LLMTaskBudgetExceeded) as exc:
    status.stage = "stopped"
    persist(checkpoint.last_completed_stage)   # прогресс уже сохранён на предыдущих шагах
    return RunResult(stopped=True, message=f"{exc}\n\nПродолжить: resume {task.task_id}")
```

**Никакого автоматического перехода на платный tier** — это единственная
реакция на исчерпание лимита во всей системе (см. `config/settings.py::
validate_free_only`, `../config/settings.md §12.2`). Пользователь видит
сообщение и команду `resume <task_id>`, которая при следующем запуске
пропустит уже завершённые шаги благодаря `TaskCheckpoint` (§3 выше).

---

## Куда идти дальше

- Точный контракт `Orchestrator`/`LLMBudget` → `../orchestrator/state_machine.md` (Orchestrator) и `../orchestrator/budget.md` (LLMBudget).
- Общий фундамент LLM-слоя (`LLMClient`, исключения, оценка токенов,
  выбор провайдера) → `../llm/core.md`.
- Единственный реализованный провайдер (`GroqClient`, лимитер, калибратор)
  → `../llm/groq_client.md`.
- Contracts structured-output и промпты ролей → `../llm/schemas.md`, `../llm/prompts.md`, `../llm/chunking.md`.
- Сами роли (что именно они делают с ответом модели) →
  `../roles/_index.md`.
- Общие структуры данных (`Plan`, `Evidence`, `DraftNote`, ...) →
  `../storage/models.md`.
