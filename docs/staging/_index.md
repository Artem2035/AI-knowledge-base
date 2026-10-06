# `staging/` — обзор пакета

## Назначение пакета

Слой между «LLM закончил работу» и «изменения попали в реальный Vault». Весь код здесь **без LLM**: сохранение и загрузка `StagingChangeset`, чекпоинты для `resume`, единственная точка записи в Vault (`commit.py`), человекочитаемый diff, опциональное объединение готовых заметок.

## Файлы пакета → документы

| Файл кода | Документ |
|---|---|
| `staging/__init__.py` | — (пустой файл-маркер) |
| `staging/changeset.py` | `changeset.md` |
| `staging/checkpoint.py` | `checkpoint.md` (v5) |
| `staging/commit.py` | `commit.md` |
| `staging/diff.py` | `diff.md` |
| `staging/draft_merge.py` | `draft_merge.md` |

## Зависимости

```
Orchestrator.run()
   │ после каждого шага/батча
   ├─► checkpoint.save_checkpoint   (Plan, SectionDraft[], NoteAnnotation[])
   │
   └─ build_draft_note ─► [draft_merge.merge_* ─► fix_links_after_merge] ─► apply_inline_links
         ─► build_moc ─► build_relationships ─► validation.run_validation
         ─► changeset.save_changeset ─► checkpoint.delete_checkpoint
                                              │
                         diff.render_diff_summary ─► CLI (approve)
                                              │ user: y
                changeset.load_changeset ─► commit.commit_changeset ─► vault/writer.py
```

Два **разных** механизма персистентности: `TaskCheckpoint` — после каждого шага и батча, пока задача не дошла до staging; `StagingChangeset` — один раз, на последнем шаге. `DraftNote` в чекпоинте не хранятся, собираются заново.

## Кто вызывает

`orchestrator/state_machine.py` (`../orchestrator/state_machine.md`), `cli/main.py` (`../cli/main.md`), `cli/draft_merge_editor.py`.

## Порядок чтения

1. `checkpoint.md` — механизм resume.
2. `changeset.md` → `diff.md` — что видит пользователь.
3. `draft_merge.md` — опциональное объединение.
4. `commit.md` — запись в Vault после approve.