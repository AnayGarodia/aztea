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
| Visual Regression        | Playwright + Chromium      | `python -m playwright install --check` | `visual_regression.playwright_not_installed` |
| Linter (JS/TS)           | Node.js ≥ 18 + npx         | `node --version && npx --version`      | `linter_agent.node_not_available` (Python/ruff path still works) |
| Type Checker (tsc)       | Node.js ≥ 18 + npx         | `npx tsc --version`                    | `type_checker.tsc_not_available` (mypy path still works) |
| Multi-Language Executor  | Node.js, Deno, Bun, Go, Rust | see per-language check below         | `multi_language_executor.runtime_not_available` |
| Shell Executor           | POSIX shell (`/bin/sh`)    | `which sh`                             | Always available on Linux                |
| DB Sandbox               | None (stdlib `sqlite3`)    | —                                      | —                                        |
| Python Code Executor     | Python 3.10+               | `python --version`                     | —                                        |
| Multi-File Executor      | Python 3.10+               | `python --version`                     | —                                        |
| Semantic Codebase Search | `git` (for git-clone path) | `git --version`                        | `semantic_codebase_search.git_not_available` |

All other agents (CVE Lookup, arXiv, Web Researcher, etc.) require only standard network access and the Python packages in `requirements.txt`.

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

# Node.js (Linter JS/TS, Type Checker tsc)
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

# Git (Semantic Codebase Search git-clone path)
git --version

# ruff (Linter — Python path)
ruff --version
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
