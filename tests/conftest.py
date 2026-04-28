"""Add project root to sys.path so test files can import top-level modules."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
# Tests register users via the core auth.register_user helper rather than the
# HTTP /auth/register route, so they don't naturally have a chance to call the
# new /auth/legal/accept endpoint. Disable the gate in CI/local test runs;
# production deployments do NOT set this var and remain gated.
os.environ.setdefault("AZTEA_BYPASS_LEGAL_GATE", "1")
