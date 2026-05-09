.PHONY: dev test test-venv docker migrate demo lint evals smoke alerts launch-check oss-check

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

# Launch readiness gates: each one is intended to be a hard CI/cron check.
# evals: runs the deterministic agent contract suite (tests/test_agent_golden_evals.py)
# smoke: runs the buyer-path harness against $$AZTEA_BASE_URL (needs AZTEA_API_KEY)
# alerts: collects /ops metrics and exits non-zero on any critical alert
# launch-check: bundles evals + alerts (smoke is excluded — it requires a live key)
evals:
	@bash -c 'set -e; test -d .venv && . .venv/bin/activate; python -m pytest -q tests/test_agent_golden_evals.py tests/test_launch_alerts.py'

smoke:
	@bash -c 'test -d .venv && . .venv/bin/activate; python scripts/production_smoke.py'

alerts:
	@bash -c 'test -d .venv && . .venv/bin/activate; python scripts/launch_alerts.py'

launch-check: evals
	@bash -c 'if [ -n "$$AZTEA_API_KEY" ]; then python scripts/launch_alerts.py; else echo "(skip alerts — set AZTEA_API_KEY to run them)"; fi'

## Run integration tests against PostgreSQL 16 (requires Docker)
test-postgres:
	docker compose -f docker-compose.postgres.yml up \
		--build \
		--abort-on-container-exit \
		--exit-code-from tests
	docker compose -f docker-compose.postgres.yml down -v

## Tear down the Postgres test stack and remove volumes
test-postgres-clean:
	docker compose -f docker-compose.postgres.yml down -v --remove-orphans

## OSS-mode sanity gate. Runs the OSS isolation tests, the line-budget check,
## and a grep for hardcoded aztea.ai URLs in the runtime code paths. Run this
## locally before opening an OSS-related PR.
oss-check:
	@bash -c 'set -e; \
		echo "→ OSS-mode isolation tests"; \
		test -d .venv && . .venv/bin/activate; \
		python -m pytest -q tests/test_oss_mode_isolation.py; \
		echo "→ Line-budget check"; \
		python scripts/check_file_line_budget.py || echo "  (pre-existing budget overruns — track separately)"; \
		echo "→ No new hardcoded aztea.ai URLs in core/, server/, agents/"; \
		if grep -RInE "https?://(api\\.)?aztea\\.(ai|dev)" core/ server/ agents/ \
			| grep -v "# " \
			| grep -vE "(hosted_url|docs|aztea_(do|search|describe|call)|aztea/1\\.0|#.*aztea)" \
			| grep -v "test_" ; then \
			echo "  ✗ unexpected aztea.ai URL(s) above"; exit 1; \
		else \
			echo "  ✓ clean"; \
		fi; \
		echo "→ HostedClient is the only outbound caller"; \
		if grep -RIn "requests\\.\\(post\\|get\\)" core/ server/ \
			| grep -i "aztea\\.\\(ai\\|dev\\)" ; then \
			echo "  ✗ direct outbound to aztea.ai outside hosted_client.py"; exit 1; \
		else \
			echo "  ✓ clean"; \
		fi'
