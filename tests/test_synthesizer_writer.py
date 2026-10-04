from __future__ import annotations

from roles.synthesizer_writer import build_relationships, prepare_linking_context
from storage.models import DraftNote, NoteAction, OutlineNote


def test_prepare_linking_context_builds_sorted_titles_and_normalized_map():
    notes = [
        OutlineNote(title="Векторные базы данных"),
        OutlineNote(title="Эмбеддинги"),
    ]
    known_titles, title_map = prepare_linking_context(notes)

    assert known_titles == ["Векторные базы данных", "Эмбеддинги"]
    assert title_map["Эмбеддинги"] == "Эмбеддинги"


def test_build_relationships_maps_titles_to_paths():
    d1 = DraftNote(action=NoteAction.CREATE, path="Знания/A.md", title="A", links_out=["B"])
    d2 = DraftNote(action=NoteAction.CREATE, path="Знания/B.md", title="B", links_out=[])

    rels = build_relationships([d1, d2])

    assert len(rels) == 1
    assert rels[0].from_note == "Знания/A.md"
    assert rels[0].to_note == "Знания/B.md"
    assert rels[0].link_type == "wikilink"


def test_build_relationships_unresolved_title_falls_back_to_title_itself():
    d1 = DraftNote(action=NoteAction.CREATE, path="Знания/A.md", title="A", links_out=["Неизвестная тема"])

    rels = build_relationships([d1])

    assert rels[0].to_note == "Неизвестная тема"

def test_prepare_linking_context_uses_file_stem_for_update_notes():
    notes = [
        OutlineNote(title="Плановое имя", action="update", existing_path="Знания/Файл в Vault.md"),
        OutlineNote(title="Новая"),
    ]
    known_titles, title_map = prepare_linking_context(notes)

    assert known_titles == ["Новая", "Файл в Vault"]
    assert title_map["Плановое имя"] == "Файл в Vault"
    assert title_map["Файл в Vault"] == "Файл в Vault"


def test_build_relationships_resolves_link_to_update_note_by_file_stem():
    upd = DraftNote(action=NoteAction.UPDATE, path="Знания/Файл в Vault.md", title="Плановое имя",
                    append_section="## Р\n\nТекст")
    new = DraftNote(action=NoteAction.CREATE, path="Знания/Новая.md", title="Новая",
                    links_out=["Файл в Vault"])

    rels = build_relationships([upd, new])

    assert rels[0].from_note == "Знания/Новая.md"
    assert rels[0].to_note == "Знания/Файл в Vault.md"