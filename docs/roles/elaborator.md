# Документация: `roles/elaborator.py` — роль Elaborator (v2)

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Для каждого подпункта плана (`OutlineSubpoint`) модель пишет **готовый markdown раздела** из собственных знаний (внешних источников нет, `RESEARCH_MODE=knowledge`). Результат — `SectionDraft` (`../storage/models.md`). Заголовок `## …` и сборку заметки добавляет код (`../tools/note_assembly.md`). Промежуточного `Evidence` и отдельных ролей Writer/Critic больше нет.

Самый частый по числу вызовов шаг, поэтому клиент — `Orchestrator.self.extraction_client`. Тег роли: `role="elaborator"`. Системная инструкция зависит от домена: `llm/prompts/elaborator.py::get_system_instruction(plan.domain)` (по одной статичной строке на домен, важно для Groq prompt caching). Схема ответа: `SectionBatchOutput` (`../llm/schemas.md`). Резерв вывода: `groq_reserved_output_by_role["elaborator"]` (2500), `reasoning_effort="low"`.

## 1. Константы

| Имя | Значение | Назначение |
|---|---|---|
| `_MAX_SPLIT_DEPTH` | `2` | Сколько раз делим батч пополам при `LLMSchemaError`. |
| `_MAX_NEIGHBOR_HEADINGS` | `15` | Сколько заголовков соседних разделов показываем модели («не повторяй их»). |

## 2. `class ElaborationUnit` (dataclass)

Единица батчинга — один подпункт одной заметки: `note: OutlineNote`, `subpoint: OutlineSubpoint`.

## 3. Чистый код (без LLM)

### `build_elaboration_units(plan, already_done_subpoint_ids) -> list[ElaborationUnit]`
Плоский список подпунктов в порядке `plan.notes` → `note.subpoints`, без уже обработанных (resume).

### `_render_unit(u, index=None) -> str` (приватная)
Текст единицы для промпта: `=== Раздел [i] ===`, заметка, заголовок раздела, `kind`, техзадание (`covers`), заголовки остальных разделов этой заметки (до 15).

### `_frame(plan) -> str` (приватная)
Статичная рамка промпта: тема, домен, требование написать каждый раздел отдельно и указать `unit_index`.

### `_placeholder(u) -> SectionDraft` (приватная)
Секция с `PLACEHOLDER_MARKDOWN` и `needs_check=True` (с `logger.warning`).

### `_accept_sections(output, units, *, final) -> tuple[dict[int, SectionDraft], list[int]]` (приватная)
Принимает валидные секции из ответа. Отбрасываются: `unit_index` вне диапазона, дубль индекса, пустой текст после `prepare_section`. Приписывать текст «первому разделу» нельзя: это портит содержимое. Секция с незакрытым fence: при `final=False` отбрасывается (попадёт в `missing` и будет повторена), при `final=True` fence закрывается кодом и ставится `needs_check=True`. **Возвращает** `(принятые по индексу, список пропавших индексов)`.

## 4. Вызовы LLM

### `_request_sections(units, plan, client, status, system_instruction) -> SectionBatchOutput` (приватная)
Нумерованный листинг + `generate_structured(role="elaborator", …)`.

### `_elaborate_batch(units, plan, client, status, system_instruction, *, depth=0, final=False) -> list[SectionDraft]` (приватная)
Возвращает секцию для **каждой** единицы (при неудаче — placeholder). Логика:
- `LLMPromptTooLargeError`: одна единица → placeholder; иначе деление пополам;
- `LLMSchemaError`: при `depth >= 2` или одной единице → placeholders; иначе деление пополам с `depth + 1`;
- затем `_accept_sections`; если есть пропавшие/битые и `final=False` — **один** повтор только за ними (`final=True`); при `final=True` оставшиеся становятся placeholder'ами.

Остальные исключения (лимиты) пробрасываются.

### `elaborate_outline(plan, client, status, already_done_subpoint_ids, on_batch_done, max_subpoints_per_batch) -> None`
Строит единицы, делит их `batch_for_quality_and_budget` (токен-бюджет и потолок `max_subpoints_per_batch`, `settings.max_subpoints_per_generation_batch`, дефолт 3), на каждый батч вызывает `_elaborate_batch` и `on_batch_done(subpoint_ids, sections)`. В колбэк уходят **все** подпункты батча, включая placeholder'ы: Orchestrator обязан немедленно персистить их (`../staging/checkpoint.md`), иначе при resume они были бы запрошены заново.

### `elaborate_outline_sync(plan, client, status, max_subpoints_per_batch=3) -> list[SectionDraft]`
Без чекпоинтинга: для тестов и прямых вызовов.

Документация по `roles/elaborator.py` завершена. Далее — `vault_analyst.md`.