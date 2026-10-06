# Документация: `roles/synthesizer_writer.py` — контекст ссылок и связи

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Прежняя роль Writer (LLM-вызов на заметку) удалена: тело заметки собирает `../tools/note_assembly.md`. От модуля остались две чистые функции без LLM. Имя файла сохранено, чтобы имя документа совпадало с именем модуля (`../CONTRIBUTING.md`).

## 1. `prepare_linking_context(plan_notes: list[OutlineNote]) -> tuple[list[str], dict[str, str]]`

Строит (1) отсортированный список имён, на которые можно ссылаться, и (2) карту «нормализованное имя → каноническое» для снаппинга ссылок (`markdown_tools.snap_link`).
- `action="create"`: каноническое имя — заголовок из плана.
- `action="update"` с `existing_path`: каноническое имя — **stem файла** (`Знания/Файл в Vault.md` → `Файл в Vault`), потому что `[[ссылка]]` в Obsidian разрешается по имени файла. Плановый заголовок такой заметки тоже снаппится к stem.

Заголовки прочих заметок Vault сюда не подмешиваются. Используется в `Orchestrator.run()` (`title_map` для аннотатора). **Возвращает** `(known_titles без дублей, title_map)`. Не поднимает.

## 2. `build_relationships(drafts: list[DraftNote]) -> list[Relationship]`

Карта «заголовок → путь» по итоговым черновикам (для update дополнительно по stem файла), затем по `Relationship(from_note=путь, to_note=путь или заголовок как есть, link_type="wikilink")` на каждую ссылку каждого `links_out`. Вызывается **после** слияния и сборки MOC, иначе связи указывали бы на несуществующие пути или не видели рёбер MOC. Не поднимает.

Документация по `roles/synthesizer_writer.py` завершена. Далее — `annotator.md` или обзор `_index.md`.