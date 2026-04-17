# AgentMarket — Pre-launch TODO

Items are grouped by area and roughly prioritized within each section.
**P0** = launch blocker · **P1** = launch week · **P2** = soon after

---

## Production Readiness Assessment

### Core A2A workflow: Orchestrator → Specialist

```
DISCOVER → CONTRACT → WORK ASYNC → SETTLE → REPEAT
```

| Stage | Status | Key gap |
|---|---|---|
| **Discover** | ~95% | core signals now present (`verified`, `trust_score`, `success_rate`, call/latency stats) |
| **Contract** | 95% | `callback_url` ✓; `budget_cents` ✓; `max_spend_cents` ✓; `daily_spend_limit_cents` ✓ |
| **Work async** | 95% | claim/heartbeat/complete ✓; SSE ✓; clarification ✓; callback push + HMAC ✓ |
| **Settle** | 96% | ledger ✓; dispute ✓; 2-judge ✓; dispute-window hold/release ✓; output verification decision window ✓ |
| **Protocols** | 98% | MCP manifest + stdio framing/auth/schema hardening ✓ |
| **SDK** | 96% | sync/async hire ✓; `hire_many` ✓; `wait_for` ✓; `budget_cents` ✓; callback signing + receiver helper ✓; missing: AgentServer callback decorator sugar |

**Overall: ~97% toward a working agent-to-agent marketplace.**
Remaining gaps: mostly launch operations (infra, security/audit, legal/compliance), plus product UX polish.

---

## 1. Launch Infrastructure (P0)

### 1.1 Domain & Hosting
- [ ] **Register domain** — pick and register `agentmarket.dev` (or preferred domain); configure DNS.
- [ ] **SSL/TLS certificate** — Let's Encrypt via Certbot or managed cert (Render/Railway/Fly handle this automatically).
- [ ] **Pick hosting provider** — deploy backend to Railway / Render / Fly.io / EC2; document the chosen stack.
- [ ] **Deploy frontend** — Vercel or Netlify for the React app; or serve via nginx on the same host. Configure `VITE_API_URL` env var to point to production backend.
- [ ] **Production `.env`** — inject secrets via hosting provider's secret manager, not a committed `.env` file. Confirm `STRIPE_SECRET_KEY`, `GROQ_API_KEY`, `STRIPE_WEBHOOK_SECRET` are set.
- [ ] **CDN / static assets** — serve frontend build artifacts from a CDN (Vercel/Netlify do this automatically; otherwise configure CloudFront or Cloudflare).
- [ ] **Staging environment** — mirror of prod for pre-release smoke testing.

### 1.2 Stripe Go-Live
- [ ] **Enable Stripe Connect on dashboard** — go to `https://dashboard.stripe.com/connect` and opt in.
- [ ] **Add test balance** — Stripe Dashboard → Balance → Add to test balance → $100 (required for test transfers).
- [ ] **Switch to live Stripe keys** — replace `sk_test_...` and `pk_test_...` with live keys in production env; update `STRIPE_WEBHOOK_SECRET` for live webhook endpoint.
- [ ] **Register live webhook endpoint** — add `https://yourdomain.com/stripe/webhook` in Stripe Dashboard under live mode.
- [ ] **Complete Stripe platform profile** — business name, address, bank account for platform payouts; required before live transfers work.
- [ ] **Test end-to-end deposit + withdrawal in live mode** with a small real amount.

---

## 2. Payments & Stripe (P0)

- [x] ~~**Settlement-pending escrow (inverted escrow fix)** — successful jobs now remain unsettled during dispute window and are released by sweeper after window close; dispute resolution handles unsettled and legacy-settled paths without double settlement.~~
- [x] ~~**Dispute filing deposit** — `POST /jobs/{id}/dispute` now charges 5% (min 5¢) from filer into dispute deposit escrow; disputes persist `filing_deposit_cents`; settlement releases deposit back on win/split/void and forfeits to platform on loss.~~
- [ ] **Price float → integer migration** — `price_per_call_usd` is stored as SQLite REAL; billing math uses `Decimal(str(value))` as workaround. Add `price_per_call_cents INTEGER` column, backfill, and cut over in a single migration.
- [ ] **SSRF validation review** — `endpoint_url` and `verifier_url` go through `_is_safe_url()`; audit handling of IPv6, URL-encoded chars, and redirect chains.
- [ ] **Secrets audit** — confirm `STRIPE_SECRET_KEY`, `GROQ_API_KEY`, `STRIPE_WEBHOOK_SECRET` are never logged or returned in API responses.
- [x] ~~**Free-credits first-run path** — `POST /auth/register` already credits $1.00 (100 cents) to new wallets on registration.~~

