from __future__ import annotations

from storage.models import DraftNote, NoteAction
from tools.markdown_tools import (
    build_note_path,
    render_markdown,
    slugify_filename,
    strip_wikilink_brackets,
)


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
    assert "created:" in rendered
    assert "Chunking — разбиение" in rendered
    assert "[[Эмбеддинги]]" in rendered
    assert "[[[[Эмбеддинги]]]]" not in rendered


def test_render_markdown_frontmatter_has_only_title_tags_created():
    """Регрессия: свойства заметки должны быть ограничены title/tags/created
    — 'sources' и любые прочие ключи не должны попадать в YAML frontmatter."""
    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/X.md",
        title="X",
        frontmatter={"created": "2026-09-04", "custom_field": "не должно попасть в YAML"},
        body_md="Достаточно длинный текст заметки для прохождения валидации.",
        source_refs=["https://example.com/a", "https://example.com/b"],
    )
    rendered = render_markdown(draft)
    frontmatter_block = rendered.split("---\n")[1]
    assert "sources" not in frontmatter_block
    assert "custom_field" not in frontmatter_block


def test_render_markdown_puts_sources_as_link_list_at_top_of_body():
    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/Y.md",
        title="Y",
        body_md="Основной текст заметки Y, достаточно длинный для теста.",
        source_refs=["https://example.com/a", "https://example.com/b"],
    )
    rendered = render_markdown(draft)
    assert "## Источники" in rendered
    assert "- https://example.com/a" in rendered
    assert "- https://example.com/b" in rendered
    # источники должны идти РАНЬШЕ основного текста заметки
    assert rendered.index("## Источники") < rendered.index("Основной текст заметки Y")


def test_render_markdown_no_sources_block_when_empty():
    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/Z.md",
        title="Z",
        body_md="Текст без источников.",
    )
    rendered = render_markdown(draft)
    assert "## Источники" not in rendered


def test_strip_wikilink_brackets_removes_all_nesting():
    assert strip_wikilink_brackets("[[[[Title]]]]") == "Title"
    assert strip_wikilink_brackets("[[Title]]") == "Title"
    assert strip_wikilink_brackets("Title") == "Title"
    assert strip_wikilink_brackets("") == ""


def test_render_markdown_links_out_never_double_wrapped():
    """Регрессия для бага '## Связанные заметки' -> [[[[Title]]]]:
    даже если модель кладёт в links_out уже обёрнутую ссылку, итоговый
    markdown должен содержать ровно одну пару скобок."""
    draft = DraftNote(
        action=NoteAction.CREATE,
        path="Знания/Y2.md",
        title="Y2",
        body_md="Текст заметки для теста ссылок, достаточно длинный.",
        links_out=[
            "[[Overview of Python environment isolation]]",
            "Обычный заголовок без скобок",
        ],
    )
    rendered = render_markdown(draft)
    assert "[[[[" not in rendered
    assert "]]]]" not in rendered
    assert "[[Overview of Python environment isolation]]" in rendered
    assert "[[Обычный заголовок без скобок]]" in rendered