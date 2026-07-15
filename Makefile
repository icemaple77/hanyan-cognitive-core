.PHONY: dev db redis test clean scan qmd-generate context

dev:
	uv run uvicorn gateway.main:app --reload --host 0.0.0.0 --port 8000

db:
	docker compose up -d db

db-stop:
	docker compose stop db

redis:
	docker compose up -d redis

test:
	uv run pytest

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache

docker-up:
	docker compose up -d

docker-down:
	docker compose down

scan:
	uv run python -m scanner.watcher

qmd-generate:
	uv run python -m core.qmd_generator

context:
	uv run python -c "from gateway.api.context_routes import router; print('Context API route ready: POST /api/v1/context')"
