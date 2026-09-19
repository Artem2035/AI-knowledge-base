"""
Контроль лимитов LLM-провайдера (в MVP — только Groq). Два независимых
уровня защиты:

1. MAX_LLM_CALLS_PER_TASK — жёсткий потолок на одну задачу (защита от
   "тихого" разрастания числа вызовов), не связан напрямую с реальным
   лимитом API.
2. Локальный soft-throttle по RPM (ожидание перед вызовом, если недавно
   было много запросов) — снижает шанс словить 429, но НЕ является
   источником истины. Источник истины — реальный ответ API (см.
   llm/groq_client.py).

Ничего здесь не "чинит" исчерпание лимита переключением на платный tier —
только останавливает и сообщает.
"""
from __future__ import annotations

import time
from collections import deque

from storage.models import LLMCallLog, TaskStatus


class LLMFreeLimitReached(Exception):
    """Поднимается, когда бесплатный API провайдера возвращает устойчивую
    429 (после исчерпания retry) или когда достигнут дневной soft-limit."""


class LLMTaskBudgetExceeded(Exception):
    """Поднимается при достижении MAX_LLM_CALLS_PER_TASK."""


class LLMBudget:
    def __init__(self, max_calls_per_task: int, rpm_soft_limit: int, rpd_soft_limit: int):
        self.max_calls_per_task = max_calls_per_task
        self.rpm_soft_limit = rpm_soft_limit
        self.rpd_soft_limit = rpd_soft_limit
        self._minute_window: deque[float] = deque()
        self._day_count = 0

    def check_and_register_task_call(self, status: TaskStatus) -> None:
        if status.llm_calls_used >= self.max_calls_per_task:
            raise LLMTaskBudgetExceeded(
                f"Достигнут лимит {self.max_calls_per_task} вызовов LLM на задачу "
                f"(MAX_LLM_CALLS_PER_TASK). Задача остановлена, прогресс сохранён в staging."
            )

    def wait_if_needed_for_rpm(self) -> None:
        """Простой локальный throttle: не более rpm_soft_limit вызовов за
        скользящее окно 60 секунд. Best-effort, не заменяет реальный retry на 429."""
        now = time.monotonic()
        while self._minute_window and now - self._minute_window[0] > 60:
            self._minute_window.popleft()
        if len(self._minute_window) >= self.rpm_soft_limit:
            sleep_for = 60 - (now - self._minute_window[0]) + 0.5
            if sleep_for > 0:
                time.sleep(sleep_for)
        self._minute_window.append(time.monotonic())

    def check_rpd_soft_limit(self) -> None:
        if self._day_count >= self.rpd_soft_limit:
            raise LLMFreeLimitReached(
                f"Достигнут ориентировочный дневной лимит free tier "
                f"({self.rpd_soft_limit} запросов, GROQ_RPD_SOFT_LIMIT). "
                "Остановка без перехода на платный API. Попробуйте снова завтра "
                "(RPD сбрасывается по UTC согласно политике Groq)."
            )

    def register_call(self, status: TaskStatus, role: str, ok: bool, error: str | None = None) -> None:
        status.llm_calls_used += 1
        status.llm_calls_log.append(LLMCallLog(role=role, ok=ok, error=error))
        self._day_count += 1