"""Add project root to sys.path so test files can import top-level modules.

Also registers Hypothesis profiles used by `tests/property/`. The import is
guarded so the existing suite still runs without `hypothesis` installed —
property tests will simply fail at import time, which is the right signal
that dev deps need updating.
"""
import os
import sys

_repo_root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _repo_root)
# The Python SDK (`aztea` package) lives under sdks/python-sdk and isn't a
# direct child of the repo root, so we expose it on sys.path for tests that
# exercise the CLI wizard end-to-end via Typer's CliRunner.
sys.path.insert(0, os.path.join(_repo_root, "sdks", "python-sdk"))

os.environ.setdefault("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
# Tests register users via the core auth.register_user helper rather than the
# HTTP /auth/register route, so they don't naturally have a chance to call the
# new /auth/legal/accept endpoint. Disable the gate in CI/local test runs;
# production deployments do NOT set this var and remain gated.
os.environ.setdefault("AZTEA_BYPASS_LEGAL_GATE", "1")
# server.application's import-time guard refuses to load without API_KEY.
# tests/integration/conftest.py sets this for the integration suite; but
# tests/property/ pulls server.application into its import graph too, so
# without this default a bare `pytest tests/property/` fails collection
# before any property test runs. Setting it at the top-level conftest unblocks
# all collection paths uniformly.
os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

# Effectively disable the global per-IP/per-key rate limiter for the test suite.
# Why: a 120 rpm / 10 rps burst was leaking generic `rate_limit_exceeded` 429s
# into tests that intend to exercise narrower limits (per-job steer cap of 20)
# or that fan out many requests in one fixture. The flags are captured at
# feature_flags import time, so they must be set before any aztea import.
# Dedicated rate-limit tests (tests/integration/test_auth_rate_limits.py)
# don't depend on hitting the global cap.
os.environ.setdefault("AZTEA_RATE_LIMIT_DEFAULT_RPM", "1000000")
os.environ.setdefault("AZTEA_RATE_LIMIT_WORKER_RPM", "1000000")
os.environ.setdefault("AZTEA_RATE_LIMIT_ANON_RPM", "1000000")
os.environ.setdefault("AZTEA_RATE_LIMIT_BURST_RPS", "1000000")
# Also disable slowapi's per-endpoint caps (e.g. POST /skills
# @limiter.limit("10/minute")). These are independent of the per-key
# middleware above and were the source of the 429s the publish-flow +
# listing-safety-parity + agent-generator tests collectively bursted
# into after the 2026-05-17 master-only SKILL.md cut pushed all those
# tests onto one key bucket. tests/integration/test_auth_rate_limits.py
# drives slowapi directly with its own client and isn't affected.
os.environ.setdefault("AZTEA_LIMITER_DISABLED", "1")

try:
    from hypothesis import HealthCheck, settings

    settings.register_profile(
        "dev",
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    settings.register_profile(
        "ci",
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
except ImportError:
    # Hypothesis not installed yet; property tests will surface the missing dep
    # at collection time rather than silently skipping.
    pass


import pytest


@pytest.fixture(autouse=True)
def _reset_rate_limit_store_per_test():
    """Wipe core.rate_limit's in-process LRU store before every test.

    Why: even with bumped limits above, the store grows unboundedly across
    a session if not reset, which can amplify the cost of the LRU-evict
    sweep and (historically) caused cascading flakiness when limits were
    lower. This keeps each test starting from a clean slate.
    """
    from core import rate_limit

    rate_limit.reset_store_for_tests()
    yield
