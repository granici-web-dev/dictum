# SPEC — idea2backlog

Версия 0.1 · 07.09.2026 · статус: черновик для старта разработки

## 1. Что это
Внутренний инструмент команды: любой участник даёт идею или задание — голосовым в Telegram, файлом с диктофона или текстом — и оно проходит фиксированный workflow до карточек в бэклоге Trello. Человек участвует на воротах, а не переписывает документы.

Один пайплайн, три входа: **Telegram → intake → brief → research → prd → decompose → publish**.

## 2. Не-цели MVP
- Веб-интерфейс (бот — единственный UI на MVP)
- Мультипользовательская авторизация, роли
- Диаризация говорящих
- Рыночный ресёрч по умолчанию для фич (`kind: feature` → internal/skip)
- Интеграции кроме Trello

## 3. Стадии

| Стадия | Тип | Вход | Выход | Ворота после |
|---|---|---|---|---|
| ingest | код | Telegram voice/audio/text | `transcript.md` | — |
| intake | LLM `/intake` | transcript.md | `idea.md` или `candidates.md` | если candidates или confidence=low |
| brief | LLM `/brief` batch, диалог | idea.md + ответы | `brief.md` | всегда |
| research | LLM `/research` | brief.md | `research.md` | — |
| prd | LLM `/prd` | brief, research, template | `prd.md` | — |
| decompose | LLM `/decompose` | prd.md | `issues.json`, `issues.md` | всегда |
| publish | код | issues.json | карточки Trello, `publish.json` | — |

### 3.1 ingest
- Telegram voice (`.oga`) → ffmpeg → mp3 16 kHz mono.
- Audio file (mp3/m4a/wav): проверка `consent_confirmed`, иначе отказ с пояснением.
- Файлы > 24 МБ или > 20 мин: нарезка по тишине (ffmpeg `silencedetect`), куски ≤ 10 мин с перекрытием 5 с, транскрипция каждого, склейка по перекрытию.
- Text: без транскрипции, `duration: null`.
- Frontmatter transcript.md: `source`, `duration`, `lang` (Whisper language detection), `consent_confirmed`.

### 3.2 Ворота
Сообщение в Telegram с кратким содержанием артефакта + inline-кнопки: **Дальше** / **Править** / **Стоп**. «Править» — пользователь пишет текст, он попадает как `user_edit` в следующий вызов той же стадии (стадия перезапускается с правкой). Таймаут ворот — 24 ч, после этого run → `stalled`.

`auto_approve=true` (флаг run) пропускает ворота, кроме consent. Только для демо.

### 3.3 brief-диалог в Telegram
`/brief` в режиме batch задаёт вопросы по одному; ответы пользователя — обычные сообщения. Стадия хранит историю в `runs.brief_dialog` (JSON). «хватит» / кнопка «Собирай» завершает.

## 4. Данные

`runs`: id, chat_id, source, lang, kind, status (`ingesting|intake|awaiting_gate|brief|research|prd|decompose|awaiting_publish|published|failed|stalled`), consent_confirmed, auto_approve, created_at, updated_at.
`artifacts`: run_id, stage, path, version, created_at — каждая перезапись стадии = новая версия, старые не удаляются.
`gates`: run_id, stage, decision (`next|edit|stop`), user_edit, decided_at.
`publish_log`: run_id, issue_id, trello_card_id, url.

Файлы прогона лежат в `runs/<run_id>/` (S3-совместимо позже), `inputs/` и `outputs/` — только для локального запуска через `make run-text`.

## 5. Контракты артефактов
- `transcript.md`, `idea.md`, `candidates.md` — см. `/intake`
- `brief.md` — frontmatter `name`, `kind`, `lang`, `open_questions` — см. `/brief`
- `prd.md` — таблица «Скоп MVP» с `ID`, приоритет, `Зависит от`, критерий готовности — см. `/prd`
- `issues.json` — схема в `/decompose`; дополнительно к схеме: `estimate: "S|M|L"` (S ≤ 2 ч, M ≤ полдня, L ≤ день) и `test_hint` (одна строка, как проверить) — поля взяты из практики Task Master
- `publish.json` — маппинг issue_id → card_id/url

## 6. Trello
- Одна доска на команду, `TRELLO_BOARD_ID` в .env.
- Списки: `Backlog`, по одному на фазу `Phase 1 — <title>` … создаются при первой публикации, если нет.
- Карточка = issue: title, description + DoD как чеклист, label по `area`, `scope_id` в description (Trello custom fields платные — не использовать).
- Зависимости: строка «Depends on: I-003, I-007» в конце description + attachment-ссылка на карточку.
- Идемпотентность: повторный publish того же `issues.json` не создаёт дублей (`publish_log`).

## 7. Вызов LLM-стадий
`app/stages.py::run_stage(name, inputs: dict, user_edit: str | None) -> str`
- system = содержимое `.claude/commands/<name>.md`
- user = конкатенация входных артефактов + `user_edit`
- Модель: по умолчанию Claude Sonnet; для `decompose` допустим более сильный (флаг в .env).
- Все вызовы логируются с run_id, стадией, токенами, длительностью.
- Ретрай ×2 на сетевые ошибки; невалидный JSON от `decompose` → один повтор с сообщением об ошибке валидации.

## 8. Нефункциональное
- EU-only: API-эндпоинты Anthropic/OpenAI с EU-регионом, где доступны; хостинг Hetzner.
- Аудио удаляется после успешной транскрипции, если `KEEP_AUDIO=false` (по умолчанию).
- Лимиты: ≤ 3 параллельных run на chat_id, ≤ 60 мин аудио на run.

## 9. Открытые вопросы
- [уточнить: шаблон `templates/prd_oneshot.md` — заменить черновик на реальный]
- [уточнить: отдельный репозиторий или платформа GastroBeleg — сейчас отдельный]
- [уточнить: язык интерфейса бота по умолчанию — de / en / ru]
- [уточнить: нужен ли `kind: feature` → internal research на MVP или всегда skip]

## 10. Референсы
- claude-task-master (28k★) — схема задач и паттерн «оценить сложность → расширить»
- ai-prd-generator — уточняющий цикл до порога уверенности
- Backlog.md — markdown в git как источник истины, трекер как витрина
