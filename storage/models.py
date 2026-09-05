"""
Структурированные объекты, которыми обмениваются этапы workflow.

Правило проекта: между ролями никогда не передаётся длинный "сырой" текст —
только эти типизированные модели. Это даёт: (1) валидацию на границах между
ролями, (2) возможность сериализовать в JSON и персистить прогресс на диск
(resume после исчерпания лимита Gemini), (3) предсказуемый contract для
structured-output вызовов Gemini.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Task / Plan
# ---------------------------------------------------------------------------


class Task(BaseModel):
    task_id: str = Field(default_factory=_new_id)
    raw_query: str
    language: str = "ru"
    created_at: str = Field(default_factory=_now)


class Subtopic(BaseModel):
    title: str
    description: str = ""
    search_queries: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    task_id: str
    topic_title: str
    summary: str = ""
    subtopics: list[Subtopic] = Field(default_factory=list)
    suggested_source_types: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------


class SourceCandidate(BaseModel):
    source_id: str = Field(default_factory=_new_id)
    url: str
    title: str = ""
    snippet: str = ""
    subtopic: str = ""
    relevance_score: float = 0.0
    selected: bool = False
    fetched_text: str | None = None
    fetch_error: str | None = None


# ---------------------------------------------------------------------------
# Evidence (Extractor + Critic объединены)
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    evidence_id: str = Field(default_factory=_new_id)
    concept: str
    statement: str
    source_id: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    is_definition: bool = False
    contradicts: list[str] = Field(default_factory=list)  # evidence_id других утверждений
    critic_note: str = ""


# ---------------------------------------------------------------------------
# Vault Analyst
# ---------------------------------------------------------------------------


class ExistingNote(BaseModel):
    path: str
    title: str
    frontmatter: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    summary: str = ""
    content_hash: str = ""
    similarity_score: float = 0.0
    matched_concept: str = ""
    decision: Literal["reuse", "extend", "distinct", "unknown"] = "unknown"


# ---------------------------------------------------------------------------
# Synthesizer + Writer
# ---------------------------------------------------------------------------


class NoteAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"


class DraftNote(BaseModel):
    draft_id: str = Field(default_factory=_new_id)
    action: NoteAction
    # для CREATE — новый путь; для UPDATE — путь существующей заметки
    path: str
    title: str
    folder: str = ""
    frontmatter: dict = Field(default_factory=dict)
    body_md: str = ""
    tags: list[str] = Field(default_factory=list)
    links_out: list[str] = Field(default_factory=list)  # заголовки/пути других заметок
    source_refs: list[str] = Field(default_factory=list)  # url источников
    # для UPDATE: что именно добавляется (append-блок), чтобы не перезаписывать всё
    append_section: str | None = None


class Relationship(BaseModel):
    from_note: str
    to_note: str
    link_type: Literal["wikilink", "tag", "backlink"] = "wikilink"


# ---------------------------------------------------------------------------
# Validation / Staging
# ---------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    level: Literal["error", "warning"]
    code: str
    message: str
    draft_id: str | None = None


class ValidationReport(BaseModel):
    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.level == "warning"]


class StagingChangeset(BaseModel):
    task_id: str
    created_at: str = Field(default_factory=_now)
    creates: list[DraftNote] = Field(default_factory=list)
    updates: list[DraftNote] = Field(default_factory=list)
    deletes: list[str] = Field(default_factory=list)  # пути; в MVP по умолчанию всегда пусто
    relationships: list[Relationship] = Field(default_factory=list)
    validation: ValidationReport | None = None


# ---------------------------------------------------------------------------
# Orchestrator status / бюджет
# ---------------------------------------------------------------------------


class GeminiCallLog(BaseModel):
    role: str
    timestamp: str = Field(default_factory=_now)
    prompt_tokens_est: int = 0
    ok: bool = True
    error: str | None = None


class TaskStatus(BaseModel):
    task_id: str
    stage: str = "created"
    gemini_calls_used: int = 0
    gemini_calls_log: list[GeminiCallLog] = Field(default_factory=list)
    stopped_reason: str | None = None  # напр. "gemini_free_limit_reached"
    finished: bool = False
