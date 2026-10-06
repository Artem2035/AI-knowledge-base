# `tools/` — обзор пакета

## Назначение пакета

«Skills» — переиспользуемые детерминированные функции без LLM. Ничего здесь не вызывает `generate_structured(...)`; этим `tools/` отличается от `roles/` (роль = LLM-вызов + сборка промпта).

## Файлы пакета → документы

| Файл кода | Документ | Режим |
|---|---|---|
| `tools/__init__.py` | — (пустой файл-маркер) | — |
| `tools/note_assembly.py` | `note_assembly.md` | всегда |
| `tools/markdown_tools.py` | `markdown_tools.md` | всегда |
| `tools/dedup.py` | `dedup.md` | всегда |
| `tools/web_search.py` | `web_search.md` | только `RESEARCH_MODE=web` |
| `tools/web_fetch.py` | `web_fetch.md` | только `RESEARCH_MODE=web` |

## Кто использует

| Skill | Потребители |
|---|---|
| `note_assembly` | `orchestrator/state_machine.py` (`build_draft_note`, `apply_inline_links`, `build_moc`), `roles/elaborator.py` (`prepare_section`, `close_unbalanced_fence`, `PLACEHOLDER_MARKDOWN`), `roles/annotator.py` (`plain_text`, `ABSTRACT_MIN_SECTIONS`), `staging/draft_merge.py`, `validation/markdown_validator.py` |
| `markdown_tools` | `tools/note_assembly.py`, `roles/annotator.py`, `roles/synthesizer_writer.py`, `vault/writer.py`, `staging/changeset.py`, `staging/draft_merge.py`, `validation/link_validator.py`, `orchestrator/state_machine.py` |
| `dedup` | `roles/vault_analyst.py`, `retrieval/search.py`, `vault/index.py`, `cli/main.py`, `orchestrator/state_machine.py` |
| `web_search`, `web_fetch` | `roles/researcher.py` (web-режим, заблокирован в `Orchestrator.run()`) |

## Порядок чтения

`note_assembly.md` → `markdown_tools.md` → `dedup.md` → `web_search.md` → `web_fetch.md`.