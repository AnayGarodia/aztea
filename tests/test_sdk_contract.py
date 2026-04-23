import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import requests
import uvicorn

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core import auth
from core import disputes
from core import jobs
from core import payments
from core import registry
from core import reputation
import server.application as server

SDK_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "sdks" / "python"
sys.path.insert(0, str(SDK_PYTHON_ROOT))
from aztea.client import AzteaClient


TEST_MASTER_KEY = "test-master-key"


def _close_module_conn(module) -> None:
    conn = getattr(module._local, "conn", None)
    if conn is None:
        return
    conn.close()
    try:
        delattr(module._local, "conn")
    except AttributeError:
        pass


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def sdk_server(monkeypatch):
    db_path = Path(__file__).resolve().parent / f"test-sdk-contract-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes)
    for module in modules:
        _close_module_conn(module)
        monkeypatch.setattr(module, "DB_PATH", str(db_path))

    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)

    port = _free_tcp_port()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    app_server = uvicorn.Server(config)
    app_server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=app_server.run, name="sdk-contract-server", daemon=True)
    thread.start()

    try:
        deadline = time.time() + 8
        while not app_server.started and thread.is_alive() and time.time() < deadline:
            time.sleep(0.05)
        assert app_server.started, "uvicorn server did not start in time"
        yield f"http://127.0.0.1:{port}"
    finally:
        app_server.should_exit = True
        thread.join(timeout=5)
        for module in modules:
            _close_module_conn(module)
        for suffix in ("", "-shm", "-wal"):
            path = Path(f"{db_path}{suffix}")
            if path.exists():
                path.unlink()


def _random_identity(prefix: str) -> tuple[str, str]:
    token = uuid.uuid4().hex[:8]
    return f"{prefix}-{token}", f"{prefix}-{token}@example.com"


def test_python_sdk_contract_major_flow(sdk_server):
    public = AzteaClient(base_url=sdk_server)
    worker_name, worker_email = _random_identity("worker")
    caller_name, caller_email = _random_identity("caller")
    worker_user = public.auth.register(worker_name, worker_email, "password123")
    caller_user = public.auth.register(caller_name, caller_email, "password123")

    worker = AzteaClient(base_url=sdk_server, api_key=str(worker_user["raw_api_key"]))
    caller = AzteaClient(base_url=sdk_server, api_key=str(caller_user["raw_api_key"]))

    register = worker.registry.register(
        name=f"SDK Worker {uuid.uuid4().hex[:6]}",
        description="SDK contract worker",
        endpoint_url=f"{sdk_server}/agents/financial",
        price_per_call_usd=0.05,
        tags=["sdk-contract"],
        input_schema={"type": "object", "properties": {"ticker": {"type": "string", "title": "Ticker", "description": "Stock ticker symbol"}}},
        output_examples=[{"input": {"ticker": "AAPL"}, "output": {"summary": "Apple Inc."}}],
    )
    agent_id = str(register["agent_id"])
    approve = requests.post(
        f"{sdk_server}/admin/agents/{agent_id}/review",
        headers={"Authorization": f"Bearer {TEST_MASTER_KEY}"},
        json={"decision": "approve", "note": "sdk contract auto-approval"},
        timeout=15,
    )
    assert approve.status_code == 200, approve.text
    wallet = caller.wallets.me()
    dep = requests.post(
        f"{sdk_server}/wallets/deposit",
        headers={"Authorization": f"Bearer {TEST_MASTER_KEY}"},
        json={"wallet_id": str(wallet["wallet_id"]), "amount_cents": 2_000, "memo": "sdk test"},
        timeout=10,
    )
    assert dep.status_code == 200, dep.text
    listed = caller.registry.list(tag="sdk-contract")
    assert any(str(item["agent_id"]) == agent_id for item in listed.get("agents", []))

    job = caller.jobs.create(agent_id=agent_id, input_payload={"ticker": "AAPL"}, max_attempts=2)

    received: dict[str, object] = {}
    done = threading.Event()

    def _consume() -> None:
        try:
            for message in job.stream_messages():
                received["message"] = message
                done.set()
                return
        finally:
            done.set()

    stream_thread = threading.Thread(target=_consume, name="sdk-python-stream", daemon=True)
    stream_thread.start()

    claim = worker.jobs.claim(job.job_id, lease_seconds=300)
    worker.jobs.send_progress(job.job_id, percent=42, note="analyzing filing")
    assert done.wait(timeout=2), "expected stream message delivery via Python SDK"
    worker.jobs.complete(
        job.job_id,
        output_payload={"ticker": "AAPL", "signal": "positive"},
        claim_token=str(claim["claim_token"]),
    )
    terminal = job.wait_for_completion(timeout=30, poll_interval=0.25)
    assert terminal["status"] == "complete"
    assert isinstance(terminal["output_payload"], dict)

    @worker.worker(agent_id=agent_id, concurrency=1, poll_interval=0.1)
    def handler(payload):
        return {"echo": payload.get("ticker", "UNKNOWN")}

    second_job = caller.jobs.create(agent_id=agent_id, input_payload={"ticker": "MSFT"}, max_attempts=2)
    processed = handler.run_once()
    assert processed >= 1
    second_terminal = second_job.wait_for_completion(timeout=30, poll_interval=0.25)
    assert second_terminal["status"] == "complete"


def test_typescript_sdk_generated_contract_types_compile_against_live_openapi(sdk_server):
    sdk_ts_dir = Path(__file__).resolve().parents[1] / "sdks" / "typescript"
    openapi_path = sdk_ts_dir / "openapi.contract.json"
    generated_contract_file = sdk_ts_dir / "scripts" / "generated-contract-check.ts"

    spec_response = requests.get(f"{sdk_server}/openapi.json", timeout=15)
    spec_response.raise_for_status()
    openapi_path.write_text(spec_response.text, encoding="utf-8")

    health_response = requests.get(f"{sdk_server}/health", timeout=15)
    health_response.raise_for_status()
    health_payload = health_response.json()

    generated_contract_file.write_text(
        (
            "import type { components } from '../src/generated/types';\n"
            f"const health: components['schemas']['HealthResponse'] = {json.dumps(health_payload)};\n"
            "void health;\n"
        ),
        encoding="utf-8",
    )

    try:
        subprocess.run(["npm", "install", "--silent"], cwd=sdk_ts_dir, check=True)
        subprocess.run(
            ["npx", "openapi-typescript", str(openapi_path), "-o", "src/generated/types.ts"],
            cwd=sdk_ts_dir,
            check=True,
        )
        subprocess.run(["npm", "run", "test"], cwd=sdk_ts_dir, check=True)
    finally:
        if generated_contract_file.exists():
            generated_contract_file.unlink()
        if openapi_path.exists():
            openapi_path.unlink()
