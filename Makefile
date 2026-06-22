# ===========================================
# Warp Engine - Makefile
# ===========================================

.PHONY: help install dev test lint format clean docker-build docker-up docker-down docker-logs docker-shell

# Load local secrets/credentials from .env when present (copy .env.example -> .env).
-include .env
export

# Default target
help:
	@echo "Warp Engine - Available Commands"
	@echo "============================="
	@echo ""
	@echo "Development:"
	@echo "  make install      - Install dependencies (uses pyproject.toml)"
	@echo "  make dev          - Run development server"
	@echo "  make test         - Run tests"
	@echo "  make lint         - Run linter (ruff + mypy)"
	@echo "  make format       - Format code (black + isort)"
	@echo ""
	@echo "Docker (Development):"
	@echo "  make docker-build - Build Docker images"
	@echo "  make docker-up    - Start all services"
	@echo "  make docker-down  - Stop all services"
	@echo "  make docker-logs  - View logs"
	@echo "  make docker-shell - Shell into API container"
	@echo ""
	@echo "Utilities:"
	@echo "  make clean        - Clean up generated files"
	@echo "  make ssl-certs    - Generate self-signed SSL certs"

# ===========================================
# Development
# ===========================================

install:
	pip install -e ".[dev]"

dev:
	PYTHONPATH=src uvicorn warp.main:app --reload --host 0.0.0.0 --port 8000

test:
	PYTHONPATH=src pytest tests/ -v --cov=src/warp --cov-report=html

lint:
	ruff check src/ tests/
	mypy src/

format:
	black src/ tests/
	isort src/ tests/
	ruff check --fix src/ tests/

# ===========================================
# Docker Development
# ===========================================

docker-build:
	docker-compose build

docker-up:
	docker-compose up -d
	@echo ""
	@echo "Services started!"
	@echo "  API:     http://localhost:8000"
	@echo "  Swagger: http://localhost:8000/docs"
	@echo "  Adminer: http://localhost:8080"
	@echo ""

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f

docker-shell:
	docker-compose exec api /bin/bash

docker-restart:
	docker-compose restart api

docker-clean:
	docker-compose down -v --rmi local

# ===========================================
# Database
# ===========================================

db-reset:
	docker-compose down -v
	docker-compose up -d postgres mysql
	@echo "Waiting for databases to initialize..."
	@sleep 10
	docker-compose up -d api

db-shell-pg:
	docker-compose exec postgres psql -U postgres -d testdb

db-shell-mysql:
	docker-compose exec mysql mysql -u root -p"$(MYSQL_PASS)" $(MYSQL_DB)

# ===========================================
# Utilities
# ===========================================

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name ".coverage" -delete 2>/dev/null || true

ssl-certs:
	@mkdir -p docker/nginx/ssl
	openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
		-keyout docker/nginx/ssl/key.pem \
		-out docker/nginx/ssl/cert.pem \
		-subj "/C=US/ST=State/L=City/O=Organization/CN=localhost"
	@echo "SSL certificates generated in docker/nginx/ssl/"

# ===========================================
# Quick Start
# ===========================================

quickstart: docker-build docker-up
	@echo ""
	@echo "Quick start complete! Try these commands:"
	@echo ""
	@echo "  # List all users"
	@echo "  curl http://localhost:8000/api/v1/users"
	@echo ""
	@echo "  # Get products with filtering"
	@echo "  curl 'http://localhost:8000/api/v1/products?filter[status]=active'"
	@echo ""
	@echo "  # Create a new user"
	@echo "  curl -X POST http://localhost:8000/api/v1/users \\"
	@echo "    -H 'Content-Type: application/json' \\"
	@echo "    -d '{\"username\":\"test\",\"email\":\"test@example.com\",\"password_hash\":\"hash\"}'"
	@echo ""
