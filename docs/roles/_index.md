# `roles/` — обзор пакета

> 1 экран: что лежит в `roles/`, кто с кем связан, куда идти за деталями.
> **Namespace-предупреждение:** пакет кода содержит также
> `roles/researcher.py` и `roles/extractor_critic.py` (активны только в
> `RESEARCH_MODE=web`, который сейчас заблокирован в
> `Orchestrator.run()`, `../orchestrator/state_machine.md §4`) — по решению,
> зафиксированному в `../CONTRIBUTING.md`, они НЕ документируются в этом
> заходе. Ниже — только активный путь `RESEARCH_MODE=knowledge`.

## Назначение пакета

Роли — НЕ отдельные "агенты" со своим циклом: каждая роль — это функция
(иногда несколько), которая (1) собирает динамический промпт из уже
готовых структурированных данных (`../storage/models.md`), (2) вызывает
`client.generate_structured(role=..., ...)` (`../flows/llm_cycle.md §5`),
(3) конвертирует Pydantic-ответ модели (`../llm/schemas.md`) обратно в
объекты `storage/models.py`. Роли никогда не пишут в реальный Vault и не
хранят состояние между вызовами — всё состояние передаёт `Orchestrator`.

## Файлы пакета → документы (активный путь)

| Файл кода | Документ | `role=` (тег) |
|---|---|---|
| `roles/__init__.py` | — (пустой файл-маркер) | — |
| `roles/outline_planner.py` | `outline_planner.md` | `"outline_planner"` |
| `roles/elaborator.py` | `elaborator.md` | `"elaborator"` |
| `roles/vault_analyst.py` | `vault_analyst.md` | `"vault_dedup"`, `"folder_assignment"` |
| `roles/synthesizer_writer.py` | `synthesizer_writer.md` | `"synthesizer_write"` |
| `roles/critic.py` | `critic.md` | `"critic"` |
| `roles/researcher.py`, `roles/extractor_critic.py` | — | не документируются (web-режим) |

## Зависимости

```
Task ──► outline_planner.build_plan ──► Plan
                                            │
                            (пользователь утверждает план — cli/plan_editor.py)
                                            │
                                            ▼
                         elaborator.elaborate_outline ──► Evidence[]
                                            │
                                            ▼
              vault_analyst.resolve_notes_against_vault (мутирует Plan.notes)
                                            │
                                            ▼
                    critic.run_critic_cycle ──► synthesizer_writer.write_note
                                            │        (Writer пишет, Critic ревьюит,
                                            │         bounded retry — max_critic_rounds)
                                            ▼
                                       DraftNote[]
```

Каждая роль вызывает `client.generate_structured(...)` через
`llm/base.py::LLMClient` (`../llm/core.md §1`), используя схемы из
`../llm/schemas.md` и статичные инструкции из `../llm/prompts.md`.

## Кто вызывает

`orchestrator/state_machine.py::Orchestrator.run()`
(`../orchestrator/state_machine.md §4`) — вызывает роли строго в порядке
диаграммы выше, персистит прогресс после каждого шага.

## Порядок чтения

1. `outline_planner.md` — первый шаг, самый дешёвый по бюджету.
2. `elaborator.md` — самый частый по числу вызовов шаг.
3. `vault_analyst.md` — дедупликация против существующего Vault.
4. `synthesizer_writer.md` → `critic.md` — читать вместе: `critic.md`
   оркестрирует цикл "Writer → Critic → (при rewrite) Writer снова" и
   постоянно ссылается на `write_note` из `synthesizer_writer.md`.
