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
