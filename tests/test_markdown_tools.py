from __future__ import annotations

from storage.models import DraftNote, NoteAction
from tools.markdown_tools import build_note_path, render_markdown, slugify_filename


def test_slugify_keeps_cyrillic():
    assert slugify_filename("Retrieval-Augmented Generation") == "Retrieval-Augmented Generation"
    assert slugify_filename("Векторные базы данных") == "Векторные базы данных"


def test_slugify_strips_invalid_fs_chars():
    assert "?" not in slugify_filename("Что такое RAG?")
    assert ":" not in slugify_filename("RAG: обзор")


def test_build_note_path_with_folder():
    assert build_note_path("Знания/RAG", "Chunking") == "Знания/RAG/Chunking.md"


def test_build_note_path_without_folder():
    assert build_note_path("", "Chunking") == "Chunking.md"


def test_render_markdown_includes_frontmatter_and_body():
    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/Chunking.md",
        title="Chunking",
        frontmatter={"title": "Chunking", "created": "2026-09-04"},
        tags=["rag", "nlp"],
        body_md="Chunking — разбиение текста на фрагменты перед индексацией.",
        source_refs=["https://example.com/chunking"],
        links_out=["Эмбеддинги"],
    )
    rendered = render_markdown(draft)
    assert rendered.startswith("---\n")
    assert "title: Chunking" in rendered
    assert "tags:" in rendered
    assert "Chunking — разбиение" in rendered
    assert "[[Эмбеддинги]]" in rendered
    assert "sources:" in rendered
