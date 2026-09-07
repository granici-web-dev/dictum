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
- Text: без транскрипции, `duration: null`, `consent_confirmed` не пишется: записи нет.
- Frontmatter transcript.md: `source`, `duration`, `lang` (Whisper language detection); `consent_confirmed` — только для аудио.

### 3.2 Ворота
Сообщение в Telegram с кратким содержанием артефакта + inline-кнопки: **Дальше** / **Править** / **Стоп**. «Править» — пользователь пишет текст, он попадает как `user_edit` в следующий вызов той же стадии (стадия перезапускается с правкой). Таймаут ворот — 24 ч, после этого run → `stalled`.

`auto_approve=true` (флаг run) пропускает ворота, кроме consent. Только для демо. Стадия `brief` при этом получает `interactive: false` — тот же параметр, которым пользуется локальный прогон (§7.1).

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
`app/stages.py::run_stage(stage, inputs: dict[str, str], user_edit=None, history=None, params=None) -> StageResult`
- `inputs`: путь → содержимое; пути те же, что упоминают промпты (`inputs/transcript.md`, `outputs/brief.md`, `templates/prd_oneshot.md`).
- system = `.claude/commands/<stage>.md` + `templates/api_mode.md` (режим API: файлы приложены в тегах `<file path="...">`, сохранять нельзя, итоговые файлы выводятся в таких же тегах, ничего вне тегов).
- user = `<params>` (если есть) + `<file>`-блоки входов + `<user_edit>`, если есть. `history` — предыдущие ходы диалога (brief, P2-04), вставляются перед текущим сообщением.
- `params` — параметры запуска стадии (`mode`, `lang`, `interactive`): `brief` вызывается с `mode: batch` и `interactive: false`, `research` — с `mode` по `kind` брифа. Промпты написаны для Claude Code, где стадия ведёт диалог; в режиме API диалога нет, поэтому `templates/api_mode.md` запрещает вопросы и требует помечать неясное как `[уточнить: ...]`.
- `StageResult`: `files` (путь → содержимое из `<file>`-тегов ответа), `model`, `input_tokens`, `output_tokens`, `duration_ms`.
- Набор файлов проверяется: `intake` → ровно один из `inputs/idea.md` / `outputs/candidates.md`; `decompose` → оба `outputs/issues.json` и `outputs/issues.md`; остальные стадии → ровно один свой файл. Любой другой набор — `StageError`. Файлы на диск пишет вызывающий: CLI в `outputs/`, воркер в `runs/<run_id>/` с версиями (§4).
- Модель: `ANTHROPIC_MODEL`, для `decompose` — `ANTHROPIC_MODEL_DECOMPOSE`. Обе по умолчанию `claude-sonnet-5`; смена дефолта — правка этой строки и `.env.example`. Из ответа берутся text-блоки.
- Каждый вызов логируется: stage, model, токены, длительность; run_id добавляет воркер (фаза 2).
- Ретраи: `max_retries=2` SDK Anthropic (сетевые ошибки, 408/409/429/5xx, экспоненциальный backoff). Ответ со `stop_reason != end_turn`, с чужим набором файлов или с двумя блоками на один путь → `StageError` с полем `raw` (текст ответа), без повтора. Содержимое файла нормализуется: ровно один перевод строки в конце. Невалидный JSON от `decompose` → один повтор с сообщением об ошибке валидации через `user_edit` (P1-04).

### 7.1 Локальный прогон
`app/cli.py` (`make run-text TEXT="…"` или `--file путь`) собирает `inputs/transcript.md` из текста и гонит intake → brief → prd → decompose. Если у входа есть свой frontmatter, он снимается, а `lang` берётся оттуда: иначе на прогоне вышло бы два блока frontmatter и два разных языка. Приоритет языка: `--lang`, затем frontmatter входа, затем `DEFAULT_LANG`. `brief` получает `mode: batch`, `interactive: false`, `lang`. `/research` не вызывается: `outputs/research.md` пишется строкой «Ресёрч не запускался: локальный прогон через make run-text», потому что `/prd` просит этот файл на вход. Формулировку из `mode: skip` («по решению пользователя») здесь брать нельзя: никто ничего не решал, а артефакт уходит в `/prd` как данные. Коды возврата: `0` — дошёл до конца, `1` — стадия упала (сырой ответ модели сохраняется в `outputs/<stage>.raw.md`), `2` — intake вернул кандидатов и ждёт выбора человека, `64` — ошибка в аргументах запуска. Argparse по умолчанию отдаёт на такую ошибку `2`, поэтому парсер переопределён: иначе опечатка неотличима от ожидания решения.

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
