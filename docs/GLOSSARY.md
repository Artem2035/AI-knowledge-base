# Глоссарий

> Короткие определения терминов. Не заменяет reference: полный контракт смотрите в указанном документе.

## Данные между этапами

| Термин | Что это | Где контракт |
|---|---|---|
| **Task** | Исходный запрос пользователя, нормализованный кодом. | `storage/models.md §1.1` |
| **Plan** | Тема, домен, абзац-резюме и дерево заметок (`OutlineNote`) с подпунктами (`OutlineSubpoint`). | `storage/models.md §1.4` |
| **domain** | Область темы: `technical`, `humanities`, `life_management`. Выбирает инструкцию Elaborator и добавляет тег `domain/<домен>`. | `storage/models.md §1.0` |
| **kind** | Тип раздела (подпункта): допустимые значения зависят от домена, универсальный `other`. | `storage/models.md §1.0` |
| **SectionDraft** | Готовый markdown одного подпункта (результат Elaborator). | `storage/models.md §2.2` |
| **NoteAnnotation** | Теги, ссылки и резюме заметки (результат Annotator). | `storage/models.md §2.3` |
| **DraftNote** | Черновик заметки, собранный кодом из секций и аннотации; единица staging. | `storage/models.md §4.2` |
| **MOC** | «Map of Content»: заметка-оглавление темы, которую собирает код (`build_moc`), без LLM. | `tools/note_assembly.md §6` |
| **Relationship** | Связь (wikilink/tag/backlink) между заметками. | `storage/models.md §4.3` |
| **ValidationIssue / ValidationReport** | Проблема / итог детерминированной валидации. | `storage/models.md §5` |
| **StagingChangeset** | Что предлагается применить к Vault: creates/updates/deletes + validation. | `storage/models.md §5.3` |
| **TaskStatus** | Счётчик LLM-вызовов текущей сессии, передаётся по ссылке. | `storage/models.md §6.2` |
| **TaskCheckpoint** | Снимок состояния задачи на диске (v5), механизм `resume`. | `staging/checkpoint.md` |
| **Evidence** *(legacy)* | Прежний атомарный факт для цепочки Writer/Critic и web-режима. В активном пути не используется. | `storage/models.md §2.1` |

## Режимы и флаги

| Термин | Значение |
|---|---|
| **FREE_ONLY** | Только бесплатные провайдеры; `false` запрещён (`config/settings.md §12.2`). |
| **RESEARCH_MODE=knowledge** | Дефолт: без веб-поиска, Elaborator пишет разделы из знаний модели; заметки помечаются `source: model-knowledge`. |
| **RESEARCH_MODE=web** | Прежний путь с веб-поиском; заблокирован в `Orchestrator.run()`. |
| **MAX_LLM_CALLS_PER_TASK** | Жёсткий потолок вызовов LLM на сессию/попытку (`orchestrator/budget.md`). |
| **RPM / RPD / TPM / TPD** | Лимиты Groq Free Tier; TPM (8000) самый узкий (`llm/groq_client.md §5`). |
| **needs_check / unverified_sections** | Модель не уверена в деталях раздела: в заметке появляется callout «Требует проверки», в diff перечисляются такие разделы. |
| **needs_review** *(legacy)* | Прежняя пометка «критик не одобрил»; поле осталось для старых данных. |
| **enable_draft_merging** | Опциональное слияние готовых заметок без LLM; по умолчанию выключено. |

## Obsidian и Vault

| Термин | Значение |
|---|---|
| **frontmatter** | YAML в начале файла; только `title`, `tags`, `created`, `source` (`tools/markdown_tools.md`). |
| **wikilink** | Ссылка `[[Заголовок]]`; в Obsidian разрешается по имени файла. |
| **backlink** | Обратная ссылка. |
| **callout** | Блок `> [!type] Заголовок` (`abstract`, `warning`, `tip`, `info`). |
| **staging** | Слой между «LLM закончил» и «изменения попали в Vault». |
| **approve / commit** | `approve` — команда CLI с подтверждением; `commit` — запись в Vault (`staging/commit.md`). |

## Принципы

- **Внешние ключи не придумывает LLM.** Модель ссылается на элементы по индексу в промпте (`unit_index`, `index`), код подставляет настоящие id.
- **Что проверяет код, LLM не проверяет.** Валидация, сборка заметки, MOC, слияние, inline-ссылки: детерминированный код.
- **Ограниченные повторы.** Бюджет вызовов, один повтор за пропавшими разделами Elaborator, деление батча пополам не глубже двух раз; затем placeholder или пустая аннотация, а не бесконечный цикл.
- **Сбой второстепенного не роняет задачу.** Пустая аннотация и placeholder допустимы; они видны в diff и validation.