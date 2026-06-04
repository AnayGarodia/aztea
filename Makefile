.PHONY: dev setup test test-venv docker migrate demo lint evals smoke alerts launch-check oss-check check-runtime-deps lockfile lockfile-verify

dev:
	uvicorn server:app --reload

# One-command toolbelt install: CLI + MCP registration + Claude deference hooks.
# Pass-through args: make setup ARGS="--client all --pretool-block"
setup:
	@./setup $(ARGS)

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

# Contract-drift detection (audit 2026-05-19 preventative layer):
# - tests/contract/ pins documented behavior (Free-label ↔ price, recipe
#   schemas, reserved envelope keys, jwt alg=none refusal, …)
# - scripts/lint_specs.py greps agent code for core.llm imports and
#   asserts the spec's runtime_requirements declares "llm provider", so
#   llm_used can't silently lie.
contract-tests:
	@bash -c 'set -e; test -d .venv && . .venv/bin/activate; python -m pytest -q tests/contract && python scripts/lint_specs.py'

# Catch agent imports that aren't pinned in requirements.txt. Prevents the
# "agent listed in catalog but ModuleNotFoundError on first call" failure
# mode (quant_patch_validator hit this for hypothesis on 2026-05-20).
check-runtime-deps:
	@bash -c 'set -e; test -d .venv && . .venv/bin/activate; python scripts/check_runtime_deps.py'

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

## Regenerate the pinned requirements.txt + requirements-dev.txt from the .in
## sources. Run inside the python:3.11-slim build container to keep wheel
## resolution aligned with the prod image:
##   docker run --rm -v $(PWD):/app -w /app python:3.11-slim bash -c \
##     'pip install pip-tools && make lockfile'
## Local dev (Python 3.12) also works; wheels are mostly cross-compatible but
## the Docker path is authoritative.
lockfile:
	@bash -c 'set -e; \
		test -d .venv && . .venv/bin/activate; \
		pip-compile --quiet --strip-extras --output-file=requirements.txt requirements.in; \
		pip-compile --quiet --strip-extras --output-file=requirements-dev.txt requirements-dev.in; \
		echo "  ✓ requirements.txt + requirements-dev.txt regenerated"'

## Drift gate: regenerate lockfiles into a temp dir and diff against tracked
## copies. Fails if requirements.in changed without rerunning `make lockfile`.
## Use in CI to enforce the lockfile-PR invariant.
lockfile-verify:
	@bash -c 'set -e; \
		test -d .venv && . .venv/bin/activate; \
		tmp=$$(mktemp -d); \
		pip-compile --quiet --strip-extras --output-file=$$tmp/req.txt requirements.in; \
		pip-compile --quiet --strip-extras --output-file=$$tmp/req-dev.txt requirements-dev.in; \
		if ! diff -q requirements.txt $$tmp/req.txt >/dev/null 2>&1; then \
			echo "✗ requirements.txt is stale relative to requirements.in. Run \`make lockfile\` and commit the result."; \
			diff requirements.txt $$tmp/req.txt | head -40; \
			exit 1; \
		fi; \
		if ! diff -q requirements-dev.txt $$tmp/req-dev.txt >/dev/null 2>&1; then \
			echo "✗ requirements-dev.txt is stale relative to requirements-dev.in. Run \`make lockfile\` and commit the result."; \
			diff requirements-dev.txt $$tmp/req-dev.txt | head -40; \
			exit 1; \
		fi; \
		echo "  ✓ lockfiles in sync with .in sources"'

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
