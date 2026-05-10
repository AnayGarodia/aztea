# OSS vs hosted Aztea

Aztea is **Apache-2.0** open source. The runtime is yours, free, forever — fork it, ship it, embed it. A few centralized services that burn LLM API credits or rely on the aztea.ai network stay paid, but you never *need* them: the OSS instance is fully functional standalone.

This document is the source-of-truth for what runs where.

---

## Quick reference

| Service                          | Local (OSS, free)                                                  | Hosted (paid via aztea.ai)                                       |
| -------------------------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------- |
| Agent runtime + ledger + jobs    | ✅ full                                                             | ✅ full                                                           |
| Built-in agents                  | ✅ all 34 curated (you provide LLM keys)                            | ✅ same agents, we provide LLM credits, metered                   |
| MCP server (Claude Code, etc.)   | ✅ full                                                             | ✅ full                                                           |
| Insert-only ledger + wallets     | ✅ real local ledger (atomic, race-guarded, reconcilable)           | ✅ same ledger + Stripe Checkout top-ups + Stripe Connect payouts |
| Dispute filing + state machine   | ✅ full                                                             | ✅ full                                                           |
| Dispute judge                    | ✅ local LLM (your keys) OR deterministic keyword fallback          | ✅ aztea.ai's tuned judge, our LLM credits                        |
| Public registry / discovery      | Local-only (your instance)                                         | List your agent on aztea.ai's public marketplace                 |
| Cross-instance trust scores      | Local trust math (per-instance)                                    | Federated trust read-on-demand via `GET /registry/agents/{id}/global-trust` (501 in OSS). Auto-merge into local trust score is a backlog item. |
| Real money in/out (Stripe)       | ❌ topup/withdraw routes return 501                                 | ✅ Stripe Checkout topup, `stripe.Transfer` payouts to agent owners |

---

## How the switch works

A single env var toggles hosted-mode:

```bash
# OSS-mode (default — fully self-contained)
unset AZTEA_HOSTED_API_URL
unset AZTEA_HOSTED_API_KEY

# Hosted-mode (calls out to aztea.ai for paid services)
export AZTEA_HOSTED_API_URL=https://api.aztea.ai
export AZTEA_HOSTED_API_KEY=<from your aztea.ai account>
```

When `AZTEA_HOSTED_API_URL` is **unset**, the codebase guarantees:

- No outbound HTTP call goes to aztea.ai (or anywhere else outside the LLM provider you configured + the agents you registered).
- Stripe routes return `501 Not Implemented` with a JSON pointer to hosted aztea.ai or a link to configure your own Stripe.
- The dispute judge uses either a local LLM (if `AZTEA_ENABLE_LIVE_DISPUTE_JUDGES=1` and an LLM key is configured) or a deterministic keyword-based fallback. Disputes never strand.
- The publish-to-public-registry endpoint returns `501`.
- The federated trust endpoint returns `501`. The local trust score is still available on the regular agent endpoint.

When `AZTEA_HOSTED_API_URL` is **set**:

- Dispute judging tries the hosted endpoint first; falls back to local on any error.
- Built-in agents marked `prefer_hosted` (currently: code review, arxiv research, web researcher, quality judge, AI red teamer) try the hosted endpoint first; fall back to local on any error.
- Stripe routes are reachable when `STRIPE_SECRET_KEY` is also configured.
- `/registry/agents/{id}/publish` syndicates the spec to aztea.ai's public catalog.
- Caller and quality ratings are pushed to aztea.ai's federated cache fire-and-forget (`core/reputation.py::_push_rating_to_hosted_async`). Local IDs are HMAC-hashed before they leave the instance.
- `/registry/agents/{id}/global-trust` proxies the federated trust score (read-on-demand). Note: today the local `compute_trust_metrics()` does not auto-blend the global score into the canonical `trust_score` field — callers that want federated signal need to read both endpoints. Auto-merge is tracked in `.agents/TODO.md` backlog.

---

## Where the OSS / hosted boundary lives in the code

| File                                       | Purpose                                                              |
| ------------------------------------------ | -------------------------------------------------------------------- |
| `core/feature_flags.py`                    | `hosted_mode_enabled()`, `stripe_enabled()` — single source of truth |
| `core/hosted_client.py`                    | The only module that makes outbound calls to api.aztea.ai            |
| `core/judges.py::_try_hosted_judgment`     | Hosted-first branch in the dispute judge                             |
| `server/application_parts/part_004.py`     | `_try_hosted_builtin_agent` — hosted-first dispatch for `prefer_hosted` agents |
| `server/application_parts/part_008.py`     | `/registry/agents/{id}/publish` and `/global-trust`                  |
| `server/application_parts/part_013.py`     | Stripe routes (gated by `_stripe_unavailable_error`)                 |
| `server/builtin_agents/constants.py`       | `PREFER_HOSTED_AGENT_IDS` — the opt-in set                           |
| `core/reputation.py::_push_rating_to_hosted_async` | Fire-and-forget rating push                                  |

If you're contributing and you need to add a new hosted service, route it through `core/hosted_client.py`. Never make a direct outbound call to `aztea.ai` from anywhere else in the codebase.

---

## Why this model (and not "10% of every transaction")

The original idea was a 10% transaction fee. That's not enforceable on self-hosters because they own the database — they can patch the fee out in a one-line change. Charging instead for **services that burn our LLM credits** (judges, hosted agents) and for **services that require the aztea.ai network** (public registry, federated trust) is enforceable: those calls require valid credentials against our hosted API, which we control.

Self-hosters who only need the local runtime never pay anything. Self-hosters who want the hosted services can opt in incrementally — pay for hosted judges only, or hosted agents only, or just publish to the public registry. The 10% commission still applies, but only on traffic that flows through aztea.ai's public marketplace.

---

## OSS sanity check

Before you deploy, verify your instance is genuinely OSS-mode-only:

```bash
make oss-check
```

Or manually:

```bash
# Boot with no hosted, no Stripe
unset AZTEA_HOSTED_API_URL STRIPE_SECRET_KEY
export GROQ_API_KEY=<your-key>
export API_KEY=test-master-key
uvicorn server:app --port 8000 &

# Stripe routes 501 with hosted pointer
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/wallets/topup/session
# Expect: 401 (auth required) or 501 with auth — both indicate Stripe is gated

# Publish endpoint 501s
curl -s -X POST http://localhost:8000/registry/agents/00000000-0000-0000-0000-000000000000/publish \
  -H "Authorization: Bearer test-master-key"
# Expect: 404 (agent not found) — confirms route is registered but no hosted call attempted

# No outbound aztea.ai calls
grep -rn 'aztea\.ai' core/ server/ agents/ | grep -v test | grep -v '#'
# Expect: only the documented hosted-mode and 501-pointer references
```
