.PHONY: dev test lint build up down logs

dev:
	uvicorn pingwatch.api.app:app --reload --app-dir src --port 5000

test:
	pytest -q

lint:
	ruff check src tests && mypy src

build:
	cd docker && docker compose build

up:
	cd docker && docker compose up -d

down:
	cd docker && docker compose down

logs:
	docker logs pingwatch -f --tail=200
