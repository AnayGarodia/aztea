# Vendored axe-core

- **File:** `axe.min.js`, version **4.8.4** (unmodified upstream build)
- **Source:** https://github.com/dequelabs/axe-core/releases/tag/v4.8.4
  (fetched from the cdnjs mirror referenced by
  `agents/accessibility_auditor.py::_AXE_CDN_URL`)
- **sha256:** `1d7975184c74f8bc15076edf2e6c207570a67933366de570c05a2c5af1732e6a`
- **License:** MPL-2.0 (see `LICENSE` in this directory; attribution entry
  in the repo-root `NOTICE`)

## Why it's vendored

`accessibility_auditor` injects axe-core into the audited page from the
CDN. When the CDN is unreachable or the target page's CSP blocks the
fetch, the audit used to hard-fail. This local copy is the fallback
(injected via `add_script_tag(content=...)`); the CDN stays primary so
the agent keeps tracking patch releases between vendor bumps.

## Invariants

- **The file ships unmodified.** MPL-2.0 is file-level copyleft: if this
  file is ever modified, the modifications must remain MPL-licensed and
  be marked as changed. Don't edit it — bump the version instead.
- **The vendored version must match `_AXE_CDN_URL`'s version.** A unit
  test pins the two together (`tests/test_agent_real_tool.py`).

## Updating

1. Pick the new version; update `_AXE_CDN_URL` in
   `agents/accessibility_auditor.py`.
2. `curl -sL https://cdnjs.cloudflare.com/ajax/libs/axe-core/<ver>/axe.min.js -o axe.min.js`
3. Refresh the sha256 above (`shasum -a 256 axe.min.js`) and the version
   string in this README.
4. Run the accessibility auditor tests.