---

## 3. Agent-to-Agent Workflows (P0)

### 3.1 Webhook Callbacks
- [x] ~~**Callback HMAC secret** — `callback_secret` field on `JobCreateRequest`; backend signs POST body with `X-AgentMarket-Signature: sha256=...` header.~~
- [x] ~~**SDK `CallbackReceiver` helper** — `CallbackReceiver(secret)` class with `@receiver.on_job_complete` decorator and `dispatch(body, sig)` method. Also exports `verify_callback_signature()`.~~
- [x] ~~**Test: end-to-end orchestrator hires specialist with callback** — agent A hires agent B, does own work, receives callback when B completes, verifies result.~~

### 3.2 Agent Discovery Signals
- [x] ~~**`output_examples` field on agent registration** — stored as JSON blob; returned in search results and agent responses.~~
- [x] ~~**`verified` badge automation** — schema column added; auto-set to 1 after verifier_url passes a quality check.~~
- [x] ~~**Search result enrichment** — `client.search_agents()` returns `total_calls`, `avg_latency_ms`, `success_rate` alongside `trust_score`.~~
- [x] ~~**`dispute_rate` on agent listings** — computed as `disputes_filed / total_calls`; returned in all agent responses via reputation enrichment.~~

### 3.3 Clarification & Verification
- [x] ~~**Clarification timeout** — if agent sends a clarification request and caller does not respond within N minutes, auto-fail or auto-proceed. `clarification_timeout_seconds` on job create.~~
- [x] ~~**Output verification hook** — caller can POST acceptance/rejection within a grace window before settlement finalizes. Rejection auto-opens dispute; expired window auto-accepts.~~
- [x] ~~**Parent/child job linkage + cascade policy** — add `parent_job_id` on child jobs and explicit behavior for parent fail (fail children vs detach).~~
- [x] ~~**Frontend clarification UI** — JobDetailPage shows pending clarification requests with inline response form.~~

### 3.4 A2A & Protocol Interoperability
- [x] ~~**MCP hardening** — tool schemas now use snake_case and stdio framing/auth parsing has dedicated regression coverage.~~

---

## 4. Security (P0/P1)

- [ ] **SSRF validation review** — audit `_is_safe_url()` for IPv6 addresses, URL-encoded characters, and redirect chains on `endpoint_url` and `verifier_url`.
- [ ] **Secrets in env** — audit that `STRIPE_SECRET_KEY`, `GROQ_API_KEY`, `STRIPE_WEBHOOK_SECRET` are never logged or returned in API responses.
- [ ] **Dependency audit** — run `pip-audit` and `npm audit`; resolve HIGH/CRITICAL CVEs before launch.
- [x] ~~**Rate-limit auth endpoints** — already at 10/minute per IP via `_AUTH_RATE_LIMIT`.~~

---

## 5. Infrastructure & Reliability (P0/P1)

### 5.1 Database
- [ ] **SQLite → Postgres migration path** — SQLite with WAL is fine for early beta but document the migration path; add `DATABASE_URL` env var abstraction in `core/db.py`.
- [ ] **Automated database backups** — daily SQLite backup to S3/object storage; test restore procedure.
- [ ] **Connection pool tuning** — current thread-local pool may exhaust under concurrent load; add connection limit and queue timeout.

### 5.2 Deployment
- [x] ~~**Production Docker image** — non-root user, HEALTHCHECK, gunicorn + uvicorn workers with configurable `WEB_CONCURRENCY`/`GUNICORN_TIMEOUT`.~~
- [x] ~~**Graceful shutdown** — lifespan now signals worker stop events, drains in-flight HTTP requests, and joins background threads with configurable shutdown timeouts.~~
- [x] ~~**Process supervision** — Dockerfile uses gunicorn + UvicornWorker; `WEB_CONCURRENCY`, `GUNICORN_TIMEOUT`, `GUNICORN_MAX_REQUESTS` env vars configurable.~~

### 5.3 Observability
- [ ] **Error tracking** — integrate Sentry (`sentry-sdk[fastapi]` backend + `@sentry/react` frontend); capture unhandled exceptions with request context.
- [ ] **Structured log aggregation** — ship JSON logs to Datadog / Logtail / CloudWatch; set up alerts on error rate spikes.
- [ ] **Metrics endpoint** — expose `/metrics` (Prometheus-compatible) for request latency p50/p95/p99, job queue depth, settlement failure rate, wallet balance totals.
- [ ] **Uptime monitoring** — external health check pinging `/health` every 60s; alert on 2 consecutive failures.

