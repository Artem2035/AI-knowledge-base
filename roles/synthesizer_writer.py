"""
Вспомогательные функции для связей между заметками плана. Генерация текста
заметки (Writer) удалена: тело заметки собирает код
(tools/note_assembly.py::build_draft_note) из готовых секций Elaborator.
"""
from __future__ import annotations

from storage.models import DraftNote, OutlineNote, Relationship
from tools.markdown_tools import normalize_link_title


def prepare_linking_context(plan_notes: list[OutlineNote]) -> tuple[list[str], dict[str, str]]:
    """Строит (1) отсортированный список заголовков всех заметок плана (их
    заголовки зафиксированы планом ещё до написания) и (2) карту
    normalized->canonical для снаппинга ссылок к точному написанию
    (дефисы и т.п.). Заголовки существующих заметок Vault сюда не
    подмешиваются."""
    title_map = {normalize_link_title(n.title): n.title for n in plan_notes}
    return sorted(set(title_map.values())), title_map


def build_relationships(drafts: list[DraftNote]) -> list[Relationship]:
    title_to_path = {d.title: d.path for d in drafts}
    return [
        Relationship(from_note=d.path, to_note=title_to_path.get(t, t), link_type="wikilink")
        for d in drafts for t in d.links_out
    ]