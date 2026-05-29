// OWNS: the curated playground template catalog. Each template is a
//       working handler shape that loads into the Monaco editor as the
//       buyer's starting point.
// NOT OWNS: the playground runtime (server/routes/playground.py), the
//       Monaco editor wrapper (BuildPage.jsx), or any persisted state
//       — these are pure data.
// DECISIONS:
//   * Source bodies are stored inline so the build doesn't need a
//     server round-trip to enumerate templates. Bundle cost is small
//     (each template is under 60 lines).
//   * Categories match the Wave 3 brief: Security, Data, DevTools, AI.
//   * Every template defines `def handler(payload)` and returns a
//     dict — the same shape the publish flow expects.
//   * Sample input lives next to the source so the "Test" button can
//     pre-fill the input panel.

export const TEMPLATE_CATEGORIES = [
  { id: 'security',  label: 'Security' },
  { id: 'data',      label: 'Data' },
  { id: 'devtools',  label: 'Dev tools' },
  { id: 'ai',        label: 'AI utilities' },
]

export const TEMPLATES = [
  // ── Security ────────────────────────────────────────────────────────
  {
    id: 'cve-scan-requirements',
    category: 'security',
    name: 'CVE scan against requirements.txt',
    blurb: 'Take a requirements.txt body and list any package on the public CVE feed.',
    sampleInput: { manifest: 'requests==2.20.0\nflask==1.0.0\n' },
    source: `# Scan a requirements.txt for known CVEs.
# In a real listing this would call the dependency_auditor agent;
# the template demonstrates the handler shape + payload contract.

def handler(payload):
    manifest = payload.get("manifest", "")
    if not isinstance(manifest, str) or not manifest.strip():
        return {"error": "manifest_required", "findings": []}

    pinned = []
    for line in manifest.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" in line:
            name, ver = line.split("==", 1)
            pinned.append({"package": name.strip(), "version": ver.strip()})

    return {
        "packages_audited": len(pinned),
        "pinned": pinned,
        # Real implementation: fan out to the dependency_auditor agent.
        "findings": [],
        "summary": f"Parsed {len(pinned)} pinned dependency(ies).",
    }
`,
  },
  {
    id: 'jwt-decode',
    category: 'security',
    name: 'JWT decoder + claim summariser',
    blurb: 'Decode a JWT (header + claims, no signature check) and surface expiry status.',
    sampleInput: { token: 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyXzEifQ.x' },
    source: `import base64
import json

def _b64url_decode(segment):
    pad = '=' * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)

def handler(payload):
    token = payload.get("token", "")
    if not isinstance(token, str) or token.count(".") != 2:
        return {"error": "invalid_jwt_shape"}

    header_b64, claims_b64, _sig = token.split(".")
    try:
        header = json.loads(_b64url_decode(header_b64))
        claims = json.loads(_b64url_decode(claims_b64))
    except Exception as exc:
        return {"error": "decode_failed", "detail": str(exc)}

    expiry = claims.get("exp")
    return {
        "header": header,
        "claims": claims,
        "has_expiry": expiry is not None,
        "subject": claims.get("sub"),
    }
`,
  },
  {
    id: 'webhook-signature',
    category: 'security',
    name: 'HMAC webhook signature validator',
    blurb: 'Verify an HMAC-SHA256 signature against an expected secret + payload.',
    sampleInput: {
      payload: '{"event":"ping"}',
      signature: 'replace-with-hex-sig',
      secret: 'whsec_test',
    },
    source: `import hashlib
import hmac

def handler(payload):
    body = payload.get("payload", "")
    sig = payload.get("signature", "")
    secret = payload.get("secret", "")
    if not body or not sig or not secret:
        return {"error": "payload, signature, secret are required"}

    expected = hmac.new(
        secret.encode("utf-8"),
        body.encode("utf-8") if isinstance(body, str) else body,
        hashlib.sha256,
    ).hexdigest()

    return {
        "valid": hmac.compare_digest(expected, sig),
        "expected_signature_prefix": expected[:12],
    }
`,
  },

  // ── Data ─────────────────────────────────────────────────────────────
  {
    id: 'json-to-csv',
    category: 'data',
    name: 'JSON → CSV',
    blurb: 'Flatten a list of dicts into a CSV string with stable column order.',
    sampleInput: { rows: [{ name: 'a', n: 1 }, { name: 'b', n: 2 }] },
    source: `import csv
import io

def handler(payload):
    rows = payload.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return {"csv": "", "row_count": 0}

    # Stable column order = union of keys, sorted.
    columns = sorted({k for row in rows if isinstance(row, dict) for k in row.keys()})
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        if isinstance(row, dict):
            writer.writerow(row)

    return {"csv": buf.getvalue(), "row_count": len(rows), "columns": columns}
`,
  },
  {
    id: 'csv-schema-infer',
    category: 'data',
    name: 'CSV schema inferrer',
    blurb: 'Read CSV text and infer column types (string / int / float / bool).',
    sampleInput: { csv: 'a,b,c\n1,2.5,true\n3,4.1,false\n' },
    source: `import csv
import io

def _infer(value):
    if value.lower() in ("true", "false"):
        return "bool"
    try:
        int(value)
        return "int"
    except ValueError:
        pass
    try:
        float(value)
        return "float"
    except ValueError:
        return "string"

def handler(payload):
    text = payload.get("csv") or ""
    if not isinstance(text, str) or not text.strip():
        return {"error": "csv_required", "columns": []}

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    schema = {}
    for col in reader.fieldnames or []:
        types = {_infer(r[col]) for r in rows if r.get(col)}
        if not types:
            schema[col] = "unknown"
        elif len(types) == 1:
            schema[col] = next(iter(types))
        else:
            schema[col] = "mixed:" + ",".join(sorted(types))

    return {"columns": reader.fieldnames or [], "schema": schema, "row_count": len(rows)}
`,
  },

  // ── Dev tools ────────────────────────────────────────────────────────
  {
    id: 'dockerfile-lint',
    category: 'devtools',
    name: 'Dockerfile linter (minimal)',
    blurb: 'Flag the most common Dockerfile smells: latest tag, root user, ADD vs COPY.',
    sampleInput: { dockerfile: 'FROM python:latest\nADD . /app\n' },
    source: `def handler(payload):
    dockerfile = payload.get("dockerfile") or ""
    if not isinstance(dockerfile, str) or not dockerfile.strip():
        return {"error": "dockerfile_required", "findings": []}

    findings = []
    for lineno, raw in enumerate(dockerfile.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("FROM") and ":latest" in line:
            findings.append({"line": lineno, "rule": "no_latest_tag",
                              "message": "Pin a specific FROM tag, not :latest."})
        if line.startswith("ADD ") and "http" not in line.lower():
            findings.append({"line": lineno, "rule": "prefer_copy",
                              "message": "Use COPY instead of ADD for local files."})
        if line.startswith("USER root"):
            findings.append({"line": lineno, "rule": "no_root_user",
                              "message": "Run as a non-root user in production."})

    return {"findings": findings, "issue_count": len(findings)}
`,
  },
  {
    id: 'regex-tester',
    category: 'devtools',
    name: 'Regex tester (safe)',
    blurb: 'Run a regex against sample inputs and return matches.',
    sampleInput: { pattern: '^[a-z]+$', samples: ['hello', 'WORLD', 'mixed_99'] },
    source: `import re

# Cap pattern length to dodge catastrophic-backtracking probes.
_MAX_PATTERN_LEN = 200

def handler(payload):
    pattern = payload.get("pattern") or ""
    samples = payload.get("samples") or []
    if not isinstance(pattern, str) or not pattern:
        return {"error": "pattern_required"}
    if len(pattern) > _MAX_PATTERN_LEN:
        return {"error": "pattern_too_long", "max": _MAX_PATTERN_LEN}

    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return {"error": "regex_error", "detail": str(exc)}

    out = []
    for sample in samples[:50]:
        m = compiled.search(str(sample))
        out.append({"sample": sample, "matched": bool(m),
                     "match_text": m.group(0) if m else None})

    return {"results": out, "tested": len(out)}
`,
  },

  // ── AI utilities ────────────────────────────────────────────────────
  {
    id: 'summarize-text',
    category: 'ai',
    name: 'Text summariser (truncate-first)',
    blurb: 'A handler stub for a text summariser. Real listing would call an LLM.',
    sampleInput: { text: 'a long article body ...', max_chars: 200 },
    source: `def handler(payload):
    text = payload.get("text") or ""
    if not isinstance(text, str) or not text.strip():
        return {"error": "text_required"}

    max_chars = int(payload.get("max_chars") or 200)
    max_chars = max(40, min(max_chars, 2000))

    # In a real listing this would call run_with_fallback() from core.llm.
    # The template ships as a deterministic truncator so the playground
    # demo works without a provider key.
    truncated = text.strip()[:max_chars]
    summary = truncated + ("..." if len(text) > max_chars else "")
    return {
        "summary": summary,
        "input_chars": len(text),
        "summary_chars": len(summary),
    }
`,
  },
]


export function templateById(id) {
  return TEMPLATES.find(t => t.id === id) || null
}

export function templatesByCategory(category) {
  if (!category) return TEMPLATES
  return TEMPLATES.filter(t => t.category === category)
}
