import asyncio
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest
import requests
import uvicorn

from core import auth
from core import disputes
from core import jobs
from core import payments
from core import registry
from core import reputation
import server.application as server

SDK_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "sdks" / "python"
import sys

sys.path.insert(0, str(SDK_PYTHON_ROOT))
from aztea import AgentServer, AsyncAzteaClient, AzteaClient


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
    db_path = Path(__file__).resolve().parent / f"test-sdk-consolidation-{uuid.uuid4().hex}.db"
    modules = (registry, payments, auth, jobs, reputation, disputes)
    for module in modules:
        _close_module_conn(module)
        monkeypatch.setattr(module, "DB_PATH", str(db_path))

    monkeypatch.setattr(server, "_MASTER_KEY", TEST_MASTER_KEY)

    port = _free_tcp_port()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    app_server = uvicorn.Server(config)
    app_server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=app_server.run, name="sdk-consolidation-server", daemon=True)
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


def test_canonical_python_sdk_covers_legacy_high_level_surface(sdk_server):
    public = AzteaClient(base_url=sdk_server)
    worker_name, worker_email = _random_identity("worker")
    caller_name, caller_email = _random_identity("caller")
    worker_user = public.auth.register(worker_name, worker_email, "password123")
    caller_user = public.auth.register(caller_name, caller_email, "password123")

    worker = AzteaClient(base_url=sdk_server, api_key=str(worker_user["raw_api_key"]))
    caller = AzteaClient(base_url=sdk_server, api_key=str(caller_user["raw_api_key"]))

    register = worker.registry.register(
        name=f"SDK Compat {uuid.uuid4().hex[:6]}",
        description="SDK compatibility worker for canonical package coverage",
        endpoint_url=f"{sdk_server}/agents/financial",
        price_per_call_usd=0.05,
        tags=["sdk-compat"],
        input_schema={
            "type": "object",
            "properties": {"ticker": {"type": "string", "title": "Ticker"}},
        },
        output_schema={"type": "object"},
        output_examples=[{"input": {"ticker": "AAPL"}, "output": {"echo": "AAPL"}}],
    )
    agent_id = str(register["agent_id"])

    approve = requests.post(
        f"{sdk_server}/admin/agents/{agent_id}/review",
        headers={"Authorization": f"Bearer {TEST_MASTER_KEY}"},
        json={"decision": "approve", "note": "sdk consolidation auto-approval"},
        timeout=15,
    )
    assert approve.status_code == 200, approve.text

    wallet = caller.get_wallet()
    deposit = requests.post(
        f"{sdk_server}/wallets/deposit",
        headers={"Authorization": f"Bearer {TEST_MASTER_KEY}"},
        json={"wallet_id": wallet.wallet_id, "amount_cents": 2_000, "memo": "sdk test"},
        timeout=15,
    )
    assert deposit.status_code == 200, deposit.text

    listed = caller.list_agents(tag="sdk-compat")
    assert any(item.agent_id == agent_id for item in listed)
    searched = caller.search_agents("SDK compatibility worker")
    assert isinstance(searched, list)
    assert caller.get_balance() >= 2_000
    assert isinstance(caller.get_spend_summary("7d"), dict)

    worker_runner = worker.worker(agent_id=agent_id, concurrency=1, poll_interval=0.1)

    @worker_runner
    def handler(payload):
        return {"echo": payload.get("ticker", "UNKNOWN")}

    def _process_until_stop(stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            processed = handler.run_once()
            if processed == 0:
                time.sleep(0.1)

    stop = threading.Event()
    worker_thread = threading.Thread(target=_process_until_stop, args=(stop,), daemon=True)
    worker_thread.start()
    try:
        result = caller.hire(agent_id, {"ticker": "MSFT"}, timeout_seconds=30)
    finally:
        stop.set()
        worker_thread.join(timeout=2)
    assert result.output["echo"] == "MSFT"

    pending = caller.hire(agent_id, {"ticker": "AAPL"}, wait=False)
    raw_job = worker.jobs.get_raw(pending.job_id)

    agent_server = AgentServer(
        api_key=str(worker_user["raw_api_key"]),
        name="Unused SDK server",
        description="Unused SDK server description",
        price_per_call_usd=0.01,
        base_url=sdk_server,
    )
    agent_server._agent_id = agent_id

    @agent_server.handler
    def agent_handle(payload):
        return {"symbol": payload.get("ticker")}

    agent_server._process_job(raw_job)
    final = caller.wait_for(pending.job_id, timeout_seconds=30)
    assert final.output["symbol"] == "AAPL"

    async def _async_checks() -> None:
        async with AsyncAzteaClient(base_url=sdk_server, api_key=str(caller_user["raw_api_key"])) as async_client:
            balance = await async_client.get_balance()
            agents = await async_client.list_agents(tag="sdk-compat")
            summary = await async_client.get_spend_summary("7d")
            assert balance >= 0
            assert any(item.agent_id == agent_id for item in agents)
            assert isinstance(summary, dict)

    asyncio.run(_async_checks())
