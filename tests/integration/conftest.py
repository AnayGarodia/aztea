import os

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from pathlib import Path

import pytest
import uuid
from fastapi.testclient import TestClient

from core import auth
from core import cache as result_cache
from core import compare
from core import disputes
from core import idempotency
from core import jobs
from core import payments
from core import pipelines
from core import registry
from core import reputation
from core import workspaces
import server.application as server

from tests.integration.helpers import TEST_MASTER_KEY, _close_module_conn


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = Path(__file__).resolve().parent / f"test-server-integration-{uuid.uuid4().hex}.db"
    modules = (
        registry, payments, auth, jobs, reputation, disputes, idempotency,
        result_cache, compare, pipelines, workspaces,
    )

    # Keep the per-server workspace signing key in tmp_path so test runs
    # never write into ./data and leak a real keypair into git.
    monkeypatch.setenv(
        "AZTEA_WORKSPACE_SIGNING_KEY_PATH",
        str(tmp_path / "workspace_signing_key.pem"),
    )

    for module in modules:
        _close_module_conn(module)
        monkeypatch.setattr(module, "DB_PATH", str(db_path))

    # Apply real migrations so tests see the same schema additions
    # (e.g. workspaces 0048, pipeline_runs.workspace_id 0049) that
    # production carries. Without this, init_db()'s CREATE-TABLE-IF-NOT-
    # EXISTS path runs without the columns added by later migrations.
    from core.migrate import apply_migrations as _apply_migrations
    _apply_migrations(str(db_path))

    yield db_path

    for module in modules:
        _close_module_conn(module)

    for suffix in ("", "-shm", "-wal"):
        path = Path(f"{db_path}{suffix}")
        if path.exists():
            path.unlink()


@pytest.fixture
def client(isolated_db, monkeypatch):
    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)
    with TestClient(server.app) as test_client:
        yield test_client
