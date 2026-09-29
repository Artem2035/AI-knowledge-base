# `tools/` — обзор пакета

## Назначение пакета

«Skills» — переиспользуемые детерминированные функции без LLM. Ничего здесь не вызывает `generate_structured(...)` — этим `tools/` отличается от `roles/` (роль = LLM-вызов + сборка промпта, skill = чистый код).

## Файлы пакета → документы

| Файл кода | Документ | Режим |
|---|---|---|
| `tools/__init__.py` | — (пустой файл-маркер) | — |
| `tools/web_search.py` | `web_search.md` | только `RESEARCH_MODE=web` |
| `tools/web_fetch.py` | `web_fetch.md` | только `RESEARCH_MODE=web` |
| `tools/markdown_tools.py` | `markdown_tools.md` | всегда |
| `tools/dedup.py` | `dedup.md` | всегда |

## Кто использует

| Skill | Потребители |
|---|---|
| `web_search`, `web_fetch` | `roles/researcher.py` (web-режим, сейчас заблокирован в `Orchestrator.run()`) |
| `markdown_tools` | `roles/synthesizer_writer.py`, `vault/writer.py`, `staging/changeset.py`, `staging/draft_merge.py`, `validation/link_validator.py`, `orchestrator/state_machine.py` |
| `dedup` | `roles/vault_analyst.py`, `retrieval/search.py`, `vault/index.py`, `cli/main.py`, `orchestrator/state_machine.py` |

## Порядок чтения

`markdown_tools.md` (самый используемый) → `dedup.md` → `web_search.md` → `web_fetch.md`.