---

## 6. Notifications & Email (P1)

- [ ] **Pick transactional email provider** — SendGrid, Postmark, or Resend; add `SMTP_*` / `SENDGRID_API_KEY` env var.
- [ ] **Email on job complete** — notify caller when their job finishes (include output summary and cost).
- [ ] **Email on deposit confirmed** — receipt after Stripe `checkout.session.completed`.
- [ ] **Email on dispute opened/resolved** — notify both parties.
- [ ] **Email on withdrawal processed** — confirmation with amount and expected arrival.
- [ ] **Welcome email** — sent on `POST /auth/register`; include quickstart link.
- [ ] **User notification preferences** — allow users to opt out of non-critical emails.

---

## 7. Frontend (P1)

### 7.1 Missing Pages / Flows
- [x] ~~**Job detail page** — now includes status timeline, output payload rendering, explicit clarification thread with response form, and rating/dispute actions.~~
- [x] ~~**Agent detail page** — now includes public profile metrics (trust, pricing, reliability stats) and `output_examples` rendering.~~
- [ ] **Onboarding flow** — 3-step wizard for new users: (1) fund wallet, (2) browse agents, (3) make first call. Skip if already active.
- [ ] **API key management page** — list keys, create new (with scopes), revoke, show prefix only after creation with copy-once warning.

### 7.2 UX Polish
- [ ] **Empty states** — every list (agents, jobs, transactions) needs a helpful empty state with a CTA.
- [ ] **Loading skeletons** — replace spinners with skeleton screens on WalletPage, AgentListPage, JobsPage.
- [ ] **Toast / notification system** — replace inline error `<p>` tags with a toast stack; success toasts on hire, deposit, withdraw.
- [ ] **Mobile responsiveness** — test all pages at 375px, 768px, 1280px; fix layout breaks.
- [ ] **Keyboard navigation** — all modals and dropdowns must be keyboard-accessible (Escape to close, Tab to navigate).

### 7.3 Agent Discovery
- [ ] **Agent cards** — show trust score badge, avg response time, call count, success rate, pricing prominently.
- [ ] **Output examples on agent detail** — show 2-3 real input→output pairs so buyers can judge quality before hiring.

---

## 8. SDK (P1)

### 8.1 Python SDK
- [ ] **Callback receiver helper** — `AgentServer.on_job_complete(secret)` decorator validates HMAC and fires handler.
- [ ] **Publish to PyPI** — add GitHub Actions release job that publishes on tag push.

### 8.2 TypeScript / JavaScript SDK
- [x] ~~**`sdks/typescript/`** — `AgentMarketClient` now exposes high-level `hire()`, `hireMany()`, `search()`, `getWallet()`, `getBalance()`, and `deposit()`.~~
- [ ] **Publish to npm** as `agentmarket`.
- [ ] **Node.js + browser compatibility** — use `fetch` API; no Node-specific imports.

---

## 9. Agents & Quality (P1/P2)

### 9.1 Built-in Agents
- [ ] **Benchmark scores** — run fixed eval suite per built-in agent; record accuracy/quality metrics; surface as `output_examples` in registry.
- [ ] **Response time SLAs** — document expected P95 latency per agent; alert if exceeded.
- [ ] **Financial Research Agent** — add confidence score to output; real-time data where possible.
- [ ] **Code Review Agent** — support multi-file input; output structured diff comments.
- [ ] **Text Intelligence Agent** — add language detection; support batch text input.
- [ ] **Negotiation Agent** — improve multi-turn support via clarification protocol.
- [ ] **Add more agents**: Data Analyst, Legal Summarizer, Image Describer, Translation Agent.

### 9.2 Third-party Agent Registration
- [x] ~~**Agent verification flow** — `/registry/register` now auto-calls `output_verifier_url` during registration and sets `verified=1` on pass.~~
- [ ] **Agent.md spec documentation** — public guide for building agentmarket-compatible agents.
- [x] ~~**Endpoint health monitoring** — sweeper now pings external endpoint URLs and tracks degraded health state after 3 consecutive failures (with recovery resets).~~
- [ ] **Agent analytics dashboard** — agent owners see call volume, revenue, ratings, dispute rate.

---

## 10. Trust & Reputation (P1)

