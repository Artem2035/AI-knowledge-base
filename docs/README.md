# Индекс документации `docs/`

> Эта карта отвечает на два вопроса: «где документация нужного модуля кода?»
> и «в каком порядке это вообще читать?». `architecture.md` (общий обзор
> продукта и решений Phase 1) в этот индекс пока не включён — он отдельный
> и рассматривается позже.

## Структура

Документация организована ПОДПАПКАМИ, зеркалящими структуру пакетов кода
(`docs/llm/` ↔ `llm/`, `docs/orchestrator/` ↔ `orchestrator/`) — это сделано,
чтобы «где документация модуля X» читалось из самого пути, без похода в
таблицу ниже. Модули, для которых пока существует только один
reference-файл на весь пакет (`vault/`, `tools/`, `staging/`, `validation/`,
`roles/`, `cli/`, `storage/`, `config/`), остаются плоскими файлами в корне
`docs/` — миграция их в такие же подпапки не сделана в этом заходе (это
чисто механическая работа: переместить файл и поправить относительные
ссылки, без изменения содержания), см. раздел «Пробелы» ниже.

```
docs/
├── README.md                              (этот файл)
├── architecture.md                         (не входит в этот индекс)
├── llm_cycle.md                            (flow-док: путь одного запроса)
├── llm/
│   ├── core.md                             (base.py, common.py, factory.py)
│   └── groq_client.md                      (groq_client.py)
├── orchestrator/
│   └── reference.md                        (state_machine.py, budget.py)
├── docs_llm_schemas_prompts_chunking.md    (schemas.py, prompts/*, chunking.py — ещё не в llm/)
├── docs_roles_part1.md / docs_roles_part2.md
├── docs_storage_models.md
├── docs_staging.md
├── docs_vault.md
├── docs_tools.md
├── docs_validation.md
├── docs_cli.md
└── docs_config_settings.md
```

## Как читать

Два рода файлов, их не стоит путать:

- **Reference-доки** (`docs_*.md` в корне, либо файлы внутри `llm/`/
  `orchestrator/`) — по одному (или по группе близких) файлу кода: классы,
  функции, параметры, возвращаемые значения, исключения. Справочник, не
  рассказ — открывается точечно.
- **Flow-доки** (без префикса `docs_`, напр. `llm_cycle.md`) — рассказывают,
  что происходит на протяжении одного запроса: что вызывается, в каком
  порядке, что течёт между вызовами. **Не повторяют** сигнатуры и таблицы
  параметров — только ссылаются на нужный reference-док.

Рекомендованный порядок первого чтения:

1. `llm_cycle.md` — путь одного вызова LLM целиком (Orchestrator → роль →
   клиент → провайдер), только со ссылками на детали.
2. `docs_storage_models.md` — общие структуры данных.
3. `orchestrator/reference.md` → `llm/core.md` → `llm/groq_client.md` →
   `docs_llm_schemas_prompts_chunking.md` — весь LLM-слой снизу вверх.
4. `docs_roles_part1.md` / `docs_roles_part2.md` — роли, использующие
   LLM-слой.
5. `docs_vault.md`, `docs_staging.md`, `docs_validation.md` — всё вокруг
   Vault и без LLM.
6. `docs_tools.md`, `docs_cli.md`, `docs_config_settings.md` — периферия.

## Карта: модуль кода → документ

