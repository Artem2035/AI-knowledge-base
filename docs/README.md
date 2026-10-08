# Индекс документации `docs/`

> Карта отвечает на два вопроса: «где документация нужного модуля кода?» и «в каком порядке это читать?». `architecture.md` (исторический обзор Phase 1) в индекс не входит и частично устарел: в нём описаны удалённые роли Writer и Critic и цепочка Evidence (в начале файла стоит пометка об этом).

## Структура

Документация зеркалит пакеты кода (`docs/llm/` ↔ `llm/`, и т.д.); правила — в `CONTRIBUTING.md`.

```
docs/
├── README.md, GLOSSARY.md, CONTRIBUTING.md
├── architecture.md            (вне индекса, исторический)
├── flows/llm_cycle.md
├── llm/           _index, core, groq_client, schemas, prompts, chunking
├── orchestrator/  _index, state_machine, budget
├── storage/       _index, models
├── config/        _index, settings, env
├── roles/         _index, outline_planner, elaborator, vault_analyst, annotator, synthesizer_writer
├── staging/       _index, changeset, checkpoint, commit, diff, draft_merge
├── vault/         _index, db, reader, index, writer
├── tools/         _index, note_assembly, markdown_tools, dedup, web_search, web_fetch
├── validation/    _index, autofix, yaml_validator, markdown_validator, link_validator
└── cli/           _index, main, plan_editor, draft_merge_editor
```

## Как читать

- **Reference-доки** — по файлу кода (или пакету): классы, функции, параметры, исключения. Открываются точечно.
- **Flow-доки** (`flows/`) — рассказ о процессе, без таблиц параметров, только ссылки.
- **Руководства без файла кода** (`config/env.md`) — практика; их `_index.md` помечает явно.

Рекомендованный порядок:
1. `config/env.md` — если нужно просто запустить.
2. `flows/llm_cycle.md` — путь одного запроса.
3. `storage/models.md` — общие структуры данных.
4. `orchestrator/` → `llm/` (`core` → `groq_client` → `schemas` → `prompts` → `chunking`) — LLM-слой.
5. `roles/` и `tools/note_assembly.md` — роли и детерминированная сборка заметок.
6. `vault/`, `staging/`, `validation/` — всё вокруг Vault без LLM.
7. `tools/`, `cli/`, `config/settings.md` — периферия и справочник настроек.

Термины — `GLOSSARY.md`.

## Карта: модуль кода → документ

| Модуль кода | Документ |
|---|---|
| `orchestrator/state_machine.py` | `orchestrator/state_machine.md` |
| `orchestrator/budget.py` | `orchestrator/budget.md` |
| `llm/base.py`, `llm/common.py`, `llm/factory.py` | `llm/core.md` |
| `llm/groq_client.py` | `llm/groq_client.md` |
| `llm/schemas.py` | `llm/schemas.md` |
| `llm/prompts/*` (весь подпакет) | `llm/prompts.md` |
| `llm/chunking.py` | `llm/chunking.md` |
| Цикл одного запроса (flow) | `flows/llm_cycle.md` |
| `storage/models.py` | `storage/models.md` |
| `config/settings.py` | `config/settings.md` |
| Руководство по настройке `.env` (файла кода нет) | `config/env.md` |
| `roles/outline_planner.py` | `roles/outline_planner.md` |
| `roles/elaborator.py` | `roles/elaborator.md` |
| `roles/vault_analyst.py` | `roles/vault_analyst.md` |
| `roles/annotator.py` | `roles/annotator.md` |
| `roles/synthesizer_writer.py` (`prepare_linking_context`, `build_relationships`) | `roles/synthesizer_writer.md` |
| `tools/note_assembly.py` | `tools/note_assembly.md` |
| `tools/markdown_tools.py`, `dedup.py`, `web_search.py`, `web_fetch.py` | `tools/<имя>.md` |
| `staging/changeset.py`, `checkpoint.py`, `commit.py`, `diff.py`, `draft_merge.py` | `staging/<имя>.md` |
| `vault/db.py`, `reader.py`, `index.py`, `writer.py` | `vault/db.md`, `reader.md`, `index.md` (⚠ не `_index.md`), `writer.md` |
| `validation/__init__.py` (`run_validation`) | `validation/_index.md` |
| `validation/autofix.py` | `validation/autofix.md` |
| `validation/yaml_validator.py`, `markdown_validator.py`, `link_validator.py` | `validation/<имя>.md` |
| `cli/main.py`, `plan_editor.py`, `draft_merge_editor.py` | `cli/<имя>.md` |
| Обзор продукта (Phase 1, исторический) | `architecture.md` (вне индекса) |

## Осознанно не документируется

Не добавлять «на всякий случай» (см. `CONTRIBUTING.md`):
- `llm/router.py`, `llm/openrouter_client.py` — второй провайдер, вне фокуса MVP;
- `roles/researcher.py`, `roles/extractor_critic.py` — только `RESEARCH_MODE=web`, заблокирован в `Orchestrator.run()`.

## Пробелы и отложенное

- `retrieval/search.py` (`VaultSearcher`) активно используется `Orchestrator` и `roles/vault_analyst.py`, но отдельного документа пока нет (кандидат на следующую итерацию).
- В документах ссылки на «осознанно не документируемые» модули остаются текстом без перехода.