- [ ] **Trust score explanation** — tooltip or page explaining how trust is calculated (dispute history, ratings, call volume).
- [ ] **Minimum trust gate** — option for agent owners to reject callers below a trust threshold.
- [ ] **Dispute stats** — show dispute rate % on agent listings; agents with >10% dispute rate get a warning badge.
- [ ] **Review/rating display** — show 5-star aggregate and recent text reviews on agent detail page.

---

## 11. Legal & Compliance (P0 for real money)

- [ ] **Terms of Service** — draft and publish; cover marketplace rules, prohibited use, liability limits, dispute resolution.
- [ ] **Privacy Policy** — GDPR/CCPA compliant; data collected, retention periods, user rights.
- [ ] **Stripe Connect compliance** — users accepting payouts must agree to Stripe Connected Account Agreement; gate onboard button behind a checkbox.
- [ ] **KYC/AML for payouts** — Stripe handles via Express onboarding; document the user-facing flow.
- [ ] **Tax reporting** — Stripe Connect handles 1099-K for US payees above threshold; document this for agents.
- [ ] **Prohibited use policy** — document what agents/callers cannot do (spam, fraud, CSAM) and how violations are handled.

---

## 12. Marketing & Growth (P1/P2)

- [ ] **Landing page copy** — clear headline, value prop, pricing, and CTA for agent builders and buyers.
- [ ] **Pricing page** — public breakdown of platform fee (10%), Stripe payout fee, no subscription cost.
- [ ] **Analytics** — integrate Plausible or GA4 for traffic and conversion tracking.
- [ ] **Social accounts** — register Twitter/X, LinkedIn handles for `agentmarket`.
- [ ] **Support channel** — set up support email (`support@...`) and/or Discord server.
- [ ] **Changelog** — running changelog of notable changes starting from current state.
- [ ] **Blog / launch post** — announce on Hacker News, Product Hunt, Twitter.

---

## 13. Documentation (P1)

- [ ] **API reference** — audit every endpoint in `server.py` against `docs/api-reference.md`; update stale entries.
- [x] ~~**MCP integration guide** — `docs/mcp-integration.md` covers Claude Code, Claude Desktop, env vars, A2A vs MCP choice.~~
- [ ] **A2A integration guide** — how to configure AgentMarket as a participant in Google A2A networks.
- [ ] **Dispute guide** — for callers: how to file, timeline. For agents: how to respond.
- [ ] **`output_examples` on landing page** — show 2–3 real input→output pairs for built-in agents on the homepage/agent list. Biggest single UX improvement for cold visitors deciding whether to fund a wallet. (Backend data is now populated for built-ins; frontend component still needed.)

---

## 14. Pre-launch Checklist

- [ ] All P0 items above complete
- [ ] Full test suite passing (0 failures)
- [ ] `pip-audit` and `npm audit` clean
- [ ] Domain registered, DNS live, SSL active
- [ ] Stripe live keys configured; live deposit + withdrawal tested end-to-end
- [ ] Stripe Connect platform profile complete
- [ ] Production Docker image runs as non-root with HEALTHCHECK
- [ ] Staging environment smoke-tested
- [ ] Sentry integrated and catching errors
- [ ] Uptime monitor on `/health`
- [ ] Transactional email tested (job complete, deposit, welcome)
- [ ] ToS and Privacy Policy published and linked in footer
- [ ] At least 5 working, benchmarked built-in agents with output examples
- [x] ~~`callback_url` end-to-end tested with HMAC verification~~
- [ ] Load test: 50 concurrent job creates + completes; no 500s, ledger invariant holds

---

## 15. Model-Agnostic LLM Layer (P1 — for Codex)

### 15.0 Goal

Replace Groq hardcoding across all 9 LLM call sites with a provider-agnostic layer. Ship with 3 working providers (Groq, OpenAI, Anthropic). Let third-party agents declare their model; surface it in the marketplace. Preserve all current behavior (latency, refund semantics, fallback on rate limit) — this is a refactor, not a feature redesign.

Lock-in points today:
- 7 built-in agents (`agents/*.py`) each do `from groq import Groq` + hold a `_MODELS` list + `except _groq.RateLimitError`
- 2 call sites in `core/judges.py` (dispute judge + quality judge)
- `response_format={"type":"json_object"}` at `core/judges.py:120` is OpenAI/Groq-only; Anthropic rejects it
- No `model_provider` / `model_id` columns on the `agents` table
- `GROQ_API_KEY` is the only LLM key in `.env.example`

### 15.1 Create `core/llm/` package

**File tree to create:**

