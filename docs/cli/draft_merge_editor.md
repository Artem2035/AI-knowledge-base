# Документация: `cli/draft_merge_editor.py` — экран объединения заметок

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** `merge_confirm_cb` в `Orchestrator.run()`: вызывается после Writer+Critic, до validation/staging, только при `settings.enable_draft_merging=True`. Логика слияния — `../staging/draft_merge.md`.

## `confirm_merges(drafts: list[DraftNote], mode: str = "select") -> list[DraftNote]`

`create_count` — число черновиков с `action == "create"`.

**`mode == "all"`** (`settings.draft_merge_mode`, дефолт):
- `create_count < 2` → сообщение «объединять нечего», возврат `drafts` без изменений;
- иначе запрос заголовка (`typer.prompt`, пусто = авто) и `merge_all_drafts(drafts, merged_title=title)`.

**`mode == "select"`:**
- печатает нумерованный список (у не-`create` пометка «update — не участвует»);
- цикл `while typer.confirm("Объединить несколько заметок в одну?", default=False)`: номера через запятую, валидация — минимум 2 уникальных, в диапазоне, не задействованы в другой группе (`used`), все `action == "create"`; при успехе запрос заголовка (дефолт — `" + ".join(titles)`), группа добавляется в `groups`;
- при непустых `groups` — `apply_merges(drafts, groups)`, иначе `drafts`.

| Имя | Тип | Назначение |
|---|---|---|
| `drafts` | `list[DraftNote]` | Все черновики (create и update). |
| `mode` | `str` | `"all"` или `"select"`. |

**Возвращает:** исходный список либо результат `merge_all_drafts`/`apply_merges`. Ввод валидируется с повторным запросом, но `ValueError` из `merge_*` при рассинхроне данных здесь не перехватывается.

Документация по `cli/draft_merge_editor.py` завершена. Пакет закрыт — обзор `_index.md`.
