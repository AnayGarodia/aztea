# Hosted execution — operational runbook

This runbook covers the Wave-3 hosted-code-execution surface: the
browser playground (`POST /api/playground/test`, `POST /api/playground/publish`)
and every hosted-skill invocation that flows through
`core/skill_executor.py` and `agents/python_executor.py`.

Read this before the playground goes to public traffic. Update it every
time you change the kill-switch behaviour, the audit-log schema, or the
escape-test suite.

## 1. What the surface is

```
                                    ┌─────────────────────────┐
buyer code  ──► /api/playground ──► │ core/listing_safety    │
                                    │  + listing_safety_judge │
                                    │  (LLM intent review)    │
                                    └────────────┬────────────┘
                                                 │  no BLOCK
                                                 ▼
                                    ┌─────────────────────────┐
                                    │ agents.python_executor  │
                                    │  - regex pre-filter     │
                                    │  - subprocess + audit   │
                                    │  - RLIMIT_AS, timeout   │
                                    └────────────┬────────────┘
                                                 │
                                                 ▼
                                    ┌─────────────────────────┐
                                    │ hosted_execution_log    │
                                    │  (migration 0072)       │
                                    └─────────────────────────┘
```

The PEP 578 audit hook is the primary guard. The static regex
pre-filter is fast-path defence-in-depth. The LLM judge is a
semantic-intent guard layered on top. Every successful or killed
invocation lands in `hosted_execution_log`.

## 2. Monitoring — what to watch

1. **Kill rate per agent.** Spike in `was_killed = 1` for one
   `skill_id` over a 5-min window usually means an honest publisher
   shipped a regression or someone is probing the sandbox.

   ```sql
   SELECT skill_id, kill_reason, COUNT(*) AS n
   FROM hosted_execution_log
   WHERE was_killed = 1
     AND created_at >= datetime('now', '-1 hour')
   GROUP BY skill_id, kill_reason
   ORDER BY n DESC
   LIMIT 20;
   ```

2. **Anonymous probe volume per IP.** The `/api/playground/test`
   endpoint is IP-rate-limited at 5/min for anonymous calls. Spikes in
   `surface = 'playground_test'` with no `caller_owner_id` flag a
   coordinated abuse attempt. Aggregate by the X-Forwarded-For header
   captured in app logs (the audit table intentionally does NOT
   persist IPs to limit retained PII).

3. **Resource-ceiling hits.** Healthy traffic sits well below the
   defaults (`_MAX_MEMORY_MB = 128`, `_DEFAULT_TIMEOUT_S = 10`). A run
   that consistently exits at 124 (timeout) or with memory pressure
   is either a legitimate slow handler that needs `live_sandbox`, or
   a payload trying to map the ceilings:

   ```sql
   SELECT skill_id, AVG(execution_time_ms) AS avg_ms,
          AVG(peak_memory_mb) AS avg_mb, COUNT(*) AS n
   FROM hosted_execution_log
   WHERE created_at >= datetime('now', '-24 hours')
   GROUP BY skill_id
   HAVING avg_ms > 5000 OR avg_mb > 64
   ORDER BY avg_ms DESC;
   ```

4. **Judge-block rate.** If `core/listing_safety_judge.py` is
   refusing > 1% of new publishes, either a new attack class has
   appeared or the judge's confidence floor needs tuning. Pull the
   refused samples (the judge logs at INFO with the verdict +
   reasoning) before tuning.

## 3. Killing an agent (incident response)

```
POST /admin/agents/{agent_id}/suspend
Authorization: Bearer <admin-key>
{
  "reason": "Suspected sandbox-escape probe — investigating."
}
```

Effect:
- Sets `agents.status = 'suspended'` (reversible — see ban below for
  terminal).
- Fails every open job (`pending` / `running` /
  `awaiting_clarification`) routed to the agent.
- Refunds the caller's escrow on each failed job. The refund summary
  comes back in `kill_switch_summary.refunded_jobs`.
- Emits a `WARNING admin.agent.suspend …` log line with actor,
  reason, and counts. Grep prod logs for the audit trail.

To **re-enable** the agent after investigation:

```python
# Via the admin shell or a one-off script — there's no public re-
# enable endpoint by design (forces operator review).
from core.registry.agents_ops import set_agent_status
set_agent_status("<agent_id>", "active")
```

To **terminally ban** (use only when the agent is confirmed malicious
or the publisher has been notified):

```
POST /admin/agents/{agent_id}/ban
Authorization: Bearer <admin-key>
```

`ban` is identical to `suspend` in behaviour except the status is
non-reversible without operator intervention and the agent disappears
from the public catalog.

## 4. Investigating an abuse report

1. **Pull the suspect window.** Find every invocation for the
   reported skill_id (or caller_owner_id):

   ```sql
   SELECT execution_id, surface, caller_owner_id, skill_id,
          execution_time_ms, peak_memory_mb, sandbox_exit_code,
          was_killed, kill_reason, input_hash, output_hash, created_at
   FROM hosted_execution_log
   WHERE skill_id = '<skill_id>'
      OR caller_owner_id = '<owner_id>'
   ORDER BY created_at DESC
   LIMIT 200;
   ```

2. **Correlate input hashes.** A repeated `input_hash` across calls
   means the caller is hitting the same probe over and over. A repeated
   `input_hash` across DIFFERENT `caller_owner_id` rows means the same
   probe is being tested from multiple accounts — likely coordinated
   abuse, not honest experimentation.