```
core/llm/
  __init__.py
  base.py
  errors.py
  registry.py
  fallback.py
  providers/
    __init__.py
    groq_provider.py
    openai_provider.py
    anthropic_provider.py
```

**`core/llm/base.py`** — pure dataclasses, no SDK imports:

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol, Literal

Role = Literal["system", "user", "assistant"]

@dataclass
class Message:
    role: Role
    content: str

@dataclass
class CompletionRequest:
    model: str              # bare model id; chain overrides this
    messages: list[Message]
    temperature: float = 0.0
    max_tokens: int | None = None
    json_mode: bool = False  # True → adapters return valid JSON; each adapter translates differently
    stop: list[str] | None = None
    timeout_seconds: float = 60.0

@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"

class LLMProvider(Protocol):
    name: str
    def is_available(self) -> bool: ...
    def complete(self, req: CompletionRequest) -> LLMResponse: ...
```

**`core/llm/errors.py`**:

```python
class LLMError(Exception):
    def __init__(self, provider: str, model: str, message: str, cause: Exception | None = None):
        super().__init__(f"[{provider}:{model}] {message}")
        self.provider = provider
        self.model = model
        self.cause = cause

class LLMRateLimitError(LLMError):
    retry_after_seconds: int | None = None

class LLMTimeoutError(LLMError): ...
class LLMAuthError(LLMError): ...
class LLMBadResponseError(LLMError): ...  # non-JSON when json_mode=True
```

**`core/llm/registry.py`**:

```python
PROVIDERS: dict[str, LLMProvider] = {}  # "groq" -> GroqProvider(), etc. — populated at module load

def get_provider(name: str) -> LLMProvider:
    """Raises KeyError if provider name not registered."""

def resolve(spec: str) -> tuple[LLMProvider, str]:
    """'groq:llama-3.3-70b-versatile' -> (GroqProvider, 'llama-3.3-70b-versatile')
    Bare 'llama-3.3-70b-versatile' -> (GroqProvider, 'llama-3.3-70b-versatile') for back-compat.
    Raises ValueError on unknown provider prefix."""

DEFAULT_CHAIN: list[str]
# Read from AGENTMARKET_LLM_DEFAULT_CHAIN env var (comma-separated "<provider>:<model>" specs).
# Falls back to: ["groq:llama-3.3-70b-versatile", "openai:gpt-4o-mini", "anthropic:claude-sonnet-4-6"]
```

**`core/llm/fallback.py`**:

```python
def run_with_fallback(
    req_template: CompletionRequest,
    model_chain: list[str] | None = None,
) -> LLMResponse:
    """Iterate model_chain (default: registry.DEFAULT_CHAIN).
    For each spec: resolve to (provider, model); skip if provider.is_available() is False;
    catch LLMRateLimitError + LLMTimeoutError + HTTP 5xx → try next.
    Return first success. Raise last LLMError if all fail.
    req_template.model is IGNORED — the chain drives model selection.
    """
```

**`core/llm/__init__.py`** re-exports:

```python
from .base import CompletionRequest, LLMResponse, Message, Usage, LLMProvider
from .errors import LLMError, LLMRateLimitError, LLMTimeoutError, LLMAuthError, LLMBadResponseError
from .fallback import run_with_fallback
from .registry import resolve, get_provider, DEFAULT_CHAIN
```

### 15.2 Implement provider adapters

Each adapter **lazy-imports its SDK inside `__init__`** and sets `self._available = False` on `ImportError` or missing API key. Never raise at module import time.

**`core/llm/providers/groq_provider.py`** (GroqProvider):

- `is_available()`: returns False if `groq` SDK not installed OR `GROQ_API_KEY` env var empty
- `complete(req)`:
  - Maps `CompletionRequest` → `client.chat.completions.create(model=req.model, messages=[...], temperature=..., max_tokens=..., timeout=req.timeout_seconds)`
  - If `req.json_mode=True`: add `response_format={"type": "json_object"}` kwarg
  - Catches `groq.RateLimitError` → raise `LLMRateLimitError`
  - Catches `groq.APITimeoutError` → raise `LLMTimeoutError`
  - Returns `LLMResponse(text=completion.choices[0].message.content.strip(), model=req.model, provider="groq", usage=Usage(...))`

**`core/llm/providers/openai_provider.py`** (OpenAIProvider):

- Same structure as Groq. OpenAI SDK: `openai.OpenAI(api_key=...).chat.completions.create(...)`
- `response_format={"type":"json_object"}` supported natively when `json_mode=True`
- Catches `openai.RateLimitError`, `openai.APITimeoutError`, `openai.AuthenticationError`

**`core/llm/providers/anthropic_provider.py`** (AnthropicProvider):

Anthropic's API differs in 3 critical ways:

1. **No `response_format`**. When `req.json_mode=True`, prepend to the system message: `"You must respond with a single valid JSON object and nothing else. No prose, no markdown fences."` If the response text is not parseable as JSON, raise `LLMBadResponseError` (don't silently pass).

2. **System messages are a separate field**. Split `req.messages`:
   - All `role="system"` messages → concatenate into a single `system` string (newline-separated)
   - Remaining messages → map as-is to `messages` list
   - If no system messages, omit the `system` kwarg entirely

3. **`max_tokens` is required**. If `req.max_tokens is None`, default to 4096.

API call: `anthropic.Anthropic(api_key=...).messages.create(model=..., system=..., messages=..., max_tokens=..., temperature=..., timeout=...)`

Response: `response.content[0].text`, `response.usage.input_tokens`, `response.usage.output_tokens`, `response.stop_reason`

Catches `anthropic.RateLimitError` → `LLMRateLimitError`, `anthropic.APITimeoutError` → `LLMTimeoutError`

### 15.3 Migrate all 9 LLM call sites

Each of the following files currently has this pattern (with minor variations):

```python
import groq as _groq
from groq import Groq
_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-70b-versatile", "llama3-70b-8192", ..., "llama-3.1-8b-instant"]

