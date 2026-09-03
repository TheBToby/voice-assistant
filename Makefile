# Convenience targets for the self-hosted voice assistant stack
SHELL := /bin/bash
include .env
export

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: env
env: ## Create .env from .env.example (does not overwrite)
	@test -f .env || cp .env.example .env
	@echo ".env ready - edit it and fill in your API keys"

.PHONY: build
build: ## Build all images
	docker compose build

.PHONY: up
up: ## Start the stack (livekit + weather-mcp + agent)
	docker compose up -d

.PHONY: down
down: ## Stop the stack
	docker compose down

.PHONY: restart-agent
restart-agent: ## Rebuild & restart only the agent service
	docker compose build agent && docker compose up -d --force-recreate agent

.PHONY: logs
logs: ## Tail logs of the whole stack
	docker compose logs -f --tail=100

.PHONY: logs-agent
logs-agent: ## Tail agent logs only
	docker compose logs -f --tail=200 agent

.PHONY: ps
ps: ## Show service status
	docker compose ps

.PHONY: token
token: ## Mint an access token: make token ID=device-1 ROOM=home
	python3 scripts/mint_token.py --identity $(or $(ID),device-1) --room $(or $(ROOM),home)

.PHONY: smoke
smoke: ## Run the end-to-end smoke test (stack must be up)
	docker compose --profile smoke build smoke
	docker compose --profile smoke run --rm smoke

.PHONY: health
health: ## Check that the LiveKit server responds
	@curl -fsS http://localhost:7880/ && echo " <- LiveKit OK"

.PHONY: unit-tests
unit-tests: ## Run unit tests on the host (needs: pip install pytest)
	python3 -m pytest tests/unit -v

.PHONY: clean
clean: ## Stop stack and remove volumes (deletes model cache)
	docker compose down -v
