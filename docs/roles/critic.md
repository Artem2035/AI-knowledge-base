# Документация: `roles/critic.py` — роль Critic (bounded-retry ревью)

> Reference-док. Обзор пакета — `_index.md`. Использует
> `synthesizer_writer.md::write_note` для (пере)написания заметки.

**Назначение.** Работает ПОСЛЕ Writer, на уже НАПИСАННОМ тексте заметки (а
не на сыром evidence). Проверяет ВНУТРЕННЮЮ согласованность и полноту
относительно уже собранного evidence — фактическую точность против
внешнего мира проверить нечем (см. `../llm/prompts.md §2`). Реализует
bounded retry: НЕ цикл "пока не одобрит", а жёсткий потолок `max_rounds`
переписываний (см. `../GLOSSARY.md` — "bounded retry, не цикл до ok").

## 1. `review_draft(draft: DraftNote, assigned_evidence: list[Evidence], client: LLMClient, status: TaskStatus) -> CriticVerdictOutput`

**Описание.** Один вызов ревью. Берёт текст заметки
(`draft.append_section or draft.body_md` — т.е. для update-заметок
ревьюется именно добавляемый блок, не весь файл), строит листинг фактов,
которые должны быть отражены (или заглушку "факты не были назначены явно",
если `assigned_evidence` пуст), делает
`generate_structured(role="critic", ...)` с системной инструкцией
`llm/prompts/critic.py::SYSTEM_INSTRUCTION` (`../llm/prompts.md §2`).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `draft` | `DraftNote` | Уже написанная заметка (результат `synthesizer_writer.write_note`). |
| `assigned_evidence` | `list[Evidence]` | Факты, назначенные ИМЕННО этой заметке (фильтрация по `note_id` делается ВНЕ этой функции, в `run_critic_cycle`, §2). |
| `client` | `LLMClient` | LLM-клиент. |
| `status` | `TaskStatus` | Учёт бюджета. |

**Возвращаемое значение:** `CriticVerdictOutput` (`../llm/schemas.md §5`) —
`verdict: Literal["ok", "rewrite"]`, `feedback: str` (заполнен только при
`"rewrite"`).

**Исключения:** пробрасывает ошибки `client.generate_structured(...)`.

**Тег роли для `GroqClient`:** `role="critic"`.

## 2. `run_critic_cycle(note, evidence, known_titles, title_map, client, status, max_rounds, mark_source=None, system_instruction=None) -> DraftNote`

**Описание.** Оркестрирует цикл "Writer → Critic → (при rewrite) Writer
снова" для ОДНОЙ заметки, СТРОГО ОГРАНИЧЕННЫЙ `max_rounds` переписываний:

1. Пишет первую версию: `synthesizer_writer.write_note(note, evidence,
   known_titles, title_map, client, status, mark_source=mark_source,
   system_instruction=system_instruction)` (`../roles/synthesizer_writer.md §2`).
2. Если `max_rounds <= 0` — критик вообще не вызывается, возвращает первую
   версию как есть (эквивалент "критик выключен").
3. Иначе фильтрует `assigned_evidence = [e for e in evidence if e.note_id
   == note.note_id]` (важно: только здесь, а не в `review_draft`).
4. Цикл `while rounds < max_rounds`: вызывает `review_draft(...)` (§1);
   если `verdict == "ok"` — `break`; иначе `rounds += 1` и заметка
   переписывается заново через `write_note(...,
   extra_instructions=verdict.feedback, ...)`.
5. После выхода из цикла — `draft.critic_rounds = rounds`,
   `draft.needs_review = (rounds >= max_rounds and последний verdict ==
   "rewrite")`.

**Ключевое архитектурное решение:** если бюджет раундов исчерпан, а
вердикт всё ещё `"rewrite"` — заметка уходит в staging КАК ЕСТЬ (последняя
переписанная версия), С ПОМЕТКОЙ `needs_review=True`, БЕЗ дополнительного
финального ре-ревью (сознательная экономия одного вызова критика — иначе
цикл не был бы по-настоящему ограниченным).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `note` | `OutlineNote` | Заметка плана. |
| `evidence` | `list[Evidence]` | Весь evidence задачи (фильтрация внутри). |
| `known_titles` | `list[str]` | См. `synthesizer_writer.md §1`. |
| `title_map` | `dict[str, str]` | См. `synthesizer_writer.md §1`. |
| `client` | `LLMClient` | LLM-клиент (один и тот же для Writer и Critic — оба вызова идут через один `client`). |
| `status` | `TaskStatus` | Учёт бюджета. |
| `max_rounds` | `int` | Жёсткий потолок переписываний. Из `settings.max_critic_rounds` (дефолт `1`, `../config/settings.md §10`). `0` — критик полностью выключен. |
| `mark_source` | `str \| None` (дефолт `None`) | Прокидывается во ВСЕ вызовы `write_note` (включая переписывания). |
| `system_instruction` | `str \| None` (дефолт `None`) | Прокидывается во ВСЕ вызовы `write_note`. |

**Возвращаемое значение:** `DraftNote` (`../storage/models.md §4.2`) —
финальная (возможно, переписанная) версия, с дополнительно проставленными
`critic_rounds` и `needs_review`.

**Исключения:** пробрасывает ошибки `client.generate_structured(...)` из
`write_note`/`review_draft` (в т.ч. `LLMTaskBudgetExceeded`/
`LLMFreeLimitReached`, `../orchestrator/budget.md §1–2` — если бюджет
кончился посреди цикла критика, заметка НЕ будет достроена в рамках этой
сессии; `Orchestrator` в этом случае ловит исключение на уровне всей
задачи, и заметка не попадёт в `checkpoint.drafts`, т.к.
`persist("synthesizing")` вызывается только ПОСЛЕ успешного завершения
`run_critic_cycle` для заметки).

---

## Сводная таблица: роль → тег для `GroqClient` → клиент `Orchestrator`

| Файл / функция | `role=` | Клиент | Активность |
|---|---|---|---|
| `outline_planner.build_plan` | `"outline_planner"` | `self.llm` | Всегда |
| `elaborator._elaborate_batch` | `"elaborator"` | `self.extraction_client` | `RESEARCH_MODE=knowledge` (дефолт) |
| `vault_analyst._resolve_ambiguous` | `"vault_dedup"` | `self.llm` | Всегда, только для "серой зоны" |
| `vault_analyst._assign_folders_batch` | `"folder_assignment"` | `self.llm` | Всегда, только для НОВЫХ заметок |
| `synthesizer_writer.write_note` | `"synthesizer_write"` | `self.llm` | Всегда, раз (и более) на заметку |
| `critic.review_draft` | `"critic"` | `self.llm` | Всегда, если `max_critic_rounds > 0` |

Документация по `roles/critic.py` завершена — весь активный пакет
`roles/` документирован. Обзор — `_index.md`.
