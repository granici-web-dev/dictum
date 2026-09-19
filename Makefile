.PHONY: up down bot test run-text meeting deliver clean-board clean-runs db
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
# Запись встречи с ноутбука: спрашивает согласие и цену в терминале, разбор уходит в чат.
# RUN=<id> продолжает сорванный прогон по тому, что он успел записать.
meeting:
	uv run python -m app.cli $(if $(FILE),--meeting $(FILE),) $(if $(RUN),--run $(RUN),) $(if $(CHAT),--chat $(CHAT),)
deliver:
	uv run python -m app.cli --deliver $(RUN) $(if $(CHAT),--chat $(CHAT),)
# Уборка своей доски и артефактов после проверочных прогонов. Обе цели только руками и ни от
# чего не зависят: попасть в них из up, test или run-text нельзя, иначе однажды прогон сотрёт
# доску или артефакты за собой.
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
