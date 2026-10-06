# `roles/` — обзор пакета

> **Namespace-предупреждение:** в коде также лежат `roles/researcher.py` и `roles/extractor_critic.py` (только `RESEARCH_MODE=web`, заблокирован в `Orchestrator.run()`). Они не документируются (`../CONTRIBUTING.md`). Роли Writer и Critic **удалены**: тело заметки собирает `../tools/note_assembly.md`.

## Назначение пакета

Роль — функция (иногда несколько): (1) собирает динамический промпт из готовых структурированных данных, (2) вызывает `client.generate_structured(role=…)`, (3) конвертирует ответ в объекты `../storage/models.md`. Роли не пишут в Vault и не хранят состояние между вызовами; ловят только исключения из `llm/common.py`.

## Файлы пакета → документы

| Файл кода | Документ | `role=` (тег) | Клиент |
|---|---|---|---|
| `roles/__init__.py` | — (пустой маркер) | — | — |
| `roles/outline_planner.py` | `outline_planner.md` | `"outline_planner"` | `self.llm` |
| `roles/elaborator.py` | `elaborator.md` | `"elaborator"` | `self.extraction_client` |
| `roles/vault_analyst.py` | `vault_analyst.md` | `"vault_dedup"`, `"folder_assignment"` | `self.llm` |
| `roles/annotator.py` | `annotator.md` | `"annotator"` | `self.llm` |
| `roles/synthesizer_writer.py` | `synthesizer_writer.md` | — (без LLM) | — |
| `roles/researcher.py`, `roles/extractor_critic.py` | — | не документируются | — |

## Зависимости

```
Task ─► outline_planner.build_plan ─► Plan (domain, kind, summary)
          │ (plan_confirm_cb)
          ▼
 elaborator.elaborate_outline ─► SectionDraft[]  (markdown разделов)
          ▼
 vault_analyst.resolve_notes_against_vault  (мутирует Plan.notes)
          ▼
 annotator.annotate_notes ─► NoteAnnotation[]  (теги, ссылки, abstract; только create)
          ▼
 tools/note_assembly: build_draft_note ─► (merge) ─► apply_inline_links ─► build_moc
          ▼
 synthesizer_writer.build_relationships ─► validation ─► staging
```

## Кто вызывает

`orchestrator/state_machine.py::Orchestrator.run()` (`../orchestrator/state_machine.md`).

## Порядок чтения

`outline_planner.md` → `elaborator.md` → `vault_analyst.md` → `annotator.md` → `synthesizer_writer.md`; сборка — `../tools/note_assembly.md`.