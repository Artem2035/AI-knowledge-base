from __future__ import annotations

from llm.base import LLMClient
from llm.prompts.outline_planner import OUTLINE_PLANNER_SYSTEM_INSTRUCTION
from llm.schemas import OutlinePlanOutput
from storage.models import (
    DEFAULT_DOMAIN, DOMAIN_KINDS, OutlineNote, OutlineSubpoint, Plan, Task, TaskStatus, normalize_kind,
)


def build_plan(task: Task, client: LLMClient, status: TaskStatus) -> Plan:
    prompt = (
        f"Запрос пользователя: {task.raw_query!r}\n\n"
        "Построй план конспекта для полного изучения этой темы согласно роли."
    )
    output: OutlinePlanOutput = client.generate_structured(
        role="outline_planner",
        prompt=prompt,
        response_model=OutlinePlanOutput,
        status=status,
        system_instruction=OUTLINE_PLANNER_SYSTEM_INSTRUCTION,
    )
    # Нормализация в коде, а не доверие модели: домен вне списка -> technical,
    # kind не из набора домена -> other.
    domain = output.domain if output.domain in DOMAIN_KINDS else DEFAULT_DOMAIN
    return Plan(
        task_id=task.task_id,
        topic_title=output.topic_title,
        summary=output.summary,
        domain=domain,
        notes=[
            OutlineNote(
                title=n.title,
                rationale=n.rationale,
                subpoints=[
                    OutlineSubpoint(
                        heading=sp.heading, covers=sp.covers,
                        kind=normalize_kind(sp.kind, domain),
                    )
                    for sp in n.subpoints
                ],
            )
            for n in output.notes
        ],
    )