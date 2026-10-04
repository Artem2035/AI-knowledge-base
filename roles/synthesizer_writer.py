from pathlib import PurePosixPath

from storage.models import DraftNote, OutlineNote, Relationship
from tools.markdown_tools import normalize_link_title


def prepare_linking_context(plan_notes: list[OutlineNote]) -> tuple[list[str], dict[str, str]]:
    """Строит (1) отсортированный список имён, на которые можно ссылаться, и
    (2) карту normalized->canonical для снаппинга ссылок.

    Для action="create" каноническое имя — заголовок из плана. Для
    action="update" — имя существующего файла (stem из existing_path):
    ссылка [[...]] в Obsidian разрешается по имени файла, а не по
    плановому заголовку. Плановый заголовок update-заметки тоже
    снаппится к имени файла. Заголовки прочих заметок Vault сюда не
    подмешиваются."""
    title_map: dict[str, str] = {}
    for n in plan_notes:
        if n.action == "update" and n.existing_path:
            canonical = PurePosixPath(n.existing_path).stem
            title_map[normalize_link_title(n.title)] = canonical
            title_map[normalize_link_title(canonical)] = canonical
        else:
            title_map[normalize_link_title(n.title)] = n.title
    return sorted(set(title_map.values())), title_map


def build_relationships(drafts: list[DraftNote]) -> list[Relationship]:
    title_to_path = {d.title: d.path for d in drafts}
    # Ссылки на update-заметки идут по имени файла, а не по заголовку.
    for d in drafts:
        title_to_path.setdefault(PurePosixPath(d.path).stem, d.path)
    return [
        Relationship(from_note=d.path, to_note=title_to_path.get(t, t), link_type="wikilink")
        for d in drafts for t in d.links_out
    ]