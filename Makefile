UV_RUN = uv run --frozen --extra dev

.PHONY: install lint test test-browser typecheck verify-config verify \
	migrate run compose-up compose-down compose-smoke backup restore
install:
	uv sync --frozen --extra dev
lint:
	$(UV_RUN) ruff check .
	$(UV_RUN) ruff format --check .
test:
	$(UV_RUN) pytest -q
test-browser:
	cd frontend && npm run test:browser
typecheck:
	$(UV_RUN) pyright app
verify-config:
	$(UV_RUN) python scripts/verify_configuration.py
# Lint + format check, configuration/link verifier, unit tests and pyright, in that
# order; stops at the first failure. Browser tests are separate (make test-browser).
verify: lint verify-config test typecheck
migrate:
	$(UV_RUN) alembic upgrade head
run:
	$(UV_RUN) uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --no-access-log
compose-up:
	docker compose up --build -d
compose-down:
	docker compose down
compose-smoke:
	$(UV_RUN) python scripts/verify_compose.py
backup:
	bash scripts/backup.sh
restore:
	@test -n "$(FILE)" || { echo "usage: make restore FILE=backups/health-<stamp>.dump.age [DB=health_restore]" >&2; exit 2; }
	bash scripts/restore.sh "$(FILE)" $(DB)
