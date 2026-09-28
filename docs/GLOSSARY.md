# Глоссарий

> Короткие определения терминов, которые встречаются во всей документации.
> Не заменяет reference — для полного контракта смотрите указанный документ.

## Данные, летающие между этапами

| Термин | Что это | Где полный контракт |
|---|---|---|
| **Task** | Исходный запрос пользователя, нормализованный кодом. | `storage/models.md §1.1` |
| **Plan** | Дерево заметок (`OutlineNote`) с подпунктами (`OutlineSubpoint`) — результат Planner-а. | `storage/models.md §1.4` |
| **Evidence** | Одно атомарное утверждение/факт, привязанное к разделу заметки плана. Единый формат для web- и knowledge-режимов. | `storage/models.md §3.1` |
| **ExistingNote** | Существующая заметка Vault, найденная локальным retrieval-ом как кандидат на дедупликацию. | `storage/models.md §4.1` |
| **DraftNote** | Черновик одной заметки — итог Writer+Critic, единица staging-changeset-а. | `storage/models.md §5.2` |
| **Relationship** | Одна связь (wikilink/tag/backlink) между заметками. | `storage/models.md §5.3` |
| **ValidationIssue / ValidationReport** | Одна найденная проблема / итог полного прогона детерминированной валидации. | `storage/models.md §6.1–6.2` |
| **StagingChangeset** | То, что предлагается применить к реальному Vault — creates/updates/deletes + validation. | `storage/models.md §6.3` |
| **TaskStatus** | Счётчик LLM-вызовов ТЕКУЩЕЙ сессии, передаётся по ссылке через весь цикл вызова. | `storage/models.md §7.2` |
| **TaskCheckpoint** | Персистентный снимок состояния задачи на диске — механизм `resume`. | `staging/checkpoint.md §2` |

## Режимы и флаги

| Термин | Значение |
|---|---|
| **FREE_ONLY** | Глобальный флаг: разрешены только бесплатные провайдеры. `False` запрещён в MVP (`config/settings.md §12.2`). |
| **RESEARCH_MODE=knowledge** | Дефолтный режим: без веб-поиска, Elaborator раскрывает подтемы из знаний модели. Заметки помечаются `frontmatter.source=model-knowledge`. |
| **RESEARCH_MODE=web** | Прежний путь (веб-поиск + Extractor/Critic по реальному тексту источников) — на момент этой документации явно заблокирован в `Orchestrator.run()`, миграция не завершена. |
| **MAX_LLM_CALLS_PER_TASK** | Жёсткий потолок вызовов LLM на одну сессию/попытку задачи (`orchestrator/budget.md §3`). |
| **RPM / RPD / TPM / TPD** | Requests/Tokens per Minute/Day — четыре независимых измерения лимита Groq Free Tier; TPM — самое узкое место (`llm/groq_client.md §5`). |
| **needs_review** | Пометка `DraftNote`: критик не одобрил заметку после исчерпания `max_critic_rounds` — сигнал пользователю в diff перед `approve`. |

## Obsidian/Vault-специфичные термины

| Термин | Значение |
|---|---|
| **frontmatter** | YAML-блок в начале Markdown-файла с метаданными (`title`/`tags`/`created`/`source`). Состав ограничен кодом, не промптом — `tools/markdown_tools.md §1`. |
| **wikilink** | Ссылка вида `[[Заголовок заметки]]` — способ Obsidian связывать заметки. |
| **backlink** | Обратная ссылка — заметка B является backlink для A, если A содержит wikilink на B. |
| **staging** | Промежуточный слой между "LLM закончил работу" и "изменения попали в реальный Vault" — пользователь видит diff и подтверждает (`approve`) до записи. |
| **approve / commit** | `approve` — команда CLI, показывающая diff и запрашивающая подтверждение; `commit` — сама запись в реальный Vault после подтверждения (`staging/commit.md`). |

## Общие принципы, на которые часто ссылаются reference-доки

- **"Foreign keys не должны придумываться LLM"** — модель ссылается на
  элементы по локальному индексу в промпте (`unit_index`, `item.index`), а
  код-обвязка роли сам подставляет реальные `id`/пути (`llm/schemas.md`,
  вступление).
- **"LLM не должен проверять то, что можно проверить кодом"** — вся
  детерминированная валидация (`validation/`) не использует LLM.
- **"Bounded retry, не цикл до ok"** — и `orchestrator/budget.py`
  (soft-throttle), и `roles/critic.py` (`max_critic_rounds`) используют
  жёсткий потолок попыток, а не бесконечный цикл до успеха.
