# Security policy

## Reporting a vulnerability

Please email **security@aztea.ai** with details. Do not open a public GitHub issue, GitHub discussion, or post in any chat channel until a fix is published.

Include:

- A description of the issue and its impact.
- Steps to reproduce, ideally with a minimal proof-of-concept.
- Any version, commit SHA, or environment details that matter.
- Whether you intend to disclose publicly, and a target date if so.

We will:

- Acknowledge receipt within **2 business days**.
- Confirm or push back on severity within **5 business days**.
- Work toward a fix and a coordinated disclosure timeline. We aim for 90 days but may need longer for issues touching the ledger, identity, or payments paths.
- Credit you in the release notes (if you'd like).

If you do not hear back within 5 business days, please escalate by emailing **founders@aztea.ai**.

## Scope

In-scope:

- The Aztea server (`server/`, `core/`, `agents/`, `migrations/`).
- The MCP server (`scripts/aztea_mcp_server.py`).
- The SDKs (`sdks/python-sdk/`, `sdks/typescript/`).
- The frontend (`frontend/`).
- Any code in this repository.
- Hosted aztea.ai (separate scope; same email).

Out of scope:

- Third-party LLM provider vulnerabilities (report to the provider).
- Issues requiring physical access to the server host.
- Self-XSS where an attacker tricks a user into pasting a payload into their own console.
- Denial of service via resource exhaustion when the user has full root on the host (e.g. local-only deployments).
- Findings that require a fork to introduce a vulnerability.

## Coordinated disclosure

We prefer 90-day disclosure timelines but are happy to negotiate based on severity, complexity, and user impact. We will not pursue legal action against researchers acting in good faith under the [Standard Safe Harbor](https://github.com/disclose/diosclose).

## Hardening guarantees

The codebase has hard rules baked into CI to reduce common security regressions:

- All outbound URLs go through `core/url_security.py` (SSRF protection: private IPs, loopback, IPv6, URL-encoded bypass chars are blocked unless `ALLOW_PRIVATE_OUTBOUND_URLS=1` is set explicitly for development).
- API key values are never logged. Automatic redaction is in `core/logging_utils.py`; only the prefix (`az_xxx...`) is logged.
- The ledger is insert-only; transactions can never be retroactively modified (compensating entries are required).
- Money paths reject `float()` in CI.
- Migrations are idempotent and never deleted.

If you find a way around any of these, that's a high-severity report.
