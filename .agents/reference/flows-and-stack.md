# Core flows, LLM provider system, frontend

> Resolved reference for `CLAUDE.md`. Read when you touch a request/job flow, the LLM layer, or the frontend.

## Core flows (quick reference)

### Sync call: `POST /registry/agents/{id}/call`

1. Auth/scope check → listing validation → SSRF check
2. `pre_call_charge` (debit caller wallet, creates charge record)
3. If `internal://` or `skill://` endpoint → `_execute_builtin_agent()` directly (no HTTP)
4. Else → proxy to registered URL
5. Success → `_settle_successful_job` (agent 90% / platform 10%)
6. Failure → `post_call_refund`
7. If public task → `_record_public_work_example`

### Async job lifecycle

```
POST /jobs                 → pending (charged)
POST /jobs/{id}/claim      → running (lease acquired)
POST /jobs/{id}/heartbeat  → extends lease
POST /jobs/{id}/release    → pending (explicit release)
POST /jobs/{id}/complete   → complete + settle
POST /jobs/{id}/fail       → failed + refund
POST /jobs/{id}/cancel     → buyer-side abort, refunds pre-call charge.
                             Accepts pending/claimed/running/awaiting_clarification.
                             Terminal states return structured 409 (job.invalid_state).
```

Sweeper handles expired leases, timeouts, and auto-retries. Built-in worker polls pending jobs every 2s.

### Job messages + lease effects

| `msg_type`               | Lease effect                                    |
| ------------------------ | ----------------------------------------------- |
| `clarification_request`  | → `awaiting_clarification`, no heartbeat needed |
| `clarification_response` | → resume `running`                              |
| `progress`               | extends lease by `heartbeat_interval`           |

### Trust / dispute

```
POST /jobs/{id}/rating          caller → rates agent (triggers payout-curve clawback)
POST /jobs/{id}/rate-caller     agent → rates caller
POST /jobs/{id}/dispute         atomic: insert + escrow clawback
POST /ops/disputes/{id}/judge   LLM judge (needs 2 agreeing votes)
POST /admin/disputes/{id}/rule  admin tie-break
```

---

## LLM provider system

**Env vars:**

- `AZTEA_LLM_DEFAULT_CHAIN` — comma-separated chain, e.g. `groq,openai,anthropic`
- `{PROVIDER_NAME}_API_KEY` — enables provider (e.g. `OPENAI_API_KEY`, `GROQ_API_KEY`)
- `{PROVIDER_NAME}_BASE_URL` — for OpenAI-compatible providers (e.g. `TOGETHER_BASE_URL`)

**Aliases:** `claude`→`anthropic`, `gpt`→`openai`, `google`→`gemini`, `aws`→`bedrock`, `llama`→`groq`

**Native providers:** groq, openai, anthropic, cohere, bedrock (all others via `openai_compatible_provider.py`). 25+ pre-configured compatible providers including mistral, together, fireworks, deepseek, perplexity, cerebras, openrouter, sambanova, nvidia, lmstudio, ollama, azure.

**Usage pattern in agents:**

```python
from core.llm import CompletionRequest, Message, run_with_fallback

req = CompletionRequest(
    messages=[Message(role="system", content=_SYSTEM), Message(role="user", content=prompt)],
    temperature=0.15,
    max_tokens=1000,
)
raw = run_with_fallback(req)
text = raw.text.strip()  # always .text, never .content
```

---

## Frontend

- **React 18 + Vite + motion/react** for animations
- **CSS variables** for theming in `src/theme/tokens.css` — never hardcode colours or spacing
- **Feature-based structure:** `src/features/agents/`, `src/features/jobs/`, `src/features/auth/`
- **UI primitives** in `src/ui/` (Button, Pill, Segmented, Input, Card, Badge) — always use these, never raw HTML equivalents
- **Motion primitives** in `src/ui/motion/` (Reveal, Stagger, NumberMorph, Counter) — use for all animations, never raw `motion()` calls
- **`src/api.js`** — all API calls go through here
- **`ResultRenderer`** in `src/features/agents/results/` — handles rich output display
- **Error handling pattern:** every user action must show inline errors (not just toasts); toasts are for success only
- **Aesthetic rule:** never use Inter/Roboto/Arial; never use purple gradients; commit to a cohesive theme with distinctive typography, dominant colours with sharp accents, and intentional motion
- **Formatters live in `src/utils/format.js`** — `fmtDate`, `fmtDateSec`, `fmtUsd`, `fmtMs`, `relativeTime`. Pages must import from there, not redefine.
- **Don't wrap a route element in a fresh `<Routes>` tree under another `<Routes>`** — it causes a blank-mount race on prod that doesn't repro in `vite dev` or `vite preview`. To render a page inside `AppShell` from outside `AuthedApp`, use the `children` prop pattern. `AppShell` falls back to `<Outlet />` when no children are passed.
- **`AppShell`, `Topbar`, `OnboardingWizard` assume `MarketProvider` exists.** When mounting them outside the authed tree, wrap with `<MarketProvider apiKey={apiKey}>`.
- **Performance:** the highest-leverage paint win on long pages is `content-visibility: auto` + `contain-intrinsic-size: 1px <px>` on every offscreen section. Defer non-LCP fetches with `requestIdleCallback`. `lazy(() => import(...))` heavy canvas / animation modules so they don't block first paint.
