from __future__ import annotations

from llm.base import LLMClient
from gemini.prompts.outline_planner import OUTLINE_PLANNER_SYSTEM_INSTRUCTION
from gemini.schemas import OutlinePlanOutput
from storage.models import OutlineNote, OutlineSubpoint, Plan, Task, TaskStatus


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
    return Plan(
        task_id=task.task_id,
        topic_title=output.topic_title,
        summary=output.summary,
        notes=[
            OutlineNote(
                title=n.title,
                rationale=n.rationale,
                subpoints=[OutlineSubpoint(heading=sp.heading, covers=sp.covers) for sp in n.subpoints],
            )
            for n in output.notes
        ],
    )