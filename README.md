# idea2backlog

Идея → бриф → PRD → issues → Trello. Вход: голосовое в Telegram или текст; аудиофайл с диктофона — позже, к нему нужно согласие на запись (P3-02).

Старт: `cp .env.example .env`, заполнить, `make up`, `uv sync`, `make db`, `make test`.
Для голосовых нужен ffmpeg: `brew install ffmpeg` на macOS, `apt install ffmpeg` на Debian.
Локальный прогон без бота: `make run-text TEXT="Хочу, чтобы бот напоминал о дедлайнах"`.

Архитектура — `SPEC.md`. Что делать сейчас — `PLAN.md`. Правила — `CLAUDE.md`.