# in run():
client = Groq()
for model in _MODELS:
    try:
        resp = client.chat.completions.create(model=model, messages=msgs, ...)
    except _groq.RateLimitError: continue
```

Replace with:

```python
from core.llm import CompletionRequest, Message, run_with_fallback

# in run():
resp = run_with_fallback(CompletionRequest(
    model="",          # ignored — chain picks the model
    messages=[Message("system", _SYSTEM), Message("user", user_prompt)],
    temperature=0.0,
    max_tokens=1200,   # keep existing value per file
    json_mode=True,    # or False — see below
))
text = resp.text
```

**File-by-file checklist:**

- [ ] `agents/negotiation.py` — `json_mode=True` (output is parsed as JSON at line ~108: `return json.loads(raw)`)
- [ ] `agents/textintel.py` — check whether output is JSON-parsed; set `json_mode` accordingly
- [ ] `agents/scenario.py` — `json_mode=True`
- [ ] `agents/codereview.py` — check whether output is JSON-parsed
- [ ] `agents/wiki.py` — check whether output is JSON-parsed
- [ ] `agents/portfolio.py` — `json_mode=True`
- [ ] `agents/product.py` — same pattern
- [ ] `core/judges.py` — `_judge_once` at line ~116: `json_mode=True`. Preserve the **primary/secondary distinction** by calling `run_with_fallback` twice:
  - Primary: `model_chain=None` (uses `DEFAULT_CHAIN`)
  - Secondary: `model_chain=[DEFAULT_CHAIN[1], DEFAULT_CHAIN[2], DEFAULT_CHAIN[0]]` (rotate) so judges use different models whenever possible
  - Keep `disputes.record_judgment(... model=resp.provider + ":" + resp.model)` so judgment audit trail includes the actual provider used
- [ ] `core/judges.py` quality judge — same treatment

After each file: **delete the per-file `_MODELS` list and `import groq / from groq import Groq` lines**.

Keep `_SYSTEM`, `_USER` prompt templates, `_strip_fences`, and all other logic unchanged.

### 15.4 Schema migration

**Create `migrations/0002_agent_model_columns.sql`:**

```sql
-- Add model provider and model ID columns to agents table
ALTER TABLE agents ADD COLUMN model_provider TEXT;
ALTER TABLE agents ADD COLUMN model_id TEXT;
CREATE INDEX IF NOT EXISTS idx_agents_model_provider ON agents(model_provider);
```

Both columns are nullable — third-party agents that don't use an LLM leave them NULL.

Apply migration: `core/migrate.py` already picks up all `*.sql` files in `migrations/` ordered by name, so this runs automatically on next startup.

### 15.5 Update `core/registry.py`

- `register_agent(name, description, endpoint_url, price_per_call_usd, tags, ..., model_provider=None, model_id=None)` — add two new optional keyword args, insert into new columns
- `_serialize_agent(row)` — include `model_provider` and `model_id` in the returned dict (return `None` if column not present for back-compat during migration window)
- `get_agents(status_filter="active", tag=None, model_provider=None)` — if `model_provider` is provided, add `AND model_provider = ?` to the WHERE clause
- `search_agents(query, limit=10, tag=None, model_provider=None)` — same filter

### 15.6 Update `core/models.py`

Add to `AgentRegisterRequest`:

```python
from typing import Literal, Optional
from pydantic import Field

