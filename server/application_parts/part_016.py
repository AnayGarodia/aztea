# ── Otto realtime relay ───────────────────────────────────────────────────────
# WS /otto/realtime — an authenticated, budget-metered WebSocket relay between the
# Otto desktop app and Azure OpenAI's Realtime API (gpt-realtime-2). It mirrors the
# /otto/chat HTTP proxy (part_015) but for the bidirectional voice stream, so the
# Azure key NEVER ships in the app and a hard dollar cap protects the shared resource.
#
#   • Auth:   the same shared bearer secret as /otto/chat. The app connects with
#             `Authorization: Bearer <T>` (or `?token=<T>`); checked against the
#             OTTO_APP_TOKEN env. The token is baked into the app and extractable —
#             that's fine; the budget below is the real protection.
#   • Upstream: opens a server-side WS to the realtime backend and relays every frame in
#             both directions unchanged. With OTTO_USE_LITELLM=1 the backend is the local
#             LiteLLM proxy (/v1/realtime — it holds the Azure key + pins the deployment);
#             otherwise it's Azure directly (legacy/rollback). A client can't switch to a
#             pricier model either way (deployment is server-pinned).
#   • Budget: a single shared spend pool tracked in a tiny SQLite counter, priced at Azure
#             realtime rates, metered from each `response.done` event's token counts (text
#             vs audio). IMPORTANT: this meter reads the relayed frames, so it works
#             identically whether upstream is Azure or LiteLLM — it does NOT rely on the
#             gateway tracking audio spend. (The LiteLLM realtime virtual key's max_budget
#             is a secondary backstop.) When the pool is exhausted → the session is closed
#             and new connections are refused (close code 4402) until it's reset/raised.
#
# Server env:
#   OTTO_APP_TOKEN              shared bearer secret (must match the app's baked-in token)
#   OTTO_USE_LITELLM            "1" → relay via the LiteLLM gateway (below); else direct Azure
#   OTTO_REALTIME_LITELLM_URL   LiteLLM ws base, e.g. ws://127.0.0.1:4001
#   OTTO_REALTIME_LITELLM_KEY   LiteLLM virtual key (max_budget=$300 backstop; server-side only)
#   OTTO_REALTIME_LITELLM_MODEL LiteLLM model alias to pin (default otto-realtime)
#   AZURE_REALTIME_URL          [legacy/rollback] full upstream wss base, e.g.
#                               wss://<resource>.openai.azure.com/openai/v1/realtime
#   AZURE_REALTIME_KEY          [legacy/rollback] the Azure OpenAI resource key (server-side only)
#   AZURE_REALTIME_MODEL        [legacy/rollback] deployment/model name (default gpt-realtime-2)
#   AZURE_REALTIME_API_VERSION  [legacy/rollback] api-version query (default 2025-04-01-preview)
#   OTTO_RT_BUDGET_CAP_CENTS    realtime spend cap in cents (default 30000 = $300)
#   OTTO_RT_BUDGET_DB           sqlite path (default = OTTO_BUDGET_DB or ~/.otto-proxy-budget.sqlite3)
#   OTTO_RT_MAX_SESSION_SECONDS hard per-session duration cap (default 900 = 15 min)
#   OTTO_RT_MAX_CONCURRENT      max simultaneous sessions per worker (default 32)
import asyncio
import hmac
import json
import os
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets
from fastapi import WebSocket, WebSocketDisconnect

# Azure realtime list prices, cents per 1,000,000 tokens — (approximate + adjustable;
# this only sizes the shared cap). Audio tokens dominate a voice session, so they are
# priced separately from text. Tune to your actual Azure rate card; rounding UP here
# means the cap trips slightly early rather than overshooting $300.
_OTTO_RT_RATE_TEXT_IN = 400.0     # $4 / 1M
_OTTO_RT_RATE_TEXT_OUT = 1600.0   # $16 / 1M
_OTTO_RT_RATE_AUDIO_IN = 3200.0   # $32 / 1M
_OTTO_RT_RATE_AUDIO_OUT = 6400.0  # $64 / 1M

# Coarse, per-worker guard against a burst of simultaneous sessions. The budget is the
# real cap; this just stops one worker from fanning out to hundreds of upstream sockets.
_otto_rt_active = 0


def _otto_rt_budget_db() -> str:
    return (
        os.environ.get("OTTO_RT_BUDGET_DB")
        or os.environ.get("OTTO_BUDGET_DB")
        or os.path.expanduser("~/.otto-proxy-budget.sqlite3")
    )


def _otto_rt_budget_cap_cents() -> float:
    try:
        return float(os.environ.get("OTTO_RT_BUDGET_CAP_CENTS") or 30000)
    except (TypeError, ValueError):
        return 30000.0


def _otto_rt_max_session_seconds() -> float:
    try:
        return float(os.environ.get("OTTO_RT_MAX_SESSION_SECONDS") or 900)
    except (TypeError, ValueError):
        return 900.0


