# Scammer listings — adversarial corpus

Hand-crafted attempts to publish a malicious agent. Each file is fed
through the corresponding scanner and the loader test asserts every file
in this directory is BLOCKED by the publish flow.

## Layout

- `skill_md/*.md` — files fed to `core.listing_safety.scan_skill_md`
- `python_handler/*.py` — files fed to `core.listing_safety.scan_python_handler`
- `endpoint_url/*.txt` — one URL per file, fed to `scan_agent_md_endpoint` + `url_security.validate_agent_endpoint_url`
- `clean_negative_space/*` — legitimate samples that MUST NOT be blocked

## Naming convention

`<vector>__<short-description>.<ext>` so file names self-document the
attack class. Examples:
- `prompt_injection__plain.md`
- `prompt_injection__nfkc_fullwidth.md`
- `key_leak__openai_scoped.md`
- `ast_bypass__getattr_concat.py`
- `endpoint__percent_encoded_aztea.txt`

## Adding new entries

When you find a new attack vector in the wild or in a red-team session,
drop a sample here named after the vector. The loader picks it up
automatically — no test code changes needed.
