# idea2backlog

Идея → бриф → PRD → issues → Trello. Вход: голосовое в Telegram, текст или запись с диктофона (mp3, m4a, wav) после подтверждения, что все участники записи согласны.

Старт: `cp .env.example .env`, заполнить, `make up`, `uv sync`, `make db`, `make test`.
Для голосовых и записей нужны ffmpeg и ffprobe, ставятся вместе: `brew install ffmpeg` на macOS, `apt install ffmpeg` на Debian.
Локальный прогон без бота: `make run-text TEXT="Хочу, чтобы бот напоминал о дедлайнах"`.

Архитектура — `SPEC.md`. Что делать сейчас — `PLAN.md`. Правила — `CLAUDE.md`.
