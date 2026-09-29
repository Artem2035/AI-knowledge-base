# Индекс документации `docs/`

> Карта отвечает на два вопроса: «где документация нужного модуля кода?» и «в каком порядке это читать?». `architecture.md` (общий обзор продукта, Phase 1) в индекс не входит.

## Структура

Документация зеркалит пакеты кода (`docs/llm/` ↔ `llm/`, и т.д.); правила — в `CONTRIBUTING.md`.

```
docs/
├── README.md, GLOSSARY.md, CONTRIBUTING.md
├── architecture.md            (вне индекса)
├── flows/llm_cycle.md
├── llm/           _index, core, groq_client, schemas, prompts, chunking
├── orchestrator/  _index, state_machine, budget
├── storage/       _index, models
├── config/        _index, settings, env
├── roles/         _index, outline_planner, elaborator, vault_analyst, synthesizer_writer, critic
├── staging/       _index, changeset, checkpoint, commit, diff, draft_merge
├── vault/         _index, db, reader, index, writer
├── tools/         _index, web_search, web_fetch, markdown_tools, dedup
├── validation/    _index, yaml_validator, markdown_validator, link_validator
└── cli/           _index, main, plan_editor, draft_merge_editor
```

## Как читать

- **Reference-доки** — по файлу кода (или пакету): классы, функции, параметры, исключения. Открываются точечно.
- **Flow-доки** (`flows/`) — рассказ о процессе, без таблиц параметров, только ссылки.

Рекомендованный порядок:
1. `flows/llm_cycle.md` — путь одного запроса.
2. `storage/models.md` — общие структуры данных.
3. `orchestrator/` → `llm/` (`core` → `groq_client` → `schemas` → `prompts` → `chunking`) — LLM-слой.
4. `roles/` — роли.
5. `vault/`, `staging/`, `validation/` — всё вокруг Vault без LLM.
6. `tools/`, `cli/`, `config/` — периферия (для первого запуска начните с `config/env.md`).

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
| Цикл одного LLM-вызова (flow) | `flows/llm_cycle.md` |
| `storage/models.py` | `storage/models.md` |
| `config/settings.py` | `config/settings.md` |
| руководство по настройке `.env` | `config/env.md` |
| `roles/outline_planner.py` | `roles/outline_planner.md` |
| `roles/elaborator.py` | `roles/elaborator.md` |
| `roles/vault_analyst.py` | `roles/vault_analyst.md` |
| `roles/synthesizer_writer.py` | `roles/synthesizer_writer.md` |
| `roles/critic.py` | `roles/critic.md` |
| `staging/changeset.py`, `checkpoint.py`, `commit.py`, `diff.py`, `draft_merge.py` | `staging/<имя>.md` |
| `vault/db.py`, `reader.py`, `index.py`, `writer.py` | `vault/db.md`, `reader.md`, `index.md` (⚠ не `_index.md`), `writer.md` |
| `tools/web_search.py`, `web_fetch.py`, `markdown_tools.py`, `dedup.py` | `tools/<имя>.md` |
| `validation/__init__.py` (`run_validation`) | `validation/_index.md` |
| `validation/yaml_validator.py`, `markdown_validator.py`, `link_validator.py` | `validation/<имя>.md` |
| `cli/main.py`, `plan_editor.py`, `draft_merge_editor.py` | `cli/<имя>.md` |
| Обзор продукта (Phase 1) | `architecture.md` (вне индекса) |

## Осознанно не документируется

Не добавлять «на всякий случай» (см. `CONTRIBUTING.md`):
- `llm/router.py`, `llm/openrouter_client.py` — второй провайдер, вне фокуса MVP;
- `roles/researcher.py`, `roles/extractor_critic.py` — только `RESEARCH_MODE=web`, заблокирован в `Orchestrator.run()`;
- `retrieval/search.py` — решение отложено (модуль возможно будет удалён).

В уже написанных доках ссылки на эти модули остаются текстом без перехода.
