"""
Детерминированное автоисправление конфликтов путей перед валидацией —
без LLM. Исправляет только то, что однозначно решается суффиксом
« (2)», « (3)»…: create на уже занятый в Vault путь и два create на один
путь внутри changeset. Всё остальное (update на несуществующий файл,
пустое тело и т.п.) остаётся ошибкой для validation.
"""
from __future__ import annotations

from storage.models import DraftNote, NoteAction, ValidationIssue
from tools.markdown_tools import build_note_path, normalize_link_title

_MAX_SUFFIX = 99


def _folder_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _free_candidate(
    draft: DraftNote, taken_paths: set[str], taken_titles: set[str]
) -> tuple[str, str] | None:
    """Первый свободный (заголовок, путь) с суффиксом; None, если не нашли."""
    folder = _folder_of(draft.path)
    for n in range(2, _MAX_SUFFIX + 1):
        title = f"{draft.title} ({n})"
        path = build_note_path(folder, title)
        if path not in taken_paths and title not in taken_titles:
            return title, path
    return None


def autofix_drafts(
    drafts: list[DraftNote], existing_paths: set[str]
) -> tuple[list[DraftNote], list[ValidationIssue]]:
    """Возвращает (новые черновики, предупреждения об исправлениях).
    Исходный список не мутируется. Ссылки links_out перенаправляются на
    новый заголовок только при коллизии с файлом Vault."""
    result = list(drafts)
    issues: list[ValidationIssue] = []
    taken_paths = set(existing_paths) | {d.path for d in drafts}
    taken_titles = {d.title for d in drafts}
    seen_paths: set[str] = set()
    redirects: dict[str, str] = {}  # нормализованный старый заголовок -> новый

    for i, d in enumerate(drafts):
        if d.action != NoteAction.CREATE:
            continue
        collides_existing = d.path in existing_paths
        duplicate = d.path in seen_paths
        if not (collides_existing or duplicate):
            seen_paths.add(d.path)
            continue

        candidate = _free_candidate(d, taken_paths, taken_titles)
        if candidate is None:
            continue  # оставляем как есть: validation выдаст ошибку
        new_title, new_path = candidate
        taken_paths.add(new_path)
        taken_titles.add(new_title)
        seen_paths.add(new_path)
        if collides_existing:
            redirects.setdefault(normalize_link_title(d.title), new_title)

        result[i] = d.model_copy(update={"title": new_title, "path": new_path})
        reason = "файл уже существует в Vault" if collides_existing else "путь занят другой заметкой этой задачи"
        issues.append(ValidationIssue(
            level="warning", code="path_autofixed",
            message=f"Заметка «{d.title}»: {reason} ({d.path}) — переименована в «{new_title}» ({new_path}).",
            draft_id=d.draft_id,
        ))

    if redirects:
        for i, d in enumerate(result):
            own = normalize_link_title(d.title)
            new_links: list[str] = []
            for link in d.links_out:
                target = redirects.get(normalize_link_title(link), link)
                if normalize_link_title(target) == own or target in new_links:
                    continue
                new_links.append(target)
            if new_links != d.links_out:
                result[i] = d.model_copy(update={"links_out": new_links})

    return result, issues