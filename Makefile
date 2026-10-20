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
	bash scripts/backup.sh
.PHONY: restore
restore:
	@test -n "$(FILE)" || { echo "usage: make restore FILE=backups/health-<stamp>.dump.age [DB=health_restore]" >&2; exit 2; }
	bash scripts/restore.sh "$(FILE)" $(DB)