model_provider: Optional[Literal["groq", "openai", "anthropic", "other"]] = None
model_id: Optional[str] = Field(default=None, max_length=128)
```

Include both fields in any `AgentResponse` or `AgentSummary` Pydantic models that return agent data.

### 15.7 Update `server.py`

**Built-in agent registrations** (startup block, search for `"Financial Research Agent"`, `"Code Review Agent"`, etc.):

Add `model_provider="groq"` and `model_id="llama-3.3-70b-versatile"` to each `register_agent(...)` call.

**`GET /registry/agents`** route: add `model_provider: Optional[str] = Query(default=None)` parameter, forward to `registry.get_agents(model_provider=model_provider)`.

**`POST /registry/search`** route: add `model_provider` field to the search request body (if it has a Pydantic model) or as a query param; forward to `registry.search_agents(model_provider=model_provider)`.

### 15.8 Frontend changes

**New file: `frontend/src/components/ModelBadge.jsx`**

```jsx
const PROVIDER_COLORS = {
  groq:      { background: "#fff4e6", color: "#ad3f00" },
  openai:    { background: "#e6fbf4", color: "#0a6b4b" },
  anthropic: { background: "#f3ecff", color: "#4a2a9c" },
  other:     { background: "#f1f5f9", color: "#334155" },
};

export default function ModelBadge({ provider, modelId }) {
  if (!provider) return null;
  const style = PROVIDER_COLORS[provider] || PROVIDER_COLORS.other;
  return (
    <span className="model-badge" style={style}>
      {provider} · {modelId || "—"}
    </span>
  );
}
```

Add `frontend/src/components/ModelBadge.css`:

```css
.model-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.75rem;
  font-weight: 500;
  white-space: nowrap;
}
```

**`frontend/src/pages/AgentListPage.jsx`**: Add a provider filter chip row above the agent grid:

```jsx
const PROVIDERS = ["All", "groq", "openai", "anthropic", "other"];
const [selectedProvider, setSelectedProvider] = useState("All");

// Filter chip row
<div className="provider-filters">
  {PROVIDERS.map(p => (
    <button
      key={p}
      className={`chip ${selectedProvider === p ? "active" : ""}`}
      onClick={() => setSelectedProvider(p)}
    >
      {p}
    </button>
  ))}
</div>
```

Pass `model_provider: selectedProvider === "All" ? undefined : selectedProvider` to the API call. Render `<ModelBadge provider={agent.model_provider} modelId={agent.model_id} />` on each agent card.

**`frontend/src/pages/AgentDetailPage.jsx`**: Render `<ModelBadge>` near the trust score or pricing section.

**Registration form** (whichever JSX file handles agent registration): Add two optional fields:

```jsx
<select name="model_provider">
  <option value="">— No model declared —</option>
  <option value="groq">Groq</option>
  <option value="openai">OpenAI</option>
  <option value="anthropic">Anthropic</option>
  <option value="other">Other</option>
</select>

<input
  type="text"
  name="model_id"
  maxLength={128}
  placeholder="e.g. llama-3.3-70b-versatile"
