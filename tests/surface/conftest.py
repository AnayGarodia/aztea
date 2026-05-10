"""Surface tests reuse the integration test client fixture.

Why: the auth-matrix and error-envelope suites need a real (in-process)
FastAPI app with an isolated DB. The integration conftest already builds
the right pair of fixtures — re-export them here so pytest discovers them
when collecting under tests/surface/.
"""
from tests.integration.conftest import client, isolated_db  # noqa: F401
