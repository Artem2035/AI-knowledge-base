# obsidian-ai-kb

Персональная AI-система управления знаниями вокруг Obsidian.
Один запрос на естественном языке → план исследования → бесплатный
веб-поиск → извлечение фактов → анализ вашего Vault (без дублей) →
предложенные Markdown-заметки в staging → **вы одобряете** → изменения
попадают в реальный Vault.

**Единственный LLM-провайдер: Gemini Free Tier.** Никаких платных API,
никаких автоматических fallback на billing. См. `FREE_ONLY=true` в `.env`.

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Отредактируйте `.env`:
- `LLM_PROVIDER` — `gemini` или `groq` (по умолчанию в `.env.example` стоит `groq`)
- Для Gemini: `GEMINI_API_KEY` — https://aistudio.google.com/apikey
- Для Groq: `GROQ_API_KEY` — https://console.groq.com/keys (регистрация email/Google/GitHub, карта не нужна, лимит free tier постоянный)
- `VAULT_PATH` — абсолютный путь к вашему Obsidian Vault

Оба провайдера реализуют одинаковый интерфейс (`llm/factory.py` — единственная
точка выбора), так что переключение — это изменение одной строки в `.env`,
без правок кода. Чтобы добавить третьего провайдера — реализуйте
`generate_structured(...)` в новом `llm/<provider>_client.py` по образцу
`llm/groq_client.py` и добавьте одну ветку в `llm/factory.py`.

Первый запуск скачает модель локальных эмбеддингов (~470MB,
`paraphrase-multilingual-MiniLM-L12-v2`) — это происходит один раз и не
связано с Gemini/лимитами; при отсутствии сети или сбое загрузки система
автоматически откатывается на keyword-only (BM25) поиск без падения.

## Использование

```bash
# Индексация Vault (можно и нужно) запускать отдельно в первый раз
python -m cli.main index

# Основной сценарий: один запрос -> workflow до staging
python -m cli.main ask "Изучи тему Retrieval-Augmented Generation"

# Посмотреть, какие задачи ждут вашего решения
python -m cli.main pending

# Применить (после ручной проверки!) изменения к реальному Vault
python -m cli.main approve <task_id>

python -m cli.main ask "Изучи тему RAG"
  → останавливается на лимите
  → "Продолжить: python -m cli.main resume <task_id>"

python -m cli.main resumable
  → список: этап, попыток, обработано источников, всего вызовов Gemini

python -m cli.main resume <task_id>
  → пропускает planning/researching/fetching (уже посчитано)
  → в extraction пропускает уже обработанные источники
  → продолжает с первого недоделанногоs
```

**Важно**: команда `ask` НИКОГДА не пишет в реальный Vault. Она доводит
задачу до этапа STAGING (см. `docs/architecture.md`) и печатает diff.
Запись происходит только по явной команде `approve` после вашего `y/N`
подтверждения.

## Что делает система при исчерпании бесплатного лимита Gemini

Останавливается. Явно. Без перехода на платный API. Прогресс задачи
(план, найденные источники, извлечённые факты) уже сохранён на диск на
каждом этапе — вы можете запустить `ask` с тем же запросом позже, когда
дневной лимит free tier сбросится (обычно в полночь по Pacific Time).

## Структура проекта

См. `docs/architecture.md` — полное архитектурное описание, включая:
- какие роли требуют вызова Gemini, а какие реализованы детерминированным
  кодом (retrieval, дедупликация выше порога, вся валидация);
- почему выбраны именно эти сторонние библиотеки (`ddgs`, `tenacity`,
  `python-frontmatter`) и что было сознательно НЕ взято из
  GitHub (`obsidian-smart-connections` — лицензионный риск, `gpt-researcher`
  и полноценные agent-фреймворки — избыточны для single-user CLI);
- workflow одного запроса шаг за шагом;
- ограничения MVP.

## Тесты

```bash
pip install -r requirements.txt   # включает pytest
pytest tests/ -v
```

Тесты валидации/staging/retrieval/индексации работают полностью локально,
без сети и без реальных вызовов Gemini (используется фикстура `test_vault`
и мок `google.genai`). Тесты, требующие реального Gemini API,
намеренно не включены в автоматический прогон — их нужно тратить осознанно.

## Безопасность по умолчанию

- `ALLOW_DELETE=false` — удаление заметок запрещено, пока явно не включено.
- Запись в реальный Vault — только после `approve`.
- `.env` с ключом API — в `.gitignore`, никогда не коммитится.
- `MAX_GEMINI_CALLS_PER_TASK` — жёсткий потолок вызовов на задачу.