/>
```

Include both in the POST body only when filled.

### 15.9 Config and dependencies

**`.env.example`** — add after `GROQ_API_KEY`:

```bash
# LLM providers (at least one API key required for built-in agents)
GROQ_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# Optional: override fallback chain (comma-separated "<provider>:<model_id>")
# AGENTMARKET_LLM_DEFAULT_CHAIN=groq:llama-3.3-70b-versatile,openai:gpt-4o-mini,anthropic:claude-sonnet-4-6
```

**`requirements.txt`** — add:

```
openai>=1.50
anthropic>=0.39
```

(`groq` is already present.)

### 15.10 Tests to write

**New file: `tests/test_llm_providers.py`** (all cases monkeypatched — no real API calls):

- [ ] `test_registry_resolves_prefixed_spec` — `resolve("groq:llama-3.3-70b-versatile")` returns (GroqProvider, "llama-3.3-70b-versatile")
- [ ] `test_registry_resolves_bare_spec_defaults_to_groq` — back-compat
- [ ] `test_registry_raises_on_unknown_provider` — `resolve("foobar:x")` raises ValueError
- [ ] `test_default_chain_env_override` — set `AGENTMARKET_LLM_DEFAULT_CHAIN="openai:gpt-4o-mini"`, assert `DEFAULT_CHAIN == ["openai:gpt-4o-mini"]`
- [ ] `test_provider_unavailable_when_sdk_missing` — patch `groq` as un-importable; `GroqProvider().is_available()` returns False
- [ ] `test_provider_unavailable_when_key_missing` — `monkeypatch.delenv("GROQ_API_KEY")`, `GroqProvider().is_available()` returns False
- [ ] `test_fallback_skips_unavailable_providers` — mark Groq+OpenAI unavailable; only Anthropic available; verify Anthropic provider is called
- [ ] `test_fallback_retries_on_rate_limit` — first provider raises `LLMRateLimitError`; second returns success; assert result from second
- [ ] `test_fallback_raises_last_error_when_all_fail` — all providers raise; assert `LLMRateLimitError` propagates
- [ ] `test_anthropic_json_mode_injects_system_prompt` — capture `system` arg to mocked `messages.create`; assert it contains `"valid JSON object"`
- [ ] `test_anthropic_splits_system_messages` — `CompletionRequest` with 2 `role="system"` messages → assert they're concatenated into one `system` string
- [ ] `test_groq_json_mode_passes_response_format` — capture kwargs to mocked `chat.completions.create`; assert `response_format == {"type": "json_object"}`

**New file: `tests/test_agent_model_columns.py`** (use the `registry_db` fixture from `tests/test_bug_regressions.py`):

- [ ] `test_register_agent_persists_model_columns` — register with `model_provider="openai", model_id="gpt-4o-mini"`, GET back, assert both fields present
- [ ] `test_register_agent_model_columns_nullable` — register without model fields, assert both return None
- [ ] `test_get_agents_filters_by_provider` — register agent A (groq) + agent B (openai); `get_agents(model_provider="openai")` returns only B
- [ ] `test_get_agents_no_filter_returns_all` — no filter returns both
- [ ] `test_builtin_agents_registered_with_groq_provider` — after `server.py` startup (use FastAPI TestClient), fetch agent list, assert every built-in has `model_provider="groq"`

### 15.11 Rollout order (strictly sequential)

1. Land `core/llm/` package with all 3 providers + `tests/test_llm_providers.py`. No call sites migrated yet. CI must be green.
2. Migrate `core/judges.py` (2 call sites). Run full test suite. Manual smoke: file a dispute and trigger judgment.
3. Migrate all 7 `agents/*.py` files. Run full test suite. Manual smoke: make 1 call to each built-in agent.
4. Land `migrations/0002_agent_model_columns.sql` + `core/registry.py` + `core/models.py` + built-in registration updates + `tests/test_agent_model_columns.py`.
5. Land frontend: `ModelBadge`, filter chips, detail page badge, registration form fields.
6. Update `CLAUDE.md` system map to mention `core/llm/` and the model fields.

### 15.12 Non-goals (do not scope-creep)

- Streaming responses — all current call sites are single-turn; don't add streaming to `LLMProvider`
- Function/tool calling — not used anywhere
- Vision / multi-modal — text-only
- Per-caller provider override — the chain is server-wide
- Cost tracking per provider — `LLMResponse.usage` captures tokens but don't change the billing ledger
- Intra-provider retries — `run_with_fallback` only moves to the next provider; per-provider retry is a future concern

---

## 16. Structural Cleanup (completed)

- [x] Consolidated SDKs: `sdk/` → `sdks/python-sdk/`; old `sdks/python/` (raw HTTP client) retained for contract tests; `sdks/typescript/` unchanged
- [x] Moved `main.py` → `scripts/financial_cli.py`; `client.py` → `scripts/client_cli.py`
- [x] Deleted `test.py` (leftover smoke script)
- [x] Moved `openapi.json` → `docs/openapi.json`
- [x] Inlined `core/logger.py` into `scripts/financial_cli.py`; deleted `core/logger.py`
- [x] Untracked `registry.db-shm`, `registry.db-wal` (already covered by `*.db-shm` / `*.db-wal` gitignore patterns; explicitly staged removal)
- [x] Expanded `.gitignore`: `.qwen/`, `.agents/`, `skills/`, `skills-lock.json`, `.mypy_cache/`, `.pytest_cache/`
- [x] Updated `CLAUDE.md` repo map to reflect new layout
- [x] Updated `server.py` import from `from main import run` → `from scripts.financial_cli import run`
