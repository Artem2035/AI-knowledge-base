"""
Контракты structured-output для каждого Gemini-вызова.

Намеренно отделены от storage/models.py: Gemini не должен сам придумывать
task_id/path/source_id (это foreign keys, которыми управляет код) — вместо
этого модель ссылается на индексы элементов, переданных ей в промпте, а
код-обвязка роли уже сама подставляет реальные id/пути. Это снижает риск
галлюцинаций в структурных полях.

Простые типы (str/float/bool/list) вместо произвольных dict — потому что
JSON Schema, которую Gemini использует для structured output, работает
надёжнее с фиксированной формой полей.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class SubtopicOutput(BaseModel):
    title: str
    description: str = ""
    search_queries: list[str] = Field(default_factory=list)


class PlanOutput(BaseModel):
    topic_title: str
    summary: str = ""
    subtopics: list[SubtopicOutput] = Field(default_factory=list)
    suggested_source_types: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Researcher (отбор источников)
# ---------------------------------------------------------------------------


class SourceSelectionItem(BaseModel):
    index: int  # индекс в списке кандидатов, переданном в промпте (0-based)
    relevance_score: float = Field(ge=0.0, le=1.0)
    keep: bool


class SourceSelectionOutput(BaseModel):
    items: list[SourceSelectionItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Extractor + Critic (объединены)
# ---------------------------------------------------------------------------


class EvidenceItem(BaseModel):
    concept: str
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    is_definition: bool = False
    contradicts_indices: list[int] = Field(default_factory=list)
    critic_note: str = ""


class EvidenceBatchOutput(BaseModel):
    evidence: list[EvidenceItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Vault dedup (только "серая зона")
# ---------------------------------------------------------------------------


class DedupDecisionOutput(BaseModel):
    same_concept: bool
    decision: Literal["reuse", "extend", "distinct"]
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Synthesizer + Writer (объединены)
# ---------------------------------------------------------------------------


class FrontmatterField(BaseModel):
    key: str
    value: str


class DraftNoteOutput(BaseModel):
    action: Literal["create", "update"]
    existing_path: str = ""  # обязателен при action="update", должен совпасть с одним из переданных ExistingNote
    title: str
    folder: str = ""
    frontmatter_extra: list[FrontmatterField] = Field(default_factory=list)
    body_md: str = ""
    tags: list[str] = Field(default_factory=list)
    links_out: list[str] = Field(default_factory=list)
    append_section: str = ""  # если непусто и action="update" — добавляем блок, не переписываем всё


class SynthesisOutput(BaseModel):
    notes: list[DraftNoteOutput] = Field(default_factory=list)

# ---------------------------------------------------------------------------
# Synthesizer — Note Planner (шаг 1 map-reduce)
# ---------------------------------------------------------------------------

class NotePlanItem(BaseModel):
    title: str
    action: Literal["create", "update"]
    existing_path: str = ""  # обязателен при action="update"
    folder: str = ""
    # Индексы в списке evidence, переданном в промпте (0-based) — не даём
    # модели самой формулировать concept-строки для сопоставления, чтобы
    # избежать рассинхрона между шагом планирования и шагом записи.
    evidence_indices: list[int] = Field(default_factory=list)
    tags_hint: list[str] = Field(default_factory=list)


class NotePlanOutput(BaseModel):
    notes: list[NotePlanItem] = Field(default_factory=list)