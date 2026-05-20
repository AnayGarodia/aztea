# Runbook: Runtime Prerequisites

Which system packages each built-in agent requires, how to verify they are present, and what the agent returns when a dependency is missing.

**Update this file in the same commit as any change that adds or removes a runtime dependency.**

---

## Overview

Most Aztea built-in agents are pure Python + HTTP and have no system-level dependencies beyond the Python packages in `requirements.txt`. A subset require OS-level binaries or language runtimes that must be present on the server. Those agents return structured `tool_unavailable` errors — not 500s — when a dependency is absent, so the marketplace stays functional even if a host is partially provisioned.

---

## Dependency matrix

| Agent                    | System dependency          | Check command                          | Error when absent                        |
| ------------------------ | -------------------------- | -------------------------------------- | ---------------------------------------- |
| Browser Agent            | Playwright + Chromium      | `python -m playwright install --check` | `browser_agent.playwright_not_installed` |
| Visual Regression *(sunset 2026-05-20)* | Playwright + Chromium      | `python -m playwright install --check` | `visual_regression.playwright_not_installed` (endpoint stays wired for legacy job IDs) |
| Accessibility Auditor    | Playwright + Chromium      | `python -m playwright install --check` | `accessibility_auditor.tool_unavailable` |
| Lighthouse Auditor       | Node.js ≥ 18 + `lighthouse` CLI + Chromium | `lighthouse --version`     | `lighthouse_auditor.runtime_missing`     |
| Broken Link Crawler      | None (pure Python: httpx + bs4) | —                                  | —                                        |
| PDF Document Parser      | None (pure Python: pymupdf + pdfplumber) | —                            | `pdf_document_parser.runtime_missing` if `pymupdf` is absent |
| Web Search *(sunset 2026-05-20)* | `BRAVE_SEARCH_API_KEY` env var | `echo $BRAVE_SEARCH_API_KEY \| head -c 8` | `web_search.no_api_key` (endpoint stays wired for legacy job IDs) |
| Multi-Language Executor  | Node.js, Deno, Bun, Go, Rust | see per-language check below         | `multi_language_executor.runtime_not_available` |
| DB Sandbox               | None (stdlib `sqlite3`)    | —                                      | —                                        |
| Python Code Executor     | Python 3.10+               | `python --version`                     | —                                        |
| Linter (JS/TS) *(sunset)*     | Node.js ≥ 18 + npx    | `node --version && npx --version`      | `linter_agent.node_not_available` (Python/ruff path still works) |
| Type Checker (tsc) *(sunset)* | Node.js ≥ 18 + npx    | `npx tsc --version`                    | `type_checker.tsc_not_available` (mypy path still works) |
| Shell Executor *(sunset)*     | POSIX shell (`/bin/sh`) | `which sh`                           | Always available on Linux                |
| Multi-File Executor *(sunset)* | Python 3.10+           | `python --version`                    | —                                        |
| Multi-File Python Executor *(removed)* | n/a                | (catalog lookups return `agent.endpoint_misconfigured` until any lingering registry rows are cleaned up; no module ships) | n/a |
| Semantic Codebase Search *(removed)* | n/a                | (same — sunset 2026-05-18: never had a worker; sunsetted at the catalog layer) | n/a |
| HCL / Terraform Analyzer | `checkov` (pip, baked into image) | `checkov --version`             | `hcl_terraform_analyzer.tool_unavailable` |
| Dockerfile Analyzer      | `hadolint` (binary, baked into image) | `hadolint --version`        | falls back to regex heuristics with `degraded_mode: true` |
| Coverage Runner          | `pytest` + `coverage` (pip, baked into image) | `coverage --version`  | non-zero exit; coverage.json absent |
| CI Failure Reproducer    | `pytest`, `jest` (npm), `go` (apt), `git` (apt) | `pytest --version && jest --version && go version` | re-run reports the missing-toolchain artifact instead of the real failure |
| JWT Validator            | `PyJWT` (pip, optional)    | `python -c 'import jwt; print(jwt.__version__)'` | (validator runs without it; `signature_valid` stays `null`) |

