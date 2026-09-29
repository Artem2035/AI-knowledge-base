> ⚠ **Не путать `_index.md` (этот файл — обзор пакета) с `index.md` (документация модуля `vault/index.py`).**

# `vault/` — обзор пакета

## Назначение пакета

Слой прямого взаимодействия с Obsidian Vault на файловой системе + локальный SQLite-индекс метаданных. Разделение обязанностей строгое: `reader.py` ТОЛЬКО читает, `writer.py` ТОЛЬКО пишет и вызывается исключительно из `staging/commit.py` после `approve`, `db.py` — кэш метаданных (не дублирует Vault), `index.py` — инкрементальная синхронизация ФС ↔ `db.py`.

## Файлы пакета → документы

| Файл кода | Документ |
|---|---|
| `vault/__init__.py` | — (пустой файл-маркер) |
| `vault/db.py` | `db.md` |
| `vault/reader.py` | `reader.md` |
| `vault/index.py` | `index.md` (⚠ не `_index.md`) |
| `vault/writer.py` | `writer.md` |

## Зависимости

```
Чтение (не пишет никогда):
  reader.read_vault ─► list[ParsedNote] ─► index.VaultIndexer.sync ─► db.VaultDB
                                            └► resolve_wikilink_targets (2-й проход)

Retrieval (без LLM): retrieval/search.py::VaultSearcher ─► db.VaultDB

Запись (ТОЛЬКО после approve):
  staging/commit.py ─► writer.VaultWriter.write_draft ─► файл в Vault
                    └► index.VaultIndexer.sync (переиндексация)
```

## Кто вызывает

`orchestrator/state_machine.py` (`../orchestrator/state_machine.md`), `staging/commit.py` (`../staging/commit.md`), `cli/main.py`, `validation/*` (чтение путей/заголовков из `db`).

## Порядок чтения

1. `reader.md` → 2. `db.md` → 3. `index.md` → 4. `writer.md`.
