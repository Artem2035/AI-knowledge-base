# Документация: `staging/commit.py` — единственная точка записи в реальный Vault

> Reference-док. Обзор пакета — `_index.md`.

**Назначение.** Единственная функция во всей системе, переносящая
staged-изменения в НАСТОЯЩИЙ Vault. Вызывается ТОЛЬКО после явного
`approve` пользователя в CLI (`../cli/_index.md`). Ничего не делает
"автоматически" в фоне.

## 1. `class CommitError(Exception)`

Общий класс ошибок commit-этапа: недостающая валидация, запрещённые
deletes.

## 2. `commit_changeset(changeset, vault_path, db, embedder, allow_delete, git_enabled, backup_dir) -> list[WriteResult]`

**Описание.** Порядок:
1. Защита: `changeset.validation is None or not changeset.validation.ok`
   → `CommitError` (невозможно при нормальном workflow — защитная мера).
2. Защита: `changeset.deletes` непуст при `allow_delete=False` →
   `CommitError`.
3. Создаёт `VaultWriter(vault_path, allow_delete)` (`../vault/writer.md`).
4. Для каждого `creates`: `writer.ensure_folder(draft.folder or "")`, затем
   `writer.write_draft(draft)`.
5. Для каждого `updates`: `writer.write_draft(draft, backup_dir=backup_dir)`
   (с бэкапом старой версии).
6. Для каждого пути в `deletes` (в MVP всегда пуст): `writer.delete_note(path)`.
7. Переиндексация: `VaultIndexer(db, vault_path, embedder).sync()` +
   `.resolve_wikilink_targets()` (`../vault/index.md`) — инкрементально,
   неизменённые файлы пропускаются по `content_hash`.
8. Если `git_enabled` — `_git_commit(...)` (§3).

**Параметры:**

| Имя | Тип | Назначение |
|---|---|---|
| `changeset` | `StagingChangeset` | Что применить. |
| `vault_path` | `Path` | Реальный Vault (`settings.vault_path`). |
| `db` | `VaultDB` | Индекс — для переиндексации после записи. |
| `embedder` | `LocalEmbedder \| None` | Для пересчёта эмбеддингов затронутых заметок. |
| `allow_delete` | `bool` | `settings.allow_delete` (дефолт `False`). |
| `git_enabled` | `bool` | `settings.git_enabled`. |
| `backup_dir` | `Path` | Snapshot "before" для `update` — обычно `<staging_task_dir>/backup_before_update`. |

**Возвращаемое значение:** `list[WriteResult]` (`../vault/writer.md`) — по
одной записи на каждый `create`/`update`.

**Исключения:**
- `CommitError` — не прошёл валидацию / запрещённые deletes.
- `FileExistsError` — `CREATE` на существующий файл.
- `FileNotFoundError` — `UPDATE` на несуществующий файл.
- `PermissionError` — `delete_note` при `allow_delete=False` (дублирует
  проверку выше — двойная защита).

## 3. `_git_commit(vault_path: Path, changeset: StagingChangeset) -> None` (приватная)

Опциональный `git add -A` + `git commit` внутри `vault_path`. Git —
вспомогательная функция, НЕ должна ломать основной workflow: любая
`subprocess.CalledProcessError` перехватывается и только логируется
(`logger.warning`).

**Исключения:** НЕ поднимает.

Документация по `staging/commit.py` завершена. Обзор — `_index.md`.