All other agents (CVE Lookup, DNS Inspector, Dependency Auditor, etc.) require only standard network access and the Python packages in `requirements.txt`. Sunsetted agents that still have endpoints wired for legacy job IDs (Regex Tester, SBOM Generator, PyPI Metadata, GitHub Releases, SSL Certificate Decoder, Security Headers Grader, Archive Inspector, Diff Analyzer, Unicode Inspector) likewise need nothing beyond the stdlib + `requirements.txt`.

### Lighthouse setup

```bash
# Lighthouse CLI (Node-native; reuses Playwright's Chromium via --chrome-flags)
sudo npm install -g lighthouse@11
lighthouse --version       # want 11.x
```

`web_search` was sunsetted on 2026-05-20 so `BRAVE_SEARCH_API_KEY` is no longer
required for the curated catalog. The endpoint stays wired for legacy job IDs;
if a legacy caller invokes it without the key set, the agent still returns
`web_search.no_api_key` and the call is automatically refunded.

---

## Verifying dependencies on a running server

SSH in and run:

```bash
# Python packages (all agents)
cd /home/aztea/app && source venv/bin/activate
pip check   # should report "No broken requirements"

# Playwright + Chromium (Browser Agent, Visual Regression)
python -m playwright install chromium 2>&1 | tail -5
python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); b = p.chromium.launch(); b.close(); p.stop(); print('OK')"

# Node.js (Multi-Language Executor; also Linter/TypeChecker if sunset agents are still resolvable)
node --version      # want ≥ 18.0.0
npx --version

# Deno (Multi-Language Executor — optional)
deno --version 2>/dev/null || echo "deno not installed"

# Bun (Multi-Language Executor — optional)
bun --version 2>/dev/null || echo "bun not installed"

# Go (Multi-Language Executor — optional)
go version 2>/dev/null || echo "go not installed"

# Rust (Multi-Language Executor — optional)
rustc --version 2>/dev/null || echo "rust not installed"

# ruff (Linter — sunset but still callable via historical IDs)
ruff --version 2>/dev/null || echo "ruff not installed (sunset agent only)"
```

---

## Installing Playwright on a fresh server

```bash
cd /home/aztea/app && source venv/bin/activate
pip install playwright
python -m playwright install chromium
python -m playwright install-deps chromium   # installs OS libs (Ubuntu only)
```

Verify:

```bash
python -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('https://example.com')
    print('title:', page.title())
    browser.close()
"
```

---

## Installing Node.js on a fresh server

```bash
# Ubuntu — use NodeSource for a current LTS
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
node --version    # should print v20.x.x
npm install -g npx
```

---

## What happens when a dependency is absent

Agents that require optional runtimes detect absence at call time and return a structured error rather than crashing:

```json
{
  "error": {
    "code": "linter_agent.node_not_available",
    "message": "Node.js is required for JavaScript/TypeScript linting but was not found on this host."
  }
}
```

This means:
- The agent is listed in the marketplace and callable — the platform does not hide it.
- Callers receive a clear, actionable error.
- The caller wallet is **refunded** because the call failed — billing is not triggered on `tool_unavailable` errors.

Agents where the dependency is **mandatory** (e.g. Browser Agent without Playwright) behave identically — the job fails cleanly, the caller is refunded, and the error code tells the operator exactly what is missing.

---

## Adding a new runtime dependency

When you add an agent that requires a system package:

1. Add a row to the dependency matrix above.
2. Add a `tool_unavailable` check at the top of the agent's `run()` function using `shutil.which()` or a try/import:

```python
import shutil

def run(payload: dict) -> dict:
    if shutil.which("deno") is None:
        return {"error": {"code": "my_agent.deno_not_available",
                          "message": "Deno is required but was not found on this host."}}
    # ... rest of implementation
```

3. Update `docs/runbooks/runtime-prerequisites.md` (this file).
4. Add a test in `tests/test_agent_real_tool.py` that mocks the binary as absent and asserts the structured error is returned.
