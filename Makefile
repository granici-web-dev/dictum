.PHONY: up down bot worker test run-text
up:
	docker compose up -d
down:
	docker compose down
bot:
	uv run python -m app.bot
worker:
	uv run celery -A app.worker worker -l info
test:
	uv run ruff check . && uv run mypy app tests && uv run pytest -q
run-text:
	uv run python -m app.cli $(if $(FILE),--file $(FILE),"$(TEXT)")
