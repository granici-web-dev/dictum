# PLAN — фазы и текущая задача

Правило: фаза 1 — сквозной сценарий на самом простом входе. Аудио, ворота и ресёрч добавляются только когда текст уже доезжает до Trello.

## Фаза 1 — Сквозной сценарий (текст → Trello)  ← ТЕКУЩАЯ
- [x] P1-01 Скелет: uv, ruff, mypy strict, pytest, Makefile, docker-compose (postgres, redis), `.env.example` — проверить, что `make test` зелёный на заглушках
- [x] P1-02 `app/stages.py`: чтение промпта из `.claude/commands/`, вызов Anthropic, `StageResult` с файлами стадии (запись на диск — у вызывающего); тесты без сети
- [x] P1-03 `make run-text`: текст → intake → brief(batch, без диалога: «хватит» сразу) → prd → decompose; артефакты в `outputs/`
- [x] P1-04 Валидация `issues.json`: ids уникальны, `depends_on` существуют, нет циклов; тест на фикстуре
- [x] P1-05 `app/trello.py` (транспорт) и `app/publish.py` (отображение): списки по фазам, карточки, чеклист DoD, labels по area, отложенные скоупы в `Backlog`; идемпотентность по маркеру `dictum:<id>` на доске; тесты с замоканным httpx (respx)
- [ ] P1-06 Первый реальный прогон на `fixtures/idea_text.md` → доска Trello; скриншот в `docs/`
  - [x] Текст → `issues.json` вживую: 10 issues, 2 фазы, граф без циклов; валидатор чист. Стоило четыре вызова Sonnet, ~55 с на первые три стадии и ~112 с на prd с decompose
  - [ ] Публикация в Trello — код готов, ждёт прогона с `ALLOW_LIVE_API=true` на живой доске

## Фаза 2 — Telegram и ворота
- [ ] P2-01 Бот polling: /start, текст → run; статус-сообщения
- [ ] P2-02 Модели `runs/artifacts/gates` в Postgres + Alembic
- [ ] P2-03 Ворота с inline-кнопками после brief и decompose; «Править» → user_edit → перезапуск стадии
- [ ] P2-04 brief-диалог: вопрос → ответ → следующий вопрос; «Собирай»
- [ ] P2-05 `auto_approve` для демо

## Фаза 3 — Аудио
- [ ] P3-01 Голосовое `.oga` → ffmpeg → Whisper → transcript.md
- [ ] P3-02 Файл mp3/m4a: consent-кнопка, отказ без подтверждения
- [ ] P3-03 Нарезка длинного аудио по тишине, склейка
- [ ] P3-04 intake с несколькими идеями → candidates → выбор кнопкой

## Фаза 4 — Ресёрч и отделка
- [ ] P4-01 `/research` market-режим с веб-поиском; `kind: feature` → skip
- [ ] P4-02 Логи токенов и стоимости на run
- [ ] P4-03 Деплой на Hetzner, webhook вместо polling

## Как начинать сессию в Claude Code
Первое сообщение: «Прочитай CLAUDE.md, SPEC.md и PLAN.md. Текущая задача — P1-01. Предложи план на 5-7 шагов, не пиши код до моего ок.»
