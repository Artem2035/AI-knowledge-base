# `staging/` — обзор пакета

> 1 экран: что лежит в `staging/`, кто с кем связан, куда идти за деталями.

## Назначение пакета

Слой между "LLM закончил работу" и "изменения попали в реальный Vault".
Весь код здесь ЧИСТЫЙ (без LLM), детерминированный: сохранение/загрузка
`StagingChangeset`, чекпоинты для `resume`, единственная точка записи в
реальный Vault (`commit.py`), человекочитаемый diff, опциональное
объединение готовых заметок.

## Файлы пакета → документы

| Файл кода | Документ |
|---|---|
| `staging/__init__.py` | — (пустой файл-маркер) |
| `staging/changeset.py` | `changeset.md` |
| `staging/checkpoint.py` | `checkpoint.md` |
| `staging/commit.py` | `commit.md` |
| `staging/diff.py` | `diff.md` |
| `staging/draft_merge.py` | `draft_merge.md` |

## Зависимости

```
Orchestrator.run() ─► draft_merge (опц.) ─► validation.run_validation ─► changeset.save_changeset
        │ (после каждого шага)                                              │
        └─► checkpoint.save_checkpoint                                      ▼
                                                            diff.render_diff_summary ─► CLI
                                                                            │ user: approve
                                                                            ▼
                                            changeset.load_changeset ─► commit.commit_changeset ─► vault/writer.py
```

Два РАЗНЫХ механизма персистентности: `TaskCheckpoint` (`checkpoint.md`) —
после КАЖДОГО шага, пока задача не дошла до staging; `StagingChangeset`
(`changeset.md`) — только на последнем шаге.

## Кто вызывает

`orchestrator/state_machine.py` (`../orchestrator/state_machine.md`),
`cli/main.py` (`../cli/_index.md`).

## Порядок чтения

1. `checkpoint.md` — механизм resume.
2. `changeset.md` → `diff.md` — что видит пользователь.
3. `draft_merge.md` — опциональное объединение.
4. `commit.md` — запись в Vault после approve.
