.PHONY: install lint test migrate run compose-up compose-down compose-smoke backup
install:
	uv sync --frozen --extra dev
lint:
	uv run --frozen --extra dev ruff check .
	uv run --frozen --extra dev ruff format --check .
test:
	uv run --frozen --extra dev pytest -q
migrate:
	alembic upgrade head
run:
	uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
compose-up:
	docker compose up --build -d
compose-down:
	docker compose down
compose-smoke:
	python scripts/verify_compose.py
backup:
	docker compose exec -T postgres pg_dump -U health -d health -Fc > backups/health.dump
