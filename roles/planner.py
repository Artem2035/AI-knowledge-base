from __future__ import annotations

from llm.base import LLMClient
from gemini.schemas import PlanOutput
from storage.models import Plan, Subtopic, Task, TaskStatus

SYSTEM_INSTRUCTION = (
    "Ты — Planner в системе управления знаниями. Твоя задача: по запросу "
    "пользователя построить структуру исследования темы. Отвечай на русском "
    "языке, если пользователь пишет на русском. Не выдумывай узкоспециальные "
    "факты — только структура: подтемы, ключевые концепции, какие типы "
    "источников имеет смысл искать (например: официальная документация, "
    "научные статьи, авторитетные технические блоги, обзорные статьи). "
    "Для каждой подтемы дай 1-3 конкретных поисковых запроса на русском и/или "
    "английском (для технических тем английские запросы часто дают более "
    "качественные источники — это нормально)."
)


def build_plan(task: Task, client: LLMClient, status: TaskStatus) -> Plan:
    prompt = (
        f"Запрос пользователя: {task.raw_query!r}\n\n"
        "Построй план исследования этой темы: 5-9 подтем, покрывающих тему "
        "достаточно полно для создания базы заметок в Obsidian, но без "
        "избыточного дробления. Для каждой подтемы укажи короткое описание "
        "и 1-3 поисковых запроса."
    )
    output: PlanOutput = client.generate_structured(
        role="planner",
        prompt=prompt,
        response_model=PlanOutput,
        status=status,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    return Plan(
        task_id=task.task_id,
        topic_title=output.topic_title,
        summary=output.summary,
        subtopics=[
            Subtopic(
                title=s.title,
                description=s.description,
                search_queries=s.search_queries,
            )
            for s in output.subtopics
        ],
        suggested_source_types=output.suggested_source_types,
    )