def _otto_rt_max_concurrent() -> int:
    try:
        return int(os.environ.get("OTTO_RT_MAX_CONCURRENT") or 32)
    except (TypeError, ValueError):
        return 32


def _otto_rt_budget_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_otto_rt_budget_db(), timeout=10)
    # WAL + short busy_timeout, matching the composio/responses budget DBs: keep lock waits brief
    # so the realtime meter's asyncio.to_thread calls can't pile up under contention.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS otto_rt_budget ("
        "  id INTEGER PRIMARY KEY CHECK(id = 1),"
        "  spent_cents REAL NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT OR IGNORE INTO otto_rt_budget (id, spent_cents) VALUES (1, 0)")
    conn.commit()
    return conn


def _otto_rt_spent_cents() -> float:
    conn = _otto_rt_budget_conn()
    try:
        row = conn.execute("SELECT spent_cents FROM otto_rt_budget WHERE id = 1").fetchone()
        return float(row[0]) if row else 0.0
    finally:
        conn.close()


def _otto_rt_budget_add(delta_cents: float) -> None:
    conn = _otto_rt_budget_conn()
    try:
        conn.execute(
            "UPDATE otto_rt_budget SET spent_cents = MAX(0, spent_cents + ?) WHERE id = 1",
            (delta_cents,),
        )
        conn.commit()
    finally:
        conn.close()


def _otto_rt_usage_cents(usage: dict) -> float:
    """Dollar cost (in cents) of one realtime response from its usage block."""
    idet = usage.get("input_token_details") or {}
    odet = usage.get("output_token_details") or {}
    text_in = float(idet.get("text_tokens") or 0)
    audio_in = float(idet.get("audio_tokens") or 0)
    text_out = float(odet.get("text_tokens") or 0)
    audio_out = float(odet.get("audio_tokens") or 0)
    # Older/edge payloads may omit the per-modality breakdown. Treat the totals as audio
    # (the expensive case) so we never under-bill the cap.
    if not idet and not odet:
        audio_in = float(usage.get("input_tokens") or 0)
        audio_out = float(usage.get("output_tokens") or 0)
    return (
        text_in * _OTTO_RT_RATE_TEXT_IN
        + audio_in * _OTTO_RT_RATE_AUDIO_IN
        + text_out * _OTTO_RT_RATE_TEXT_OUT
        + audio_out * _OTTO_RT_RATE_AUDIO_OUT
    ) / 1_000_000.0


def _otto_rt_meter(text: str) -> float:
    """Return the cents to bill for an upstream text frame (0 unless it carries usage)."""
    try:
        evt = json.loads(text)
    except (ValueError, TypeError):
        return 0.0
    if not isinstance(evt, dict) or evt.get("type") != "response.done":
        return 0.0
    usage = ((evt.get("response") or {}).get("usage")) or {}
    if not usage:
        return 0.0
    try:
        return _otto_rt_usage_cents(usage)
    except (ValueError, TypeError):
        return 0.0


def _otto_rt_upstream_url() -> str | None:
    """Server-pinned Azure realtime URL with api-version + model query params."""
    base = (os.environ.get("AZURE_REALTIME_URL") or "").strip()
    if not base:
        return None
    model = (os.environ.get("AZURE_REALTIME_MODEL") or "gpt-realtime-2").strip()
    api_version = (os.environ.get("AZURE_REALTIME_API_VERSION") or "2025-04-01-preview").strip()
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query))
    query["api-version"] = api_version
    query["model"] = model
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _otto_rt_resolve_upstream() -> tuple[str, dict] | None:
    """Resolve (upstream_url, headers) for the realtime relay.

    OTTO_USE_LITELLM → the local LiteLLM gateway's /v1/realtime (Bearer the realtime virtual
    key); otherwise the server-pinned Azure URL (api-key). Returns None when the chosen
    backend is unconfigured, so the caller refuses the session.
    """
    # Per-path flag (OTTO_REALTIME_USE_LITELLM) with OTTO_USE_LITELLM as a shared fallback,
    # so realtime can stay on direct Azure while responses route via the gateway.
    _rt_flag = (
        os.environ.get("OTTO_REALTIME_USE_LITELLM")
        or os.environ.get("OTTO_USE_LITELLM")
        or ""
    ).strip().lower()
    if _rt_flag in ("1", "true", "yes", "on"):
        base = (os.environ.get("OTTO_REALTIME_LITELLM_URL") or "").strip().rstrip("/")
        key = (os.environ.get("OTTO_REALTIME_LITELLM_KEY") or "").strip()
        model = (os.environ.get("OTTO_REALTIME_LITELLM_MODEL") or "otto-realtime").strip()
        if not base or not key:
            return None
        return f"{base}/v1/realtime?model={model}", {"authorization": f"Bearer {key}"}
    url = _otto_rt_upstream_url()
    key = (os.environ.get("AZURE_REALTIME_KEY") or "").strip()
    if not url or not key:
        return None
    return url, {"api-key": key}


def _otto_rt_bearer(websocket: WebSocket) -> str:
    auth = websocket.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer ") :].strip()
    return (websocket.query_params.get("token") or "").strip()