3. **Pull the raw source.** The audit table only carries hashes. To
   see the actual payload, pull from `hosted_skills` (for published
   skills) or replay the request from the access log if available:

   ```sql
   SELECT skill_id, source_md, created_at
   FROM hosted_skills
   WHERE skill_id = '<skill_id>';
   ```

4. **Re-run the listing-safety scan on the current source.** If the
   judge originally allowed it, that's a calibration miss — file a
   ticket against the judge prompt:

   ```python
   from core.listing_safety import scan_python_handler
   findings = scan_python_handler(open('suspect_handler.py').read())
   for f in findings: print(f)
   ```

5. **Decide:** kill-switch the agent (section 3), notify the
   publisher (manual email — there is no automated notification path
   today), or escalate to a security-bounty disclosure if a real
   sandbox escape is suspected (section 5).

## 5. Suspected sandbox escape

If you find evidence of an actual escape (data exfiltration, file
read outside the sandbox, network egress past the audit hook),
**treat as a P0 incident:**

1. **Kill switch every agent published by the same `owner_id`.** Use
   the SQL above to find them all; suspend each via the admin endpoint.

2. **Stop the playground.** Set `AZTEA_PLAYGROUND_ENABLED=0` and
   redeploy. (The endpoints check this flag at request time.)

3. **Run the escape suite.** `pytest tests/security/test_sandbox_escape.py`
   must still pass 100% — if it doesn't, the escape vector is
   reproducible and you have a regression test free.

4. **Capture forensics.** The host runs subprocess containers with
   audit hooks; pull `journalctl -u aztea` and `dmesg` around the
   reported time, plus everything from `hosted_execution_log` in the
   window:

   ```bash
   sqlite3 data/aztea.db "SELECT * FROM hosted_execution_log \
     WHERE created_at BETWEEN '<start>' AND '<end>' \
     ORDER BY created_at" > forensics-$(date +%Y%m%d-%H%M).csv
   ```

5. **Notify (if material).** If buyer data could have been exfiltrated,
   trigger the breach-notification workflow. Owners of any affected
   skill IDs need to know.

6. **Patch.** Add the reproducer to
   `tests/security/test_sandbox_escape.py`. Patch the audit hook or
   static filter. Re-enable the playground only after the new test
   passes AND a second engineer has reviewed the patch.

## 6. Log analysis playbook

`hosted_execution_log` answers "what happened?". App logs (stdout /
journalctl) answer "why?". Correlate via `execution_id` when it
matters.

Useful one-liners:

```bash
# Last 50 kill events with their reasons
sqlite3 -header -column data/aztea.db "
  SELECT created_at, surface, kill_reason, skill_id
  FROM hosted_execution_log
  WHERE was_killed = 1
  ORDER BY created_at DESC LIMIT 50;
"

# Per-hour kill rate over the last day
sqlite3 -header -column data/aztea.db "
  SELECT strftime('%Y-%m-%d %H:00', created_at) AS hour,
         SUM(was_killed) AS kills, COUNT(*) AS total,
         ROUND(100.0 * SUM(was_killed) / COUNT(*), 2) AS pct
  FROM hosted_execution_log
  WHERE created_at >= datetime('now', '-24 hours')
  GROUP BY hour ORDER BY hour;
"

# Most-killed agents
sqlite3 -header -column data/aztea.db "
  SELECT skill_id, COUNT(*) AS kills
  FROM hosted_execution_log
  WHERE was_killed = 1 AND created_at >= datetime('now', '-7 days')
  GROUP BY skill_id ORDER BY kills DESC LIMIT 20;
"
```

## 7. Pre-launch hard gate

Before turning the playground on for public traffic, all of the
following must be true:

- [ ] `pytest tests/security/test_sandbox_escape.py` is **100% green**.
- [ ] At least one engineer other than the playground author has done
      an internal red-team review (one hour, focused on the new
      attack surface).
- [ ] `AZTEA_PLAYGROUND_ENABLED` is set in the prod env.
- [ ] `AZTEA_LISTING_JUDGE=on` is set (the LLM judge is enabled).
- [ ] An admin user knows where this runbook lives.
- [ ] The kill-switch endpoint has been exercised against a test
      agent on staging — verified the refund actually credits the
      caller wallet.

If any one of those is unchecked, do not turn it on.

## 8. Acceptable known limitations

These are documented gaps. Operators should know them, not be
surprised by them:

- **`/proc/version` and `/proc/cpuinfo` leak host kernel info.** runc
  refuses bind mounts inside /proc, so the host-mask layer can't
  cover those two files without gVisor. Migrate to gVisor
  (`AZTEA_SANDBOX_BACKEND=gvisor`) to close the leak — see
  `core/sandbox/isolation.py` 2026-05-18 comment.

- **DNS resolution is blocked at the audit-hook layer, not at the
  network layer.** A native-code path that bypassed the hook (via
  `ctypes` — also blocked, but defence-in-depth matters) could
  resolve. The gVisor backend's egress allowlist closes this.

- **Hashing in the audit log is non-keyed.** Two callers running the
  same payload produce the same `input_hash`. This is intentional
  (correlation tool), not a leak — but treat the hash itself as
  caller-identifying in any export.
