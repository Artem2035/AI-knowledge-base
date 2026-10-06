# Цикл одного запроса: от CLI до ответа Groq и обратно

> Это **не reference**: без таблиц параметров. Рассказ о порядке шагов и о том, что через что течёт при `python -m cli.main ask "..."`. За контрактами идите по ссылкам: `../orchestrator/state_machine.md`, `../llm/core.md`, `../llm/groq_client.md`.

## 1. Общая схема

```
cli/main.py (ask/resume) ─► _run_and_report
    │  raw_query | resume_task_id
    ▼
Orchestrator.run(...)          ── держит TaskStatus и TaskCheckpoint
    │
roles/*.py  +  tools/note_assembly.py (сборка без LLM)
    │  client.generate_structured(role=…, prompt=…, response_model=…, status=…, system_instruction=…)
    ▼
LLMClient (Protocol) ─► GroqClient.generate_structured ─► Groq API
    ▼
Pydantic-объект ответа ─► роль конвертирует в объекты storage/models ─► Orchestrator
```

Между слоями передаются примитивы или Pydantic-модели. Сырые HTTP-ответы выше `GroqClient` не поднимаются.

## 2. CLI-обвязка

`cli/main.py::_run_and_report` — общая для `ask` и `resume`. Три колбэка в `orch.run(...)`: `progress_cb` (печать шага), `plan_confirm_cb` (`cli/plan_editor.py::confirm_plan`), `merge_confirm_cb` (`cli/draft_merge_editor.py::confirm_merges`; Orchestrator вызывает его, только если слияние включено). На время интерактивных колбэков spinner останавливается. Команды — `../cli/main.md`.

## 3. Что живёт в памяти и на диске

- **`TaskStatus`** — один объект на сессию, передаётся по ссылке в оба клиента (`self.llm`, `self.extraction_client`), поэтому `MAX_LLM_CALLS_PER_TASK` остаётся общим потолком. Обнуляется в начале каждого `run()`, даже при resume.
- **`TaskCheckpoint`** (v5, `../staging/checkpoint.md`) — на диске после каждого шага и батча: `Plan`, `SectionDraft[]`, `NoteAnnotation[]` и флаги завершённых шагов. `DraftNote` в нём нет: заметки собираются заново из секций и аннотаций.

## 4. Шаги `Orchestrator.run()` и стоящие за ними LLM-вызовы

| # | Шаг | Функция | Клиент | `role=` | LLM? |
|---|---|---|---|---|---|
| 0 | Индексация Vault | `sync_vault_index` | — | — | нет |
| 1 | Planning | `outline_planner.build_plan` | `self.llm` | `outline_planner` | 1 вызов |
| 1.5 | Утверждение плана | `plan_confirm_cb` | — | — | нет |
| 2 | Elaboration | `elaborator.elaborate_outline` | `self.extraction_client` | `elaborator` | ⌈S/3⌉ батчей (+ повторы) |
| 3 | Vault analysis | `vault_analyst.resolve_notes_against_vault` | `self.llm` | `vault_dedup`, `folder_assignment` | «серая зона» + 1 на папки |
| 4 | Annotation | `annotator.annotate_notes` | `self.llm` | `annotator` | ⌈N/6⌉ батчей |
| 5 | Сборка заметок | `note_assembly.build_draft_note` | — | — | нет |
| 5.5 | Слияние (опц.) | `merge_confirm_cb` → `fix_links_after_merge` | — | — | нет |
| 6 | Inline-ссылки | `apply_inline_links` | — | — | нет |
| 7 | MOC | `build_moc` (при ≥2 create) | — | — | нет |
| 8 | Связи | `build_relationships` | — | — | нет |
| 9 | Валидация | `validation.run_validation` | — | — | нет |
| 10 | Staging | `save_changeset`, `delete_checkpoint` | — | — | нет |

S — число подпунктов плана, N — число create-заметок. Оценка для S=24, N=6: planner 1 + elaborator 8 + folder_assignment 1 + vault_dedup 0–2 + annotator 1 ≈ 10–13 вызовов. Ориентир по реальному прогону: 10 вызовов (planner 1, elaborator 5, vault_dedup 2, folder_assignment 1, annotator 1).

**Почему два клиента.** Elaborator самый частый по числу вызовов. У него отдельный `LLMBudget` и, возможно, другая модель (`groq_extraction_model`); при совпадении моделей клиенты делят один `TokenRateLimiter` и калибратор (`../llm/core.md §3.3`), потому что физически бьют в один TPM Groq.

**Что делает код вокруг вызовов:**
- Elaborator возвращает секцию для **каждого** подпункта: неверный или пропавший `unit_index` отбрасывается, один повтор за пропавшими, затем placeholder с `needs_check`; незакрытый fence — повтор, затем закрытие кодом. Чистка текста (`prepare_section`): заголовки `##…####`, омоглифы, U+2011.
- Annotator при сбое возвращает пустую аннотацию: теги, ссылки и резюме не критичны.
- Тело заметки, `abstract`-callout (≥8 разделов), humanities-предупреждение и теги `domain/…` добавляет код.
- `RESEARCH_MODE=web` заблокирован.

## 5. Путь одного вызова `generate_structured`

1. `LLMBudget`: лимит вызовов на задачу, soft-лимит RPD, локальный throttle RPM.
2. Формат ответа: strict `json_schema` для `gpt-oss-*`, иначе `json_object` + схема в тексте (`../llm/groq_client.md §6.5`).
3. Оценка токенов system и схемы; резерв вывода по роли: `groq_reserved_output_by_role` либо накопленная EMA калибратора, не ниже пола.
4. Если промпт не влезает в TPM-бюджет, `_auto_truncate_prompt` режет его с конца. Для списков этого избегают батчингом ДО вызова (`../llm/chunking.md`).
5. Параметры рассуждений (`include_reasoning`, `reasoning_effort` по роли) для поддерживаемых моделей.
6. `TokenRateLimiter.wait_and_reserve`: ожидание следующей календарной минуты, если места не хватает.
7. HTTP-запрос с retry на rate limit и сетевые сбои (`tenacity`); при отказе API от строгой схемы или параметров рассуждений — разовый откат.
8. `_parse_with_repair`; `LLMBudget.register_call`.
9. Обучение калибраторов фактическим расходом: вход и вывод калибруются раздельно.

Детали каждого шага — `../llm/groq_client.md`.

## 6. Что происходит при исчерпании лимита

Устойчивая 429, `MAX_LLM_CALLS_PER_TASK` или дневной soft-лимит дают `LLMFreeLimitReached` / `LLMTaskBudgetExceeded`. Исключение всплывает до `Orchestrator.run()`, который сохраняет чекпоинт на последнем успешном шаге и возвращает `RunResult(stopped=True)` с командой `resume <task_id>`. Перехода на платный tier нет. При resume пропускаются планирование, утверждённый план и уже написанные разделы и аннотации.

## Куда идти дальше

- `../orchestrator/state_machine.md`, `../orchestrator/budget.md`
- `../llm/core.md`, `../llm/groq_client.md`, `../llm/schemas.md`, `../llm/prompts.md`, `../llm/chunking.md`
- `../roles/_index.md`, `../tools/note_assembly.md`
- `../storage/models.md`, `../staging/checkpoint.md`