| Модуль кода | Документ | Статус |
|---|---|---|
| `orchestrator/state_machine.py` | `orchestrator/state_machine.md` | ✅ |
| `orchestrator/budget.py` | `orchestrator/budget.md` | ✅ |
| `llm/base.py`, `llm/common.py`, `llm/factory.py` | `llm/core.md` | ✅ |
| `llm/groq_client.py` | `llm/groq_client.md` | ✅ |
| `llm/router.py`, `llm/openrouter_client.py` | — | ❌ намеренно не документируются (второй провайдер, вне текущего фокуса MVP) |
| `llm/schemas.py` | `llm/schemas.md` | ✅ |
| `llm/prompts/*` (весь подпакет) | `llm/prompts.md` | ✅ |
| `llm/chunking.py` | `llm/chunking.md` | ✅ |
| Сам цикл одного LLM-вызова (flow, не reference) | `flows/llm_cycle.md` | ✅ |
| `storage/models.py` | `storage/models.md` | ✅ |
| `config/settings.py` | `config/settings.md` | ✅ |
| `roles/outline_planner.py`, `roles/researcher.py`, `roles/extractor_critic.py` | `docs_roles_part1.md` | ⏳ переезд в `roles/*.md`; `researcher`/`extractor_critic` не переносятся (web-режим) |
| `roles/elaborator.py`, `roles/vault_analyst.py`, `roles/synthesizer_writer.py`, `roles/critic.py` | `docs_roles_part2.md` | ⏳ переезд в `roles/*.md` |
| `staging/changeset.py`, `staging/checkpoint.py`, `staging/commit.py`, `staging/diff.py`, `staging/draft_merge.py` | `docs_staging.md` | ⏳ переезд в `staging/*.md` |
| `cli/main.py`, `cli/plan_editor.py`, `cli/draft_merge_editor.py` | `docs_cli.md` | ⏳ переезд в `cli/*.md` |
| `vault/db.py`, `vault/reader.py`, `vault/index.py`, `vault/writer.py` | `docs_vault.md` | ⏳ переезд в `vault/*.md` |
| `tools/web_search.py`, `tools/web_fetch.py`, `tools/markdown_tools.py`, `tools/dedup.py` | `docs_tools.md` | ⏳ переезд в `tools/*.md` |
| `validation/__init__.py`, `validation/yaml_validator.py`, `validation/markdown_validator.py`, `validation/link_validator.py` | `docs_validation.md` | ⏳ переезд в `validation/*.md` |
| `retrieval/search.py` | — | ❌ не документирован (возможно, будет удалён — см. `CONTRIBUTING.md`) |
| Общий обзор продукта/архитектуры (Phase 1) | `architecture.md` | не входит в этот индекс (пока) |

## Что уже реально мигрировано в новую структуру (подпапки)

```
docs/
├── README.md, GLOSSARY.md, CONTRIBUTING.md
├── flows/llm_cycle.md
├── llm/            _index.md, core.md, groq_client.md, schemas.md, prompts.md, chunking.md
├── orchestrator/   _index.md, state_machine.md, budget.md
├── storage/        _index.md, models.md
└── config/         _index.md, settings.md
```

Остальные пакеты (`roles/`, `staging/`, `vault/`, `tools/`, `validation/`,
`cli/`) ещё лежат старыми плоскими файлами в корне — миграция продолжается.

## Пробелы и осознанно отложенное

- `llm/router.py` (`RoleRoutingLLMClient`) и `llm/openrouter_client.py`
  (`OpenRouterClient`) — второй провайдер с failover между
  моделями-кандидатами; по решению пользователя не документируются в этом
  заходе. Если понадобится — по структуре они должны лечь рядом, в
  `docs/llm/router.md` и `docs/llm/openrouter_client.md`.
- `retrieval/search.py` (`VaultSearcher`, `RetrievalHit`) — нигде не описан
  отдельным reference-доком; упоминается только контекстно из
  `docs_roles_part2.md §2` и `docs_vault.md`.
- Плоские доки (`docs_vault.md`, `docs_tools.md`, `docs_staging.md`,
  `docs_validation.md`, `docs_roles_part1/2.md`, `docs_cli.md`,
  `docs_config_settings.md`, `docs_storage_models.md`) не перенесены в
  подпапки (`vault/`, `tools/`, `staging/`, `validation/`, `roles/`, `cli/`,
  `config/`, `storage/`) — их содержание не требует изменений, только
  перемещение файла и правку относительных ссылок; сделано не было, чтобы
  не тратить контекст на механическую работу без явного запроса.
- `docs_llm_schemas_prompts_chunking.md` логически часть `llm/` (описывает
  `llm/schemas.py`, `llm/prompts/*`, `llm/chunking.py`), но пока лежит в
  корне — по той же причине, что и выше.

## Что удалить из репозитория

`docs_llm_cycle_part1_orchestrator.md`, `docs_llm_cycle_part2_common.md`,
`docs_llm_cycle_part3_groq_client.md` — их содержимое целиком
перераспределено:

| Было | Стало |
|---|---|
| part1 §2 (`Orchestrator`), §3 (`LLMBudget`) | `orchestrator/reference.md` |
| part1 §4 (`llm/factory.py`), §5 (`LLMClient` Protocol) | `llm/core.md` |
| part1 §0, §2.1 (таблица шагов run()), §6 (TaskStatus) | `llm_cycle.md` (TaskStatus — только ссылка на `docs_storage_models.md`) |
| part2 (целиком, `llm/common.py`) | `llm/core.md §2` |
| part3 (целиком, `GroqClient` и вспомогательные классы) | `llm/groq_client.md` |
| part3 §0 (диаграмма места в цикле), финальная «итоговая карта» | `llm_cycle.md` |

Эти три файла больше не нужны — их можно удалить после того, как новые
файлы окажутся в репозитории.
