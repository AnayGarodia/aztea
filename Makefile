.PHONY: dev test test-venv docker migrate demo lint

dev:
	uvicorn server:app --reload

# Prefer project venv when present (avoids Anaconda/numpy segfaults and version skew).
test-venv:
	@bash -c 'set -e; test -d .venv && . .venv/bin/activate; export API_KEY=$${API_KEY:-test-master-key}; python -m pytest -q tests'

test:
	pytest tests/ -v

docker:
	docker compose up --build

migrate:
	python -m core.migrate

demo:
	python scripts/seed-demo.py

lint:
	flake8 .
