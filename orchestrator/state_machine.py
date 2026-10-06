"""
Orchestrator — не LLM. Чистый Python state machine, который:
- понимает пользовательский запрос (нормализует Task);
- вызывает роли в фиксированной последовательности;
- прокидывает структурированные данные между этапами;
- контролирует бюджет LLM-вызовов и останавливается при исчерпании
  лимита (без fallback на платный tier);
- ПЕРСИСТИТ ПРОГРЕСС ПОСЛЕ КАЖДОГО ШАГА (см. orchestrator/checkpoint.py),
  чтобы задачу можно было продолжить позже командой `resume <task_id>`,
  не пересчитывая уже сделанную работу;
- никогда сам не пишет в реальный Vault (это staging/commit.py, только
  после явного approve).

RESEARCH_MODE (см. config/settings.py):
- "web" — прежний путь: Researcher (веб-поиск + fetch) → Extractor/Critic
  извлекает evidence из реального текста источников.
- "knowledge" (дефолт) — Researcher/Extractor полностью пропускаются;
  Elaborator (roles/elaborator.py) генерирует evidence по каждой подтеме
  плана из знаний модели, без сетевого I/O. Заметки в этом режиме
  помечаются frontmatter.source="model-knowledge" (см.
  roles/synthesizer_writer.py::_to_draft_note) — это ЧЕРНОВОЙ конспект без
  проверяемых источников, а не исследование.

ВАЖНО про бюджет при resume: MAX_LLM_CALLS_PER_TASK — это лимит на
ОДНУ СЕССИЮ/ПОПЫТКУ (status.llm_calls_used обнуляется в начале каждого
вызова run(), в т.ч. при resume), а не на задачу за всё её время жизни.
Иначе после однократного исчерпания лимита задачу нельзя было бы
продолжить никогда. Накопительный расход по всем попыткам хранится
отдельно в TaskCheckpoint.total_llm_calls_used — только для отчёта
пользователю.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings
from llm.factory import budget_limits_for_provider, create_llm_client, extraction_budget_limits, \
    create_extraction_llm_client
from orchestrator.budget import LLMBudget, LLMFreeLimitReached, LLMTaskBudgetExceeded

from retrieval.search import VaultSearcher
from roles import annotator, elaborator, outline_planner, synthesizer_writer, vault_analyst
from staging.changeset import save_changeset
from staging.checkpoint import save_checkpoint, delete_checkpoint, TaskCheckpoint, load_checkpoint
from staging.draft_merge import fix_links_after_merge
from storage.models import StagingChangeset, Task, TaskStatus
from tools.dedup import try_create_embedder
from tools.markdown_tools import slugify_filename
from tools.note_assembly import build_draft_note, apply_inline_links, build_moc
from validation import run_validation
from vault.db import VaultDB
from vault.index import VaultIndexer

logger = logging.getLogger(__name__)

# Пометка frontmatter.source (см. tools/markdown_tools.py) для заметок,
# написанных в RESEARCH_MODE=knowledge — без внешних проверяемых
# источников. Совпадает с roles.elaborator.MODEL_KNOWLEDGE_SOURCE_ID по
# смыслу, но это отдельная константа: одна — маркер source_id у Evidence
# (внутренний), другая — человекочитаемое значение во frontmatter заметки
# (видимое пользователю в Obsidian).
KNOWLEDGE_MODE_FRONTMATTER_SOURCE = "model-knowledge"


class OrchestratorStopped(Exception):
    """Управляемая остановка задачи (лимит бюджета/LLM, либо ошибка
    resume — например, чекпоинт не найден). Прогресс сохранён (кроме
    случая, когда чекпоинт сам оказался нечитаем)."""

    def __init__(self, message: str, task_id: str):
        super().__init__(message)
        self.task_id = task_id


@dataclass
class RunResult:
    task_id: str
    changeset: StagingChangeset | None
    status: TaskStatus
    stopped: bool
    message: str


class Orchestrator:
    def __init__(self, settings: Settings):
        settings.validate_free_only()
        settings.ensure_dirs()
        self.settings = settings

        self.db = VaultDB(settings.db_path)
        self.embedder = try_create_embedder(settings.embedding_model, settings.use_local_embeddings)

        rpm_limit, rpd_limit = budget_limits_for_provider(settings)
        self.budget = LLMBudget(
            max_calls_per_task=settings.max_llm_calls_per_task,
            rpm_soft_limit=rpm_limit,
            rpd_soft_limit=rpd_limit,
        )
        # roles/* работают с этим клиентом только через generate_structured(...),
        # конкретный тип провайдера им не важен (см. llm/base.py::LLMClient).
        self.llm = create_llm_client(settings, self.budget)
        # Отдельный клиент+бюджет для extraction/elaboration (см.
        # llm/factory.py) — на Groq использует другую модель
        # (openai/gpt-oss-120b с другим TPM-профилем) — используется как в
        # web-режиме (extractor_critic), так и в knowledge-режиме
        # (elaborator) — в обоих случаях это самый частый по числу вызовов
        # шаг. Оба бюджета читают и пишут в один и тот же
        # status.llm_calls_used (передаётся при каждом вызове run()), так
        # что MAX_LLM_CALLS_PER_TASK продолжает работать как ОБЩИЙ потолок
        # на задачу независимо от того, какой из двух клиентов расходует
        # вызовы.
        ext_rpm, ext_rpd = extraction_budget_limits(settings)
        self.extraction_budget = LLMBudget(
            max_calls_per_task=settings.max_llm_calls_per_task,
            rpm_soft_limit=ext_rpm,
            rpd_soft_limit=ext_rpd,
        )
        # v4-A1 (см. docs/groq_token_budget.md §4, llm/factory.py::
        # create_extraction_llm_client): передаём уже созданный self.llm —
        # если модель extraction-клиента совпадает с основной (дефолт
        # проекта), фабрика переиспользует TokenRateLimiter/
        # TokenEstimateCalibrator основного клиента вместо создания
        # независимого — устраняет риск, что два клиента, физически деля
        # один TPM Groq, резервируют токены "не зная" друг о друге.
        self.extraction_client = create_extraction_llm_client(
            settings, self.extraction_budget, primary_client=self.llm
        )

    def close(self) -> None:
        self.db.close()

    # -- публичный API ------------------------------------------------

    def sync_vault_index(self) -> dict:
        """Шаг 8 ТЗ: анализ существующего Vault. Инкрементально, без LLM."""
        indexer = VaultIndexer(db=self.db, vault_path=self.settings.vault_path, embedder=self.embedder)
        stats = indexer.sync()
        indexer.resolve_wikilink_targets()
        return stats

    def run(
        self,
        raw_query: str | None = None,
        *,
        resume_task_id: str | None = None,
        progress_cb=None,
        plan_confirm_cb=None,  # Callable[[Plan], bool] | None
        merge_confirm_cb=None,  # Callable[[list[DraftNote]], list[DraftNote]] | None
    ) -> RunResult:
        """
        Выполняет workflow до этапа STAGING. Два режима:

        - Новая задача: run(raw_query="...").
        - Продолжение остановленной задачи: run(resume_task_id="...").
          Уже завершённые шаги (согласно чекпоинту) пропускаются, бюджет
          LLM-вызовов открывается заново на эту сессию.

        Порядок шагов: planning → утверждение плана → elaboration (готовый
        markdown разделов) → vault analysis → аннотатор (теги, ссылки,
        резюме) → детерминированная сборка DraftNote → merge (опционально)
        → relationships → validation → staging.

        DraftNote в чекпоинте не хранятся: сборка из sections и annotations
        дёшева и детерминирована, поэтому выполняется заново на каждом запуске.

        plan_confirm_cb: вызывается РОВНО ОДИН РАЗ на задачу (флаг
        checkpoint.plan_approved) сразу после построения/загрузки Plan —
        ДО самого дорогого этапа (elaboration). Если вернул False —
        контролируемая остановка, ни один «дорогой» вызов не потрачен.
        Если не передан — план утверждается автоматически.

        merge_confirm_cb: вызывается после сборки заметок, ДО валидации, и
        только при settings.enable_draft_merging=True.
        """

        def report(stage: str) -> None:
            if progress_cb:
                progress_cb(stage)

        checkpoint, task, status, base_total_calls = self._load_or_create_state(
            raw_query=raw_query, resume_task_id=resume_task_id, report=report,
        )

        if self.settings.research_mode == "web":
            raise OrchestratorStopped(
                "RESEARCH_MODE=web ещё не мигрирован на новую структуру плана "
                "(OutlineNote/subpoints) после перехода на Outline Planner. "
                "Используйте RESEARCH_MODE=knowledge (текущий дефолт).",
                task_id=task.task_id,
            )

        def persist(stage_label: str) -> None:
            """Сохраняет чекпоинт немедленно после успешного завершения
            шага. Вызывается часто (после каждого батча elaboration и
            аннотатора) — это и есть механизм resume."""
            checkpoint.last_completed_stage = stage_label
            checkpoint.status = status
            checkpoint.total_llm_calls_used = base_total_calls + status.llm_calls_used
            save_checkpoint(self.settings.checkpoint_dir, checkpoint)

        try:
            report("Анализ Vault (индексация)…")
            self.sync_vault_index()  # чистый код, без LLM — безопасно повторять всегда

            # -- Planning ------------------------------------------------
            if checkpoint.plan is None:
                report(f"Планирование конспекта ({self.settings.llm_provider})…")
                status.stage = "planning"
                plan = outline_planner.build_plan(task, self.llm, status)
                checkpoint.plan = plan
                persist("planned")
            else:
                plan = checkpoint.plan
                report("План уже построен (из чекпоинта) — пропускаем.")

            # -- Plan approval (inline confirmation) ----------------------
            if not checkpoint.plan_approved:
                approved = plan_confirm_cb(plan) if plan_confirm_cb is not None else True
                if not approved:
                    status.stage = "stopped"
                    status.stopped_reason = "План не подтверждён пользователем."
                    persist("planned")  # last_completed_stage остаётся "planned"
                    return RunResult(
                        task_id=task.task_id,
                        changeset=None,
                        status=status,
                        stopped=True,
                        message=(
                            "План конспекта не утверждён. Задача остановлена ДО "
                            "траты бюджета Groq на elaboration — сам план "
                            "(1 дешёвый вызов) сохранён.\n"
                            f"Продолжить (план будет показан снова): "
                            f"python -m cli.main resume {task.task_id}"
                        ),
                    )
                checkpoint.plan_approved = True
                persist("plan_approved")
            else:
                report("План уже утверждён (из чекпоинта) — пропускаем подтверждение.")

            # -- Elaboration: готовый markdown каждого подпункта -----------
            if not checkpoint.elaboration_done:
                report(f"Написание разделов ({self.settings.llm_provider})…")
                status.stage = "elaborating"

                def _on_elaboration_batch(subpoint_ids, new_sections):
                    checkpoint.sections.extend(new_sections)
                    checkpoint.elaborated_subpoint_ids.extend(subpoint_ids)
                    persist("elaborating")

                elaborator.elaborate_outline(
                    plan, self.extraction_client, status,
                    already_done_subpoint_ids=set(checkpoint.elaborated_subpoint_ids),
                    on_batch_done=_on_elaboration_batch,
                    max_subpoints_per_batch=self.settings.max_subpoints_per_generation_batch,
                )
                checkpoint.elaboration_done = True
                persist("elaboration_done")
            else:
                report("Разделы уже написаны (из чекпоинта) — пропускаем.")
            sections = checkpoint.sections

            # -- Vault analysis --------------------------------------------
            if not checkpoint.vault_analysis_done:
                report(
                    "Анализ существующих заметок Vault "
                    "(локально + LLM для спорных случаев)…"
                )
                status.stage = "vault_analysis"
                existing_folders = self.db.get_distinct_folders()
                topic_folder = f"{self.settings.default_notes_folder}/{slugify_filename(plan.topic_title)}".strip("/")
                searcher = VaultSearcher(self.db, embedder=self.embedder)
                vault_analyst.resolve_notes_against_vault(
                    plan, searcher, self.llm, status,
                    existing_folders=existing_folders, default_folder=topic_folder,
                    high_threshold=self.settings.dedup_high_threshold,
                    low_threshold=self.settings.dedup_low_threshold,
                )
                checkpoint.plan = plan
                checkpoint.vault_analysis_done = True
                persist("vault_analysis_done")
            else:
                report("Анализ Vault уже выполнен (из чекпоинта) — пропускаем.")

            # -- Annotation: теги, ссылки, резюме (только action="create") --
            if not checkpoint.annotation_done:
                report(f"Аннотирование заметок ({self.settings.llm_provider})…")
                status.stage = "annotating"
                _, title_map = synthesizer_writer.prepare_linking_context(plan.notes)

                def _on_annotation_batch(note_ids, new_annotations):
                    checkpoint.annotations.extend(new_annotations)
                    checkpoint.annotated_note_ids.extend(note_ids)
                    persist("annotating")

                annotator.annotate_notes(
                    plan, sections, title_map, self.llm, status,
                    already_done_note_ids=set(checkpoint.annotated_note_ids),
                    on_batch_done=_on_annotation_batch,
                )
                checkpoint.annotation_done = True
                persist("annotation_done")
            else:
                report("Аннотации уже готовы (из чекпоинта) — пропускаем.")

            # -- Сборка DraftNote (без LLM, каждый раз заново) --------------
            report("Сборка заметок…")
            status.stage = "assembling"
            mark_source = (
                KNOWLEDGE_MODE_FRONTMATTER_SOURCE
                if self.settings.research_mode == "knowledge"
                else None
            )
            annotations_by_note = {a.note_id: a for a in checkpoint.annotations}
            drafts = [
                build_draft_note(
                    note, sections, annotations_by_note.get(note.note_id),
                    domain=plan.domain, mark_source=mark_source,
                )
                for note in plan.notes
            ]

            # -- Объединение готовых заметок (опционально, БЕЗ LLM) --------
            if self.settings.enable_draft_merging and merge_confirm_cb is not None:
                report("Объединение заметок (если выбрано пользователем)…")
                drafts = merge_confirm_cb(drafts)
                # ссылки других заметок на исходные заголовки -> на объединённую
                drafts = fix_links_after_merge(drafts)

            # -- Inline-ссылки по links_out (после слияния и чистки ссылок) --
            drafts = [apply_inline_links(d) for d in drafts]

            # -- MOC: только если осталось >=2 create-заметок ---------------
            moc = build_moc(
                plan, drafts, annotations_by_note,
                domain=plan.domain,
                default_folder=f"{self.settings.default_notes_folder}/{slugify_filename(plan.topic_title)}".strip("/"),
                existing_paths=self.db.get_all_paths(),
            )
            if moc is not None:
                drafts.insert(0, moc)

            # Связи считаются ПОСЛЕ merge и MOC: иначе они ссылались бы на
            # пути заметок, которых после объединения уже нет, или не видели
            # бы рёбер MOC.
            relationships = synthesizer_writer.build_relationships(drafts)

            # -- Validation + Staging (без LLM, всегда выполняются заново,
            # т.к. дёшевы и должны учитывать текущее состояние db/Vault) ----
            report("Валидация предложенных изменений…")
            status.stage = "validating"
            creates = [d for d in drafts if d.action.value == "create"]
            updates = [d for d in drafts if d.action.value == "update"]
            changeset = StagingChangeset(
                task_id=task.task_id,
                creates=creates,
                updates=updates,
                deletes=[],
                relationships=relationships,
            )
            changeset.validation = run_validation(changeset, self.db, self.settings.allow_delete, plan=plan)
            report("Сохранение в staging (Vault пока не тронут)…")
            status.stage = "staged"
            save_changeset(self.settings.staging_dir, changeset)

            # Задача доведена до staging — чекпоинт больше не нужен:
            # дальнейшее состояние живёт в StagingChangeset.
            delete_checkpoint(self.settings.checkpoint_dir, task.task_id)

            status.finished = True
            return RunResult(
                task_id=task.task_id,
                changeset=changeset,
                status=status,
                stopped=False,
                message="Изменения подготовлены и ждут вашего approve.",
            )

        except (LLMFreeLimitReached, LLMTaskBudgetExceeded) as exc:
            status.stage = "stopped"
            status.stopped_reason = str(exc)
            persist(checkpoint.last_completed_stage)
            logger.warning("Задача %s остановлена: %s", task.task_id, exc)
            return RunResult(
                task_id=task.task_id,
                changeset=None,
                status=status,
                stopped=True,
                message=(
                    f"{exc}\n\n"
                    f"Прогресс сохранён (шаг: {checkpoint.last_completed_stage}). "
                    f"Продолжить: python -m cli.main resume {task.task_id}"
                ),
            )

    # -- внутреннее ------------------------------------------------------

    def _load_or_create_state(
        self,
        *,
        raw_query: str | None,
        resume_task_id: str | None,
        report,
    ) -> tuple[TaskCheckpoint, Task, TaskStatus, int]:
        if resume_task_id:
            checkpoint = load_checkpoint(self.settings.checkpoint_dir, resume_task_id)
            if checkpoint is None:
                raise OrchestratorStopped(
                    f"Чекпоинт для задачи {resume_task_id!r} не найден, повреждён "
                    "или относится к несовместимой версии — продолжить нельзя. "
                    "Запустите задачу заново командой 'ask'.",
                    task_id=resume_task_id,
                )
            # ВАЖНО: предполагается, что storage.models.Task допускает
            # явную передачу task_id (обычное pydantic-поле, а не
            # read-only/frozen с default_factory=uuid). Если это не так —
            # замените на task = Task(raw_query=...); task.task_id = checkpoint.task_id
            # (сработает, если модель не frozen), либо явно откройте
            # task_id как параметр конструктора в storage/models.py.
            task = Task(
                task_id=checkpoint.task_id,
                raw_query=checkpoint.raw_query,
                language=checkpoint.language,
            )
            status = TaskStatus(task_id=task.task_id, stage=checkpoint.last_completed_stage)
            base_total_calls = checkpoint.total_llm_calls_used
            report(
                f"Продолжаем задачу {task.task_id} "
                f"(последний завершённый шаг: «{checkpoint.last_completed_stage}», "
                f"уже потрачено LLM-вызовов всего: {base_total_calls})…"
            )
            return checkpoint, task, status, base_total_calls

        if not raw_query:
            raise ValueError("raw_query обязателен для новой задачи (resume_task_id не передан)")

        task = Task(raw_query=raw_query, language=self.settings.language)
        status = TaskStatus(task_id=task.task_id, stage="started")
        checkpoint = TaskCheckpoint(
            task_id=task.task_id,
            raw_query=task.raw_query,
            language=task.language,
            status=status,
        )
        save_checkpoint(self.settings.checkpoint_dir, checkpoint)
        return checkpoint, task, status, 0