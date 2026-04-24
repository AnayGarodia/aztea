from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

# Add SDK to path when not installed as a package (…/repo/tui/aztea_tui/api.py → repo root)
_SDK_PATH = Path(__file__).resolve().parents[2] / "sdks" / "python"
if str(_SDK_PATH) not in sys.path:
    sys.path.insert(0, str(_SDK_PATH))

from aztea import AzteaClient  # noqa: E402
try:  # noqa: E402
    # sdks/python package layout
    from aztea.errors import AzteaError
except ModuleNotFoundError:  # noqa: E402
    # sdks/python-sdk package layout
    from aztea.exceptions import AzteaError


class AzteaAPIError(Exception):
    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_usd(cents: int) -> str:
    return f"${cents / 100:.2f}"


def _fmt_price(usd: float) -> str:
    return f"${usd:.2f}"


def _fmt_relative(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        diff = datetime.now(timezone.utc) - dt
        s = int(diff.total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return str(iso)[:10]


# ── Typed result dataclasses ──────────────────────────────────────────────────

@dataclass
class LoginResult:
    user_id: str
    username: str
    api_key: str


@dataclass
class AgentRow:
    agent_id: str
    name: str
    description: str
    price_display: str
    price_usd: float
    trust_score: float
    success_rate: float
    total_calls: int
    status: str
    health: str
    tags: list[str]


@dataclass
class AgentDetail:
    agent_id: str
    name: str
    description: str
    price_display: str
    price_usd: float
    trust_score: float
    success_rate: float
    total_calls: int
    status: str
    tags: list[str]
    input_schema: dict
    output_examples: list


@dataclass
class JobRow:
    job_id: str
    short_id: str
    agent_id: str
    status: str
    created_display: str
    cost_display: str


@dataclass
class JobDetail:
    job_id: str
    agent_id: str
    status: str
    input_payload: dict
    output_payload: dict | None
    error_message: str | None
    cost_display: str
    created_display: str
    completed_display: str


@dataclass
class WalletInfo:
    wallet_id: str
    balance_cents: int
    balance_display: str
    trust: float | None


# ── Main API adapter ──────────────────────────────────────────────────────────

def _make_client(api_key: str | None, base_url: str) -> AzteaClient:
    return AzteaClient(base_url=base_url, api_key=api_key)


class AzteaAPI:
    def __init__(self, api_key: str | None, base_url: str) -> None:
        self._client = _make_client(api_key, base_url)
        self._base_url = base_url

    def set_api_key(self, key: str) -> None:
        self._client.set_api_key(key)

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def login(self, email: str, password: str) -> LoginResult:
        try:
            data = await asyncio.to_thread(self._client.auth.login, email, password)
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e
        return LoginResult(
            user_id=str(data.get("user_id", "")),
            username=str(data.get("username", "")),
            api_key=str(data.get("raw_api_key", "")),
        )

    async def login_with_key(self, api_key: str) -> str:
        """Validate an API key via /auth/me. Returns username."""
        tmp = _make_client(api_key, self._base_url)
        try:
            data = await asyncio.to_thread(tmp.auth.me)
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e
        finally:
            tmp.close()
        return str(data.get("username", ""))

    async def me(self) -> dict:
        try:
            return await asyncio.to_thread(self._client.auth.me)
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e

    # ── Registry ──────────────────────────────────────────────────────────────

    async def list_agents(self, tag: str | None = None) -> list[AgentRow]:
        try:
            data = await asyncio.to_thread(
                self._client.registry.list, tag=tag, rank_by="trust_score"
            )
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e
        return [
            AgentRow(
                agent_id=str(a.get("agent_id", "")),
                name=str(a.get("name", "")),
                description=str(a.get("description", "")),
                price_display=_fmt_price(float(a.get("price_per_call_usd") or 0)),
                price_usd=float(a.get("price_per_call_usd") or 0),
                trust_score=float(a.get("trust_score") or 0),
                success_rate=float(a.get("success_rate") or 0),
                total_calls=int(a.get("total_calls") or 0),
                status=str(a.get("status", "unknown")),
                health=str(a.get("endpoint_health_status") or "unknown"),
                tags=list(a.get("tags") or []),
            )
            for a in (data.get("agents") or [])
        ]

    async def list_my_agents(self) -> list[AgentRow]:
        try:
            data = await asyncio.to_thread(
                self._client._request_json, "GET", "/registry/agents/mine"
            )
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e
        return [
            AgentRow(
                agent_id=str(a.get("agent_id", "")),
                name=str(a.get("name", "")),
                description=str(a.get("description", "")),
                price_display=_fmt_price(float(a.get("price_per_call_usd") or 0)),
                price_usd=float(a.get("price_per_call_usd") or 0),
                trust_score=float(a.get("trust_score") or 0),
                success_rate=float(a.get("success_rate") or 0),
                total_calls=int(a.get("total_calls") or 0),
                status=str(a.get("status", "unknown")),
                health=str(a.get("endpoint_health_status") or "unknown"),
                tags=list(a.get("tags") or []),
            )
            for a in (data.get("agents") or [])
        ]

    async def get_agent(self, agent_id: str) -> AgentDetail:
        try:
            a = await asyncio.to_thread(self._client.registry.get, agent_id)
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e
        return AgentDetail(
            agent_id=str(a.get("agent_id", "")),
            name=str(a.get("name", "")),
            description=str(a.get("description", "")),
            price_display=_fmt_price(float(a.get("price_per_call_usd") or 0)),
            price_usd=float(a.get("price_per_call_usd") or 0),
            trust_score=float(a.get("trust_score") or 0),
            success_rate=float(a.get("success_rate") or 0),
            total_calls=int(a.get("total_calls") or 0),
            status=str(a.get("status", "unknown")),
            tags=list(a.get("tags") or []),
            input_schema=dict(a.get("input_schema") or {}),
            output_examples=list(a.get("output_examples") or []),
        )

    async def hire_agent(self, agent_id: str, payload: dict) -> dict:
        try:
            result = await asyncio.to_thread(self._client.registry.call, agent_id, payload)
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e
        return dict(result)

    # ── Jobs ──────────────────────────────────────────────────────────────────

    async def list_jobs(
        self,
        *,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[JobRow], str | None]:
        try:
            data = await asyncio.to_thread(
                self._client.jobs.list, status=status, cursor=cursor, limit=limit
            )
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e
        rows = [
            JobRow(
                job_id=str(j.get("job_id", "")),
                short_id=str(j.get("job_id", ""))[:8],
                agent_id=str(j.get("agent_id", "")),
                status=str(j.get("status", "unknown")),
                created_display=_fmt_relative(j.get("created_at")),  # type: ignore[arg-type]
                cost_display=_fmt_usd(
                    int(j.get("caller_charge_cents") or j.get("price_cents") or 0)
                ),
            )
            for j in (data.get("jobs") or [])
        ]
        return rows, data.get("next_cursor")  # type: ignore[return-value]

    async def get_job(self, job_id: str) -> JobDetail:
        try:
            j = await asyncio.to_thread(self._client.jobs.get_raw, job_id)
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e
        return JobDetail(
            job_id=str(j.get("job_id", "")),
            agent_id=str(j.get("agent_id", "")),
            status=str(j.get("status", "unknown")),
            input_payload=dict(j.get("input_payload") or {}),
            output_payload=j.get("output_payload"),  # type: ignore[arg-type]
            error_message=j.get("error_message"),  # type: ignore[arg-type]
            cost_display=_fmt_usd(
                int(j.get("caller_charge_cents") or j.get("price_cents") or 0)
            ),
            created_display=_fmt_relative(j.get("created_at")),  # type: ignore[arg-type]
            completed_display=_fmt_relative(j.get("completed_at")),  # type: ignore[arg-type]
        )

    async def stream_job_messages(
        self, job_id: str, since: int | None = None
    ) -> AsyncIterator[dict]:
        """Bridge blocking SSE iterator → async generator via daemon thread + queue."""
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def _produce() -> None:
            try:
                for event in self._client.jobs.stream_messages(job_id, since=since):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception:
                pass
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        t = threading.Thread(target=_produce, daemon=True)
        t.start()

        while True:
            item = await queue.get()
            if item is None:
                return
            yield item

    async def list_job_messages(self, job_id: str, since: int | None = None) -> list[dict]:
        try:
            data = await asyncio.to_thread(
                self._client.jobs.list_messages, job_id, since=since
            )
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e
        return list(data.get("messages") or [])

    # ── Wallet ────────────────────────────────────────────────────────────────

    async def get_wallet(self) -> WalletInfo:
        try:
            w = await asyncio.to_thread(self._client.wallets.me)
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e
        cents = int(w.get("balance_cents") or 0)
        return WalletInfo(
            wallet_id=str(w.get("wallet_id", "")),
            balance_cents=cents,
            balance_display=_fmt_usd(cents),
            trust=w.get("caller_trust"),  # type: ignore[arg-type]
        )

    async def deposit(self, wallet_id: str, amount_cents: int, memo: str = "TUI deposit") -> dict:
        try:
            result = await asyncio.to_thread(
                self._client.wallets.deposit, wallet_id, amount_cents, memo
            )
        except AzteaError as e:
            raise AzteaAPIError(str(e)) from e
        return dict(result)

    def close(self) -> None:
        self._client.close()
