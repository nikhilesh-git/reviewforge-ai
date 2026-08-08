# =============================================================================
# GitHub PR Code Reviewer — Makefile
# Provides convenient commands for development, testing, and operations.
# =============================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash
.PHONY: help up down restart logs shell test lint format migrate tunnel clean \
        build push infra-up infra-down gateway-test worker-test

# ─── Colours ──────────────────────────────────────────────────────────────────
BOLD  := \033[1m
GREEN := \033[0;32m
CYAN  := \033[0;36m
RESET := \033[0m

# ─── Variables ────────────────────────────────────────────────────────────────
COMPOSE         := docker compose
COMPOSE_DEV     := docker compose -f docker-compose.yml -f docker-compose.override.yml
ENV_FILE        := .env
SERVICE         ?= gateway
TAG             ?= latest
REGISTRY        ?= ghcr.io/your-org/github-pr-reviewer

# ─── Help ─────────────────────────────────────────────────────────────────────
help: ## Show this help message
	@echo ""
	@echo "$(BOLD)GitHub PR Code Reviewer$(RESET)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(CYAN)%-25s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# ─── Docker Compose ───────────────────────────────────────────────────────────
up: ## Start all services in detached mode (production config)
	@echo "$(GREEN)Starting all services...$(RESET)"
	$(COMPOSE) --env-file $(ENV_FILE) up -d

dev: ## Start all services with hot-reload (dev config)
	@echo "$(GREEN)Starting services in dev mode...$(RESET)"
	$(COMPOSE_DEV) --env-file $(ENV_FILE) up -d

down: ## Stop all services
	@echo "$(GREEN)Stopping all services...$(RESET)"
	$(COMPOSE) down

down-clean: ## Stop all services and remove volumes
	@echo "$(GREEN)Stopping services and removing volumes...$(RESET)"
	$(COMPOSE) down -v --remove-orphans

restart: ## Restart a specific service (SERVICE=gateway)
	$(COMPOSE) restart $(SERVICE)

build: ## Build all Docker images
	$(COMPOSE) build --no-cache

build-service: ## Build a specific service (SERVICE=gateway)
	$(COMPOSE) build --no-cache $(SERVICE)

logs: ## Follow logs for a service (SERVICE=gateway)
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

logs-all: ## Follow logs for all services
	$(COMPOSE) logs -f --tail=50

shell: ## Open shell in a service container (SERVICE=gateway)
	$(COMPOSE) exec $(SERVICE) /bin/bash

ps: ## Show running services
	$(COMPOSE) ps

# ─── Infra Only ───────────────────────────────────────────────────────────────
infra-up: ## Start only infrastructure services (postgres, redis, qdrant, etc.)
	$(COMPOSE) up -d postgres redis qdrant prometheus grafana langfuse-postgres langfuse-server

infra-down: ## Stop infrastructure services
	$(COMPOSE) stop postgres redis qdrant prometheus grafana langfuse-postgres langfuse-server

# ─── Testing ──────────────────────────────────────────────────────────────────
test: ## Run all tests
	@echo "$(GREEN)Running all tests...$(RESET)"
	pytest -v --tb=short

test-service: ## Run tests for a specific service (SERVICE=gateway)
	@echo "$(GREEN)Running tests for $(SERVICE)...$(RESET)"
	pytest services/$(SERVICE)/tests/ -v --tb=short --cov=services/$(SERVICE)/app

test-unit: ## Run only unit tests (no external dependencies)
	pytest -v -m unit --tb=short

test-integration: ## Run integration tests (requires running services)
	pytest -v -m integration --tb=short

test-cov: ## Run all tests with coverage report
	pytest --cov=services --cov=shared --cov-report=html --cov-report=term-missing

gateway-test: ## Run gateway tests in Docker
	$(COMPOSE) run --rm gateway pytest /app/tests/ -v

# ─── Code Quality ─────────────────────────────────────────────────────────────
lint: ## Run ruff linter across all Python files
	@echo "$(GREEN)Running ruff linter...$(RESET)"
	ruff check .

lint-fix: ## Run ruff and auto-fix issues
	ruff check --fix .

format: ## Format all Python code with black
	@echo "$(GREEN)Formatting with black...$(RESET)"
	black .

format-check: ## Check formatting without modifying files
	black --check .

type-check: ## Run mypy type checking
	mypy services/ shared/ --ignore-missing-imports

quality: lint format-check type-check ## Run all code quality checks

# ─── Database Migrations ──────────────────────────────────────────────────────
migrate: ## Run pending alembic migrations
	@echo "$(GREEN)Running database migrations...$(RESET)"
	$(COMPOSE) run --rm gateway alembic upgrade head

migrate-rollback: ## Roll back last migration
	$(COMPOSE) run --rm gateway alembic downgrade -1

migrate-history: ## Show migration history
	$(COMPOSE) run --rm gateway alembic history

migrate-new: ## Create a new migration (MSG="description")
	$(COMPOSE) run --rm gateway alembic revision --autogenerate -m "$(MSG)"

migrate-local: ## Run migrations against local DB (requires DATABASE_URL in env)
	alembic upgrade head

# ─── Local Development ────────────────────────────────────────────────────────
tunnel: ## Start ngrok tunnel to expose gateway (requires ngrok installed)
	ngrok http 8000

env-setup: ## Copy .env.example to .env (only if .env doesn't exist)
	@test -f .env || (cp .env.example .env && echo "$(GREEN)Created .env from .env.example$(RESET)")

install-dev: ## Install all dev dependencies locally (for IDE support)
	pip install -e shared/[dev]
	pip install -e services/gateway/[dev]
	pip install -e services/worker/[dev]
	pip install -e services/reviewer/[dev]
	pip install -e services/learner/[dev]

pre-commit: ## Run pre-commit checks (lint + format + test-unit)
	$(MAKE) lint
	$(MAKE) format-check
	$(MAKE) test-unit

# ─── Production ───────────────────────────────────────────────────────────────
push: ## Build and push all images to registry (TAG=latest)
	$(COMPOSE) build
	docker tag github-pr-reviewer-gateway $(REGISTRY)/gateway:$(TAG)
	docker tag github-pr-reviewer-worker $(REGISTRY)/worker:$(TAG)
	docker tag github-pr-reviewer-reviewer $(REGISTRY)/reviewer:$(TAG)
	docker tag github-pr-reviewer-learner $(REGISTRY)/learner:$(TAG)
	docker push $(REGISTRY)/gateway:$(TAG)
	docker push $(REGISTRY)/worker:$(TAG)
	docker push $(REGISTRY)/reviewer:$(TAG)
	docker push $(REGISTRY)/learner:$(TAG)

# ─── Clean ────────────────────────────────────────────────────────────────────
clean: ## Remove Python cache files
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf htmlcov/ .coverage coverage.xml

clean-docker: ## Remove all project Docker containers, images, and volumes
	$(COMPOSE) down -v --rmi local --remove-orphans

# ─── Health Checks ────────────────────────────────────────────────────────────
health: ## Check health of all running services
	@echo "$(CYAN)Gateway:$(RESET)"
	@curl -sf http://localhost:8000/health | python3 -m json.tool || echo "  ❌ Gateway not responding"
	@echo "$(CYAN)Prometheus:$(RESET)"
	@curl -sf http://localhost:9090/-/healthy | head -1 || echo "  ❌ Prometheus not responding"
	@echo "$(CYAN)Grafana:$(RESET)"
	@curl -sf http://localhost:3000/api/health | python3 -m json.tool || echo "  ❌ Grafana not responding"
	@echo "$(CYAN)Langfuse:$(RESET)"
	@curl -sf http://localhost:3001/api/public/health | python3 -m json.tool || echo "  ❌ Langfuse not responding"
