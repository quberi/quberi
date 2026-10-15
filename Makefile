# Quberi developer entrypoints.
#
# Every target is idempotent and safe to re-run. Long-running commands
# (compose up, watchers) do not belong here.

SHELL := /bin/bash
COMPOSE := docker compose --env-file .env

# The API container talks to Postgres over the docker network as
# ``postgres:5432``; when we invoke Alembic from the host we need
# ``127.0.0.1:5432`` instead. This env var overrides the compose DSN.
HOST_POSTGRES_DSN ?= postgresql://quberi:change-me-local-only@127.0.0.1:5432/quberi

.PHONY: help
help: ## Show this help
	@awk 'BEGIN { FS = ":.*?## " } /^[a-zA-Z_-]+:.*?## / { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# ---------------------------------------------------------------------------
# Compose lifecycle
# ---------------------------------------------------------------------------
.PHONY: up
up: ## Build & start the full stack (postgres, redis, qdrant, minio, api, web)
	$(COMPOSE) --profile app up -d --build

.PHONY: down
down: ## Stop and remove containers (keeps volumes)
	$(COMPOSE) --profile app down

.PHONY: nuke
nuke: ## Stop stack AND drop volumes — destroys all local data
	$(COMPOSE) --profile app down -v

.PHONY: ps
ps: ## Show container status
	$(COMPOSE) --profile app ps

.PHONY: logs
logs: ## Tail api logs
	$(COMPOSE) --profile app logs -f api

# ---------------------------------------------------------------------------
# Quality gates
# ---------------------------------------------------------------------------
.PHONY: lint
lint: ## Run ruff over the API source tree
	apps/api/.venv/bin/ruff check apps/api/src apps/api/alembic apps/api/tests

.PHONY: check-drift
check-drift: ## Fail if ORM models diverge from the latest migration head
	set -a; . ./.env; set +a; QUBERI_POSTGRES_DSN='$(HOST_POSTGRES_DSN)' apps/api/scripts/check-drift.sh

.PHONY: check-migrations
check-migrations: ## Round-trip: upgrade head → downgrade base → upgrade head
	set -a; . ./.env; set +a; QUBERI_POSTGRES_DSN='$(HOST_POSTGRES_DSN)' apps/api/scripts/check-migrations.sh

.PHONY: test-api
test-api: ## Run the API test suite against a dedicated quberi_test database
	cd apps/api && .venv/bin/pytest

.PHONY: test-web
test-web: ## Run the web test suite (vitest)
	cd apps/web && npm run test

.PHONY: test
test: test-api test-web ## Run every test suite

.PHONY: check
check: lint check-drift test ## Pre-commit gate: lint + drift + tests

# ---------------------------------------------------------------------------
# Alembic
# ---------------------------------------------------------------------------
.PHONY: migrate
migrate: ## Apply migrations to head (host-side)
	set -a; . ./.env; set +a; cd apps/api && QUBERI_POSTGRES_DSN='$(HOST_POSTGRES_DSN)' .venv/bin/alembic upgrade head

.PHONY: revision
revision: ## Generate a new migration. Usage: make revision m="short slug"
	@if [ -z "$$m" ]; then echo "usage: make revision m=\"short slug\""; exit 1; fi
	set -a; . ./.env; set +a; cd apps/api && QUBERI_POSTGRES_DSN='$(HOST_POSTGRES_DSN)' .venv/bin/alembic revision --autogenerate -m "$$m"
