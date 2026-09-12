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
# Researcher (отбор источников) — используется только в RESEARCH_MODE=web
# ---------------------------------------------------------------------------


class SourceSelectionItem(BaseModel):
    index: int  # индекс в списке кандидатов, переданном в промпте (0-based)
    relevance_score: float = Field(ge=0.0, le=1.0)
    keep: bool


class SourceSelectionOutput(BaseModel):
    items: list[SourceSelectionItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Extractor + Critic (объединены) — используется только в RESEARCH_MODE=web
# ---------------------------------------------------------------------------


class EvidenceItem(BaseModel):
    concept: str
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    is_definition: bool = False
    # Индекс "единицы" (чанка ОДНОГО источника) в списке, переданном в
    # промпте текущего батча — позволяет батчить чанки НЕСКОЛЬКИХ разных
    # источников в одном вызове и корректно приписать каждое утверждение
    # его настоящему source_id в коде-обвязке (см. roles/extractor_critic.py
    # :: _to_evidence_list), а не доверять LLM формулировать source_id самой.
    unit_index: int = 0
    contradicts_indices: list[int] = Field(default_factory=list)
    critic_note: str = ""


class EvidenceBatchOutput(BaseModel):
    evidence: list[EvidenceItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Elaborator — используется только в RESEARCH_MODE=knowledge (см.
# roles/elaborator.py). В отличие от EvidenceItem/EvidenceBatchOutput выше,
# здесь нет unit_index/contradicts_indices — не с чем сверять противоречия
# между "единицами текста источника", т.к. текста источника нет: вход —
# сама подтема, а не чанк чужого текста.
# ---------------------------------------------------------------------------


class ElaborationItem(BaseModel):
    concept: str
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    is_definition: bool = False
    critic_note: str = ""
    # Индекс подтемы в списке, переданном в промпте текущего батча (0-based)
    # — позволяет раскрывать НЕСКОЛЬКО подтем одним вызовом и корректно
    # приписать каждый факт к его настоящей подтеме в коде-обвязке (см.
    # roles/elaborator.py), тот же принцип, что EvidenceItem.unit_index.
    subtopic_index: int = 0


class ElaborationOutput(BaseModel):
    evidence: list[ElaborationItem] = Field(default_factory=list)


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
    # Ориентир объёма заметки, определяется Planner-ом (шаг 1) на основе
    # количества и содержательности назначенных evidence_indices.
    # "standard" — дефолт и ожидаемое большинство заметок (~1 страница).
    # "long" — только если материала объективно больше (~2 страницы) — см.
    # PLAN_SYSTEM_INSTRUCTION в gemini/prompts/synthesizer_writer.py. НЕ
    # используется как жёсткая граница валидации
    # (validation/markdown_validator.py проверяет единый минимум в 3 абзаца
    # независимо от depth_hint) — только как целевой диапазон слов в
    # промпте write_note().
    depth_hint: Literal["standard", "long"] = "standard"


class NotePlanOutput(BaseModel):
    notes: list[NotePlanItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Critic — ревью уже написанной заметки (см. roles/critic.py). Работает
# ПОСЛЕ Writer, на готовом DraftNote, а не на сыром evidence — проверяет
# итоговый текст.
# ---------------------------------------------------------------------------


class CriticVerdictOutput(BaseModel):
    verdict: Literal["ok", "rewrite"]
    # Заполняется только при verdict="rewrite" — конкретные, adresуемые
    # замечания, которые Writer сможет учесть на повторном проходе (не общие
    # фразы вроде "сделай лучше", а конкретные пункты: "раздел X дублирует
    # раздел Y", "утверждение про Z не подкреплено ни одним evidence" и т.п.)
    feedback: str = ""