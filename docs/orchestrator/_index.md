# `orchestrator/` — обзор пакета

> 1 экран: что лежит в `orchestrator/`, кто с кем связан, куда идти за
> деталями.

## Назначение пакета

Единственный НЕ-LLM слой, управляющий последовательностью шагов одной
задачи: нормализация запроса, вызов ролей в фиксированном порядке, контроль
бюджета LLM-вызовов, персистентность прогресса для resume. Сам пакет не
делает ни одного сетевого вызова к LLM напрямую — только вызывает роли
(`roles/*.py`), которые уже сами обращаются к `llm/` (`../llm/_index.md`).

## Файлы пакета → документы

| Файл кода | Документ |
|---|---|
| `orchestrator/__init__.py` | — (пустой файл-маркер) |
| `orchestrator/budget.py` | `budget.md` |
| `orchestrator/state_machine.py` | `state_machine.md` |

## Зависимости

```
cli/main.py ──► Orchestrator.__init__(settings) ──► llm/factory.py (создаёт клиентов)
                       │
                Orchestrator.run(...)
                       │
                       ├──► roles/*.py (вызывают LLM через llm/base.py::LLMClient)
                       ├──► vault/index.py (индексация, без LLM)
                       ├──► validation/__init__.py::run_validation (без LLM)
                       └──► staging/changeset.py::save_changeset (без LLM)

GroqClient.generate_structured(...) ──► LLMBudget (methods из budget.py)
```

## Кто вызывает

`cli/main.py` (`../cli/main.md`) — единственная точка входа, создающая
`Orchestrator` и вызывающая `run()`.

## Порядок чтения

1. `budget.md` — простые классы бюджета, читаются за минуту.
2. `state_machine.md` — сам `Orchestrator`, самый насыщенный файл пакета.

Сам процесс одного запроса (что вызывается в каком порядке, полная таблица
шагов) — не здесь, а в `../flows/llm_cycle.md`.
