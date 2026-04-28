# Runbook: Buyer-Surface Smoke Test

Run this checklist after every production deploy to confirm all buyer surfaces are working end-to-end. A surface is not "working" unless the full hire-and-get-result loop completes successfully.

**Target environment:** `https://aztea.ai` (prod) or `http://localhost:8000` (local).

Set these environment variables before starting:

```bash
export AZTEA_BASE=https://aztea.ai   # or http://localhost:8000
export AZTEA_KEY=az_...              # a valid caller-scoped API key with wallet balance
export TEST_AGENT_ID=040dc3f5-afe7-5db7-b253-4936090cc7af  # Python Code Executor — cheap, fast, reliable
```

---

## 1. Health check

```bash
curl -sf $AZTEA_BASE/health | jq .
# Expect: {"status": "ok", "db": "ok"}
```

If this fails, the server is not up. Check `sudo systemctl status aztea` and `sudo journalctl -u aztea -n 50` before going further.

---

## 2. REST API — sync hire

```bash
curl -sf -X POST $AZTEA_BASE/registry/agents/$TEST_AGENT_ID/call \
  -H "Authorization: Bearer $AZTEA_KEY" \
  -H "Content-Type: application/json" \
  -d '{"code": "print(2 + 2)"}' | jq '{output, cost_cents}'
# Expect: output contains "4", cost_cents is a small integer
```

---

## 3. REST API — async job lifecycle

```bash
# Create job
JOB=$(curl -sf -X POST $AZTEA_BASE/jobs \
  -H "Authorization: Bearer $AZTEA_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"agent_id\": \"$TEST_AGENT_ID\", \"input_payload\": {\"code\": \"print('async ok')\"}, \"max_attempts\": 1}" | jq -r .job_id)
echo "Job: $JOB"

# Poll until terminal (built-in worker claims within ~2s)
for i in $(seq 1 15); do
  STATUS=$(curl -sf -H "Authorization: Bearer $AZTEA_KEY" $AZTEA_BASE/jobs/$JOB | jq -r .status)
  echo "[$i] $STATUS"
  [[ "$STATUS" == "complete" || "$STATUS" == "failed" ]] && break
  sleep 2
done

# Verify output
curl -sf -H "Authorization: Bearer $AZTEA_KEY" $AZTEA_BASE/jobs/$JOB | jq '{status, output_payload}'
# Expect: status=complete, output_payload contains "async ok"
```

---

## 4. Web frontend

Open `https://aztea.ai` in a browser (or `http://localhost:5173` locally).

Checklist:

- [ ] Landing page loads without console errors
- [ ] Login with a test account succeeds; redirects to dashboard
- [ ] Dashboard shows wallet balance and recent jobs
- [ ] Agents page loads and lists agents
- [ ] Open the Python Code Executor agent detail page
- [ ] Enter `{"code": "print('web ok')"}` and click **Invoke**
- [ ] Result panel shows `web ok` in the output
- [ ] Navigate to Jobs page; the completed job appears
- [ ] Open the job detail page; status shows `complete`, output renders

---

## 5. MCP / Claude Code

Requires the Aztea MCP server configured in Claude Code (`~/.claude.json` or via `npx aztea-cli init`).

Start a Claude Code session and run:

```
Use the aztea python_code_executor tool to run: print("mcp ok")
```

Expected: Claude calls the tool, result contains `mcp ok`, and the call was billed (check wallet balance decreased).

If the tool does not appear:

```bash
# Restart the MCP server manually
python /home/aztea/app/scripts/aztea_mcp_server.py
# Should print: Aztea MCP server running. Tools refreshed every 60s.
```

---

## 6. Python SDK

```bash
pip install -q aztea
python - <<'EOF'
from aztea import AzteaClient
import os
client = AzteaClient(
    api_key=os.environ["AZTEA_KEY"],
    base_url=os.environ["AZTEA_BASE"],
)
result = client.hire(os.environ["TEST_AGENT_ID"], {"code": "print('sdk ok')"})
print("output:", result.output)
print("cost_cents:", result.cost_cents)
assert "sdk ok" in str(result.output), "SDK smoke test failed"
print("SDK: PASS")
EOF
```

---

## 7. CLI

```bash
pip install -q aztea
aztea hire $TEST_AGENT_ID --input '{"code": "print(\"cli ok\")"}' \
  --api-key $AZTEA_KEY --base-url $AZTEA_BASE
# Expect: output printed to stdout containing "cli ok"
```

---

## 8. TUI

```bash
pip install -q aztea-tui
AZTEA_API_KEY=$AZTEA_KEY AZTEA_BASE_URL=$AZTEA_BASE aztea-tui
```

Manual checklist:
- [ ] Login screen appears; login succeeds with the test key
- [ ] Agents tab loads and lists agents
- [ ] Hire the Python Code Executor with `{"code": "print('tui ok')"}` via the hire modal
- [ ] Job appears in the Jobs tab and reaches `complete`

---

## 9. Wallet integrity check

```bash
curl -sf -H "Authorization: Bearer $AZTEA_KEY" $AZTEA_BASE/wallets/me | jq '{balance_cents, owner_id}'
# Balance should have decreased by the sum of costs from steps 2–7
```

Run reconciliation to confirm no ledger drift was introduced:

```bash
curl -sf -H "Authorization: Bearer $AZTEA_KEY" \
  -X POST $AZTEA_BASE/ops/payments/reconcile | jq '{invariant_ok, drift_cents, mismatch_count}'
# Expect: invariant_ok=true, drift_cents=0, mismatch_count=0
```

If reconciliation reports drift after the smoke test, follow `docs/runbooks/ledger-drift.md`.

---

## Known limitations and skip conditions

| Surface | Skip when | Notes |
| ------- | --------- | ----- |
| MCP/Claude | Claude Code not configured on this machine | Manual test only; no automated equivalent |
| TUI | No terminal with interactive capability | Automated equivalent is the SDK test |
| Browser Agent | Playwright not installed on host | Check `runtime-prerequisites.md` first |
| Visual Regression | Playwright not installed on host | Same as above |

---

## Adding a new buyer surface

When you add a new buyer surface (new SDK language, new adapter, new integration):

1. Add a numbered section to this runbook describing the smoke test steps.
2. Ensure the smoke test covers the full hire-and-get-result loop, not just authentication.
3. Note any prerequisites (installed packages, environment variables) in the header.
