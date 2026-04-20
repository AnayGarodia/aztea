.PHONY: dev test docker migrate demo lint

dev:
	uvicorn server:app --reload

test:
	pytest tests/ -v

docker:
	docker compose up --build

migrate:
	python -m core.migrate

demo:
	python scripts/demo_verification.py

lint:
	flake8 .