async def _otto_rt_pump_client_to_upstream(client: WebSocket, upstream) -> None:
    """Forward every frame the app sends straight to Azure, unchanged."""
    while True:
        msg = await client.receive()
        if msg.get("type") == "websocket.disconnect":
            return
        text = msg.get("text")
        if text is not None:
            await upstream.send(text)
            continue
        data = msg.get("bytes")
        if data is not None:
            await upstream.send(data)


async def _otto_rt_pump_upstream_to_client(client: WebSocket, upstream, cap_cents: float) -> None:
    """Forward Azure → app, metering usage and tripping the cap when the pool is spent."""
    async for msg in upstream:
        if isinstance(msg, (bytes, bytearray)):
            await client.send_bytes(bytes(msg))
            continue
        await client.send_text(msg)
        cents = _otto_rt_meter(msg)
        if cents <= 0:
            continue
        await asyncio.to_thread(_otto_rt_budget_add, cents)
        spent = await asyncio.to_thread(_otto_rt_spent_cents)
        if spent >= cap_cents:
            # Tell the app why the stream is ending, then stop relaying. The outer
            # handler closes both sockets.
            try:
                await client.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "budget_exceeded",
                                "code": "otto_voice_at_capacity",
                                "message": "Otto voice is at capacity (shared budget reached). Please try again later.",
                            },
                        }
                    )
                )
            except Exception:
                pass
            return



def _otto_proxy_auth_ok(token: str) -> bool:
    """Authorize an /otto/* proxy request. Accepts EITHER the shared app token
    (OTTO_APP_TOKEN — legacy / unsigned clients) OR a valid signed-in user's
    API key (az_...). The Otto macOS app sends the user's own key after
    sign-in, so real users authenticate as themselves, not a shared secret."""
    if not token:
        return False
    expected = os.environ.get("OTTO_APP_TOKEN", "").strip()
    if expected and hmac.compare_digest(token, expected):
        return True
    try:
        return _auth.verify_api_key(token) is not None
    except Exception:
        _LOG.exception("otto proxy: user-key verification raised")
        return False

@app.websocket("/otto/realtime")  # noqa: F821  (app is provided by part_000's shared namespace)
async def otto_realtime(websocket: WebSocket) -> None:
    """Authenticated, budget-metered relay to Azure OpenAI Realtime for the Otto app."""
    global _otto_rt_active

    # 1. Auth — shared bearer secret (constant-time compare). Reject before accept so an
    #    unauthenticated client never completes the WS handshake.
    expected = os.environ.get("OTTO_APP_TOKEN", "").strip()
    if not expected:
        await websocket.close(code=4503)  # service not configured
        return
    token = _otto_rt_bearer(websocket)
    if not _otto_proxy_auth_ok(token):
        await websocket.close(code=4401)  # invalid token
        return

    # 2. Upstream config must be present. Resolve (url, headers) for either the LiteLLM
    #    gateway (OTTO_USE_LITELLM) or direct Azure. Either way aztea stays the frame meter:
    #    the cap below reads the relayed `response.done` usage frames, which flow through
    #    unchanged regardless of upstream — it does NOT depend on LiteLLM tracking audio
    #    spend (the realtime virtual key's max_budget is a secondary backstop).
    resolved = _otto_rt_resolve_upstream()
    if resolved is None:
        await websocket.close(code=4503)  # voice not configured
        return
    upstream_url, upstream_headers = resolved

    # 3. Budget gate — refuse new sessions once the shared pool is spent.
    cap_cents = _otto_rt_budget_cap_cents()
    if await asyncio.to_thread(_otto_rt_spent_cents) >= cap_cents:
        await websocket.close(code=4402)  # at capacity
        return

    # 4. Coarse concurrency guard (per worker).
    if _otto_rt_active >= _otto_rt_max_concurrent():
        await websocket.close(code=4429)  # too many sessions
        return

    # 5. Open the upstream Azure socket BEFORE accepting the client, so an upstream
    #    failure closes the handshake cleanly instead of a half-open relay.
    try:
        upstream_cm = websockets.connect(
            upstream_url,
            additional_headers=upstream_headers,
            max_size=None,        # realtime audio frames can exceed the 1 MiB default
            open_timeout=20,
            ping_interval=20,
            ping_timeout=20,
        )
        upstream = await upstream_cm.__aenter__()
    except Exception:
        await websocket.close(code=4502)  # upstream unavailable
        return

    _otto_rt_active += 1
    await websocket.accept()
    try:
        client_task = asyncio.create_task(_otto_rt_pump_client_to_upstream(websocket, upstream))
        upstream_task = asyncio.create_task(
            _otto_rt_pump_upstream_to_client(websocket, upstream, cap_cents)
        )
        done, pending = await asyncio.wait(
            {client_task, upstream_task},
            timeout=_otto_rt_max_session_seconds(),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        _otto_rt_active -= 1
        try:
            await upstream_cm.__aexit__(None, None, None)
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
