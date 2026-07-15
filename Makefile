.PHONY: dev db redis test clean scan qmd-generate

dev:
	uv run uvicorn gateway.main:app --reload --host 0.0.0.0 --port 8000

db:
	docker compose up -d db

db-stop:
	docker compose stop db

redis:
	docker compose up -d redis

redis-stop:
	docker compose stop redis

test:
	uv run pytest

scan:
	uv run python -m scanner.watcher

qmd-generate:
	uv run python -m core.qmd_generator

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache

docker-up:
	docker compose up -d

docker-down:
	docker compose down
