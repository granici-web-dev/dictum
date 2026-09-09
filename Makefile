.PHONY: up down bot test run-text clean-board clean-runs db
up:
	docker compose up -d
down:
	docker compose down
bot:
	uv run python -m app.bot
test:
	uv run ruff check . && uv run mypy app tests && uv run pytest -q
run-text:
	uv run python -m app.cli $(if $(FILE),--file $(FILE),"$(TEXT)") $(ARGS)
# Уборка между прогонами репетиции. Обе цели только руками и ни от чего не зависят: попасть в них
# из up, test или run-text нельзя, иначе однажды прогон сотрёт доску или артефакты за собой.
clean-board:
	uv run python -m scripts.clear_board $(if $(RUN),--run $(RUN),--all)
clean-runs:
	@echo "Каталоги прогонов, которые уйдут:"
	@ls -1 runs 2>/dev/null || echo "  (пусто)"
	@printf "Введите «да», чтобы удалить их с диска: " && read answer && [ "$$answer" = "да" ] \
		&& find runs -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
		&& echo "Готово, runs/ пуст." || echo "Отменено, ничего не удалено."
db:
	uv run alembic upgrade head
