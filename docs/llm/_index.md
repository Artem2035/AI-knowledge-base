# `llm/` — обзор пакета

> 1 экран: что лежит в `llm/`, кто с кем связан, куда идти за деталями.

## Назначение пакета

Весь код, отвечающий за общение с LLM-провайдером: общий контракт
(`base.py`), провайдер-нейтральные исключения и утилиты (`common.py`),
единственная точка выбора провайдера (`factory.py`), сам провайдер
(`groq_client.py`), схемы structured-output (`schemas.py`), статичные
системные инструкции ролей (`prompts/`), батчинг списков под токен-бюджет
(`chunking.py`).

## Файлы пакета → документы

| Файл кода | Документ | Примечание |
|---|---|---|
| `llm/base.py` | `core.md` | `LLMClient` Protocol |
| `llm/common.py` | `core.md` | Исключения, `repair_json`, оценка токенов |
| `llm/factory.py` | `core.md` | Выбор провайдера |
| `llm/groq_client.py` | `groq_client.md` | Единственный реализованный провайдер |
| `llm/schemas.py` | `schemas.md` | Pydantic-контракты structured-output |
| `llm/prompts/*.py` | `prompts.md` | **Покрывает весь подпакет** (`common.py`, `outline_planner.py`, `elaborator.py`, `annotator.py`, `vault_analyst.py`). Каждый файл мал (только тексты инструкций), отдельный документ на файл избыточен. |
| `llm/chunking.py` | `chunking.md` | Батчинг под токен-бюджет |
| `llm/router.py`, `llm/openrouter_client.py` | — | Намеренно не документируются (второй провайдер, вне текущего фокуса MVP) |

`core.md` объединяет три файла (`base.py`+`common.py`+`factory.py`) в один
документ — они малы по отдельности и составляют единый "фундамент", который
почти всегда читается вместе.

## Зависимости

```
roles/*.py  ──generate_structured(role=...)──►  llm/base.py::LLMClient (Protocol)
                                                       ▲
                                    llm/groq_client.py::GroqClient (реализация)
                                                       │
                              llm/common.py (исключения, оценка токенов)
                                                       │
                              llm/factory.py (создаёт GroqClient/RoleRoutingLLMClient)
                                                       ▲
                                     orchestrator/state_machine.py::Orchestrator.__init__

roles/*.py  ──render_item, response_model──►  llm/chunking.py::split_items_into_batches
                                                       │
                              llm/schemas.py (response_model), llm/prompts/*.py (system_instruction)
```

## Кто вызывает

`orchestrator/state_machine.py::Orchestrator.__init__` — единственное место,
которое напрямую вызывает `llm/factory.py` (см. `../orchestrator/state_machine.md`).
Все роли (`roles/*.py`) работают только с `LLMClient` Protocol, не зная,
какой конкретно класс за ним стоит.

## Порядок чтения

## Порядок чтения

1. `core.md`: контракт и общий фундамент.
2. `groq_client.md`: провайдер, лимитер, калибраторы, подсчёт токенов.
3. `schemas.md` → `prompts.md` → `chunking.md`: что роли передают в `generate_structured` и как это батчится.

Сам процесс одного вызова (что вызывается в каком порядке) — не здесь, а в
`../flows/llm_cycle.md`.
