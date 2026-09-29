# `cli/` — обзор пакета

## Назначение пакета

Единственный слой представления MVP — Typer-CLI поверх `Orchestrator`. Бизнес-логики workflow здесь нет: только консольный ввод/вывод, интерактивные callback'и и команда `approve`, единственная, что реально пишет в Vault (через `staging/commit.py`).

## Файлы пакета → документы

| Файл кода | Документ |
|---|---|
| `cli/__init__.py` | — (пустой файл-маркер) |
| `cli/main.py` | `main.md` |
| `cli/plan_editor.py` | `plan_editor.md` |
| `cli/draft_merge_editor.py` | `draft_merge_editor.md` |

## Зависимости

```
main.py::ask/resume ─► _run_and_report ─► Orchestrator.run(...)
    progress_cb ───────► печать
    plan_confirm_cb ───► plan_editor.confirm_plan(plan)
    merge_confirm_cb ──► draft_merge_editor.confirm_merges(drafts, mode)

main.py::approve ─► staging/changeset.load_changeset ─► staging/diff.render_diff_summary
                 ─► typer.confirm ─► staging/commit.commit_changeset ─► vault/writer.py
```

Поток одного запроса — `../flows/llm_cycle.md §2`.

## Порядок чтения

`main.md` → `plan_editor.md` → `draft_merge_editor.md`.
