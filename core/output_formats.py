# OWNS: rendering an agent's structured JSON output into a human-readable
#       artifact (PR comment, Slack message, markdown, plain text). The
#       JSON output stays canonical; renderers produce a STRING attached
#       under `rendered_output` so callers can copy-paste without parsing.
# NOT OWNS: the agent contracts. We don't change what agents return; we
#       map well-known output shapes (CodeReview, DepAuditor, Linter,
#       TypeChecker, GitDiffAnalyzer, generic) to format-specific text.
# INVARIANTS:
#   - render() must never raise on unexpected output shapes — fall back to
#     the generic JSON-to-markdown formatter so the user always gets text.
#   - render() MUST be deterministic and pure: no clocks, no env reads.
#   - The set of supported format names is finite and stable; clients pin
#     to these strings. Don't rename them.
# DECISIONS:
#   - We dispatch by output-shape sniffing (presence of issues/findings/
#     vulnerabilities/etc.), NOT by agent_id. Two reasons: external agents
#     can ship the same structured shape and inherit pretty rendering for
#     free, and recipes (which return per-stage outputs) can be rendered
#     by combining stage-specific renderers.
#   - Format strings are intentionally minimal: every renderer must work
#     when pasted into its target context with no further processing.
# KNOWN DEBT:
#   - No table-rendering library — we hand-format markdown tables. Fine
#     for now; revisit if formatters get more complex.
"""Render structured agent output into PR-comment / Slack / markdown / text."""

from __future__ import annotations

import json
from typing import Any

SUPPORTED_FORMATS: tuple[str, ...] = (
    "json",  # canonical, no transformation
    "markdown",  # GitHub-flavored markdown
    "github_pr_comment",  # markdown formatted for github PR review API
    "slack_blocks",  # Slack Block Kit JSON (string-encoded)
    "text",  # flat plaintext, no markdown
)

# Slack Block Kit text fields cap at 3000 chars; keep margin for safety.
_SLACK_BLOCK_CHAR_LIMIT = 2900
_GENERIC_MD_MAX_CHARS = 4000
_DEP_AUDIT_DESC_PREVIEW_CHARS = 400
_CODE_REVIEW_ISSUE_CAP = 50
_CODE_REVIEW_POSITIVE_CAP = 10
# WHY: code_review_agent scores 1-10; some external agents return 1-100.
# Disambiguate by magnitude — anything ≤ 10 takes the /10 denominator.
_SCORE_TEN_BOUNDARY = 10


def normalize_format(value: Any) -> str | None:
    """Return one of SUPPORTED_FORMATS or None for unknown/missing."""
    if not value:
        return None
    s = str(value).strip().lower().replace("-", "_")
    aliases = {
        "md": "markdown",
        "pr_comment": "github_pr_comment",
        "github": "github_pr_comment",
        "slack": "slack_blocks",
        "plain": "text",
        "plaintext": "text",
    }
    s = aliases.get(s, s)
    return s if s in SUPPORTED_FORMATS else None


def render(
    output: Any, *, format: str, agent_meta: dict[str, Any] | None = None
) -> str | dict:
    """Render `output` into the given format.

    Returns a string for human-readable formats (markdown, github_pr_comment,
    text) and a dict for slack_blocks (Block Kit JSON the caller posts as-is).

    For "json" or unknown formats, returns the input verbatim (round-trip).
    """
    fmt = normalize_format(format) or "json"
    if fmt == "json":
        return output
    if fmt == "slack_blocks":
        return _render_slack(output, agent_meta or {})
    if fmt == "text":
        return _render_text(output, agent_meta or {})
    # markdown / github_pr_comment share the same renderer; the caller
    # adds GitHub-specific framing (review verdict) only on PR comment.
    text = _render_markdown(output, agent_meta or {})
    if fmt == "github_pr_comment":
        return _wrap_pr_comment(text, output)
    return text


# ── Markdown renderers (per output shape) ──────────────────────────────────


def _looks_like_secret_scan(output: dict[str, Any]) -> bool:
    """Pure: True when ``output`` matches the secret_scanner agent's shape.

    Why: detect this BEFORE the generic linter shape so secret-scan output
    isn't mislabeled as "## Linter". 2026-05-18 audit (bug #18): the prior
    check matched any output with ``total_findings`` OR ``rule_id`` in a
    finding — both are common across many findings-style agents
    (dockerfile_analyzer, sast_scanner, k8s_manifest_validator), so calling
    `output_format=github_pr_comment` on dockerfile_analyzer rendered a
    "## Secret Scanner" header and an empty-cell findings table. Tighten
    the detection to require fields that are *uniquely* secret-scanner:
    ``findings_by_severity`` (secret_scanner's exact severity bucket key)
    or at least one finding with ``redacted_preview`` / ``entropy``.
    """
    findings = output.get("findings")
    if not isinstance(findings, list):
        return False
    if "findings_by_severity" in output:
        return True
    return any(
        isinstance(f, dict) and ("redacted_preview" in f or "entropy" in f)
        for f in findings
    )


def _collect_known_sections(output: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    """Pure: build the per-shape markdown sections in render order."""
    sections: list[str] = []
    if "issues" in output and ("severity_counts" in output or "summary" in output):
        sections.append(_md_code_review(output))
    if _looks_like_secret_scan(output):
        sections.append(_md_secret_scan(output))
    elif isinstance(output.get("findings"), list):
        sections.append(_md_linter(output, meta))
    if isinstance(output.get("diagnostics"), list) or (
        isinstance(output.get("errors"), list) and "passed" in output
    ):
        sections.append(_md_type_check(output))
    if (
        isinstance(output.get("vulnerabilities"), list)
        or isinstance(output.get("packages"), list)
    ):
        sections.append(_md_dep_audit(output))
    if "risk_summary" in output and isinstance(output.get("files"), list):
        sections.append(_md_git_diff(output))
    if isinstance(output.get("steps"), list) or isinstance(output.get("step_results"), dict):
        sections.append(_md_pipeline(output))
    return sections


def _render_markdown(output: Any, meta: dict[str, Any]) -> str:
    """Pure: dispatch to per-shape markdown renderers; falls back to generic JSON dump."""
    if not isinstance(output, dict):
        return _generic_md(output)
    sections = _collect_known_sections(output, meta or {})
    if not sections:
        return _generic_md(output)
    return "\n\n".join(s for s in sections if s.strip())


def _format_score_line(score: Any) -> str | None:
    """Pure: ``**Score:** N/10`` or ``/100`` line, ``None`` if score isn't numeric."""
    if not isinstance(score, (int, float)):
        return None
    denom = _SCORE_TEN_BOUNDARY if 0 <= score <= _SCORE_TEN_BOUNDARY else 100
    return f"**Score:** {score}/{denom}"


def _format_severity_chip(counts: dict[str, Any]) -> str | None:
    """Pure: ``🔴 3 high · 🟠 2 medium`` chip, ``None`` if every count is zero."""
    chip = " · ".join(
        f"{_count_emoji(sev)} {count} {sev}"
        for sev, count in counts.items()
        if count
    )
    return f"\n{chip}" if chip else None


def _format_issue_location(issue: dict[str, Any]) -> str:
    """Pure: ``· `file:line`` chip, empty string when no file/line is set."""
    file_ = issue.get("file") or issue.get("filename")
    line = issue.get("line") or issue.get("line_hint")
    if file_ and line:
        return f" · `{file_}:{line}`"
    if file_:
        return f" · `{file_}`"
    if line:
        return f" · line `{line}`"
    return ""


def _format_issue_lines(issue: dict[str, Any]) -> list[str]:
    """Pure: bullet line(s) for one code-review issue, including any nested fix line."""
    sev = str(issue.get("severity") or "info").lower()
    cat = issue.get("category") or ""
    # Code-review agents use `description`; linters use `message`; generic uses `title`.
    text = str(
        issue.get("description") or issue.get("title") or issue.get("message") or ""
    ).strip()
    cwe = str(issue.get("cwe_id") or "").strip()
    cwe_chip = f" [{cwe}]" if cwe else ""
    location = _format_issue_location(issue)
    head = (
        f"- {_count_emoji(sev)} **{sev}** _{cat}_{cwe_chip}{location} — {text}"
        if cat
        else f"- {_count_emoji(sev)} **{sev}**{cwe_chip}{location} — {text}"
    )
    out = [head]
    fix = str(issue.get("fix") or issue.get("suggestion") or "").strip()
    if fix:
        out.append(f"  - Fix: {fix}")
    return out


def _md_code_review(output: dict[str, Any]) -> str:
    """Pure: render the code-review output shape into GitHub-flavoured markdown."""
    summary = str(output.get("summary") or "").strip()
    issues = output.get("issues") or []
    counts = output.get("severity_counts") or {}
    lines: list[str] = ["## Code Review"]
    score_line = _format_score_line(output.get("score"))
    if score_line:
        lines.append(score_line)
    if summary:
        lines.append(f"\n{summary}")
    chip = _format_severity_chip(counts)
    if chip:
        lines.append(chip)
    if issues:
        lines.append("\n### Issues")
        for issue in issues[:_CODE_REVIEW_ISSUE_CAP]:
            lines.extend(_format_issue_lines(issue))
    positives = output.get("positive_aspects") or []
    if positives:
        lines.append("\n### What's good")
        lines.extend(f"- {p}" for p in positives[:_CODE_REVIEW_POSITIVE_CAP])
    return "\n".join(lines).strip()


def _md_secret_scan(output: dict[str, Any]) -> str:
    findings = output.get("findings") or []
    total = output.get("total_findings")
    if not isinstance(total, int):
        total = len(findings)
    by_sev = output.get("findings_by_severity") or {}
    summary = str(output.get("summary") or "").strip()
    lines: list[str] = ["## Secret Scanner"]
    if summary:
        lines.append(summary)
    elif total == 0:
        lines.append("✓ No leaked credentials detected.")
    else:
        lines.append(f"{total} potential leak{'s' if total != 1 else ''} detected.")
    if isinstance(by_sev, dict) and by_sev:
        sev_summary = " · ".join(
            f"**{int(by_sev.get(level) or 0)}** {level}"
            for level in ("critical", "high", "medium", "low")
            if int(by_sev.get(level) or 0)
        )
        if sev_summary:
            lines.append(sev_summary)
    if findings:
        lines.append("\n| Severity | Rule | Location | Preview |")
        lines.append("| --- | --- | --- | --- |")
        for f in findings[:50]:
            sev = str(f.get("severity") or "low")
            rule = str(f.get("rule_name") or f.get("rule_id") or "")
            line = f.get("line")
            col = f.get("column")
            loc = f"L{line}:{col}" if line and col else (f"L{line}" if line else "")
            preview = str(f.get("redacted_preview") or "").replace("|", "\\|")
            lines.append(f"| {sev} | `{rule}` | {loc} | `{preview}` |")
    return "\n".join(lines)


def _md_linter(output: dict[str, Any], meta: dict[str, Any] | None = None) -> str:
    """Render a `findings`-shaped output as markdown.

    2026-05-18 audit (bug #18): use the agent's display name in the header
    when available so dockerfile_analyzer / sast / k8s findings don't all
    surface under the wrong "## Linter" or (worse) "## Secret Scanner"
    label. Mirrors the Slack renderer behavior added in 1.7.0.
    """
    findings = output.get("findings") or []
    total = (
        output.get("total") if isinstance(output.get("total"), int) else len(findings)
    )
    summary = str(output.get("summary") or "").strip()
    agent_name = ""
    if isinstance(meta, dict):
        agent_name = str(meta.get("name") or meta.get("agent_name") or "").strip()
    # Preserve back-compat default ("## Linter") for callers that don't
    # pass agent meta — the existing test_render_linter_shape asserts
    # this string. Agent name takes precedence when available.
    header_label = agent_name or "Linter"
    lines: list[str] = [f"## {header_label}"]
    if summary:
        lines.append(summary)
    elif total == 0:
        lines.append("✓ No issues found.")
    else:
        lines.append(f"{total} issue{'s' if total != 1 else ''} found.")
    if findings:
        lines.append("\n| Severity | Rule | Location | Message |")
        lines.append("| --- | --- | --- | --- |")
        for f in findings[:50]:
            sev = str(f.get("severity") or "warning").lower()
            rule = str(f.get("rule") or f.get("code") or "")
            file_ = str(f.get("file") or "")
            line = f.get("line") or ""
            loc = f"`{file_}:{line}`" if file_ else ""
            msg = str(f.get("message") or "").replace("|", "\\|")
            lines.append(f"| {sev} | `{rule}` | {loc} | {msg} |")
    return "\n".join(lines)


def _md_type_check(output: dict[str, Any]) -> str:
    diags = output.get("diagnostics") or output.get("errors") or []
    passed = output.get("passed")
    lines: list[str] = ["## Type Check"]
    if passed is True or (isinstance(diags, list) and not diags):
        lines.append("✓ No type errors.")
    else:
        lines.append(f"{len(diags)} type error{'s' if len(diags) != 1 else ''}.")
    if diags:
        lines.append("\n| File | Line | Code | Message |")
        lines.append("| --- | --- | --- | --- |")
        for d in diags[:50]:
            file_ = str(d.get("file") or "")
            line = d.get("line") or ""
            code = str(d.get("code") or "")
            msg = str(d.get("message") or "").replace("|", "\\|")
            lines.append(f"| `{file_}` | {line} | `{code}` | {msg} |")
    return "\n".join(lines)


def _md_dep_audit(output: dict[str, Any]) -> str:
    vulns = output.get("vulnerabilities") or []
    if not vulns and isinstance(output.get("packages"), list):
        for pkg in output.get("packages") or []:
            if not isinstance(pkg, dict):
                continue
            for cve in pkg.get("cves") or []:
                if isinstance(cve, dict):
                    vulns.append({**cve, "package": pkg.get("name"), "fixed_in": cve.get("fixed_in")})
    lines: list[str] = ["## Dependency Audit"]
    summary = str(output.get("summary") or "").strip()
    if summary:
        lines.append(summary)
    elif not vulns:
        lines.append("✓ No known vulnerabilities.")
    else:
        lines.append(
            f"{len(vulns)} vulnerabilit{'ies' if len(vulns) != 1 else 'y'} found."
        )
    if vulns:
        lines.append("\n| Severity | Package | CVE | Fix |")
        lines.append("| --- | --- | --- | --- |")
        for v in vulns[:50]:
            sev = str(v.get("severity") or "unknown").lower()
            pkg = str(v.get("package") or v.get("name") or "")
            cve = str(v.get("cve_id") or v.get("id") or "")
            fix = str(v.get("fix_version") or v.get("fixed_in") or "—")
            lines.append(f"| {_count_emoji(sev)} {sev} | `{pkg}` | `{cve}` | `{fix}` |")
    return "\n".join(lines)


def _md_git_diff(output: dict[str, Any]) -> str:
    summary = str(output.get("summary") or "").strip()
    risks = output.get("risk_summary") or {}
    files = output.get("files") or []
    lines: list[str] = ["## Diff Risk Profile"]
    if summary:
        lines.append(summary)
    risk_chips = []
    for key, val in risks.items():
        if isinstance(val, bool) and val:
            risk_chips.append(f"⚠ {key}")
        elif isinstance(val, int) and val:
            risk_chips.append(f"{key}: {val}")
    if risk_chips:
        lines.append("\n" + " · ".join(risk_chips))
    if files:
        lines.append("\n| File | Type | +/− | Risk |")
        lines.append("| --- | --- | --- | --- |")
        for f in files[:30]:
            tags = ", ".join(f.get("risk_tags") or []) or "—"
            lines.append(
                f"| `{f.get('path', '')}` | {f.get('change_type', '')} | "
                f"+{f.get('added', 0)}/-{f.get('removed', 0)} | {tags} |"
            )
    # 1.6.2: never return a heading-only document. The 1.6.1 power-user eval
    # ran the diff_analyzer markdown renderer against an empty diff and got
    # back just "## Diff Risk Profile" with no body — looks broken to a
    # reader. When summary, risk chips, and files are all empty, surface a
    # neutral one-liner so the output is self-contained.
    if len(lines) == 1:
        lines.append("\n_No changes detected._")
    return "\n".join(lines)


def _md_pipeline(output: dict[str, Any]) -> str:
    """Pipeline / recipe output — render each step that has a known shape."""
    sections: list[str] = ["## Pipeline Result"]
    summary = str(output.get("summary") or "").strip()
    if summary:
        sections.append(summary)
    steps = output.get("steps")
    step_results = output.get("step_results")
    items: list[tuple[str, Any]] = []
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                items.append(
                    (
                        str(step.get("id") or step.get("node_id") or ""),
                        step.get("output"),
                    )
                )
    elif isinstance(step_results, dict):
        items = [
            (str(k), v.get("output") if isinstance(v, dict) else v)
            for k, v in step_results.items()
        ]
    for step_id, step_out in items:
        rendered = (
            _render_markdown(step_out, {})
            if isinstance(step_out, dict)
            else _generic_md(step_out)
        )
        sections.append(f"### Stage `{step_id}`\n{rendered}")
    return "\n\n".join(sections)


def _generic_md(output: Any) -> str:
    """Fallback: pretty-print as a code block so it stays readable."""
    if isinstance(output, str):
        return output.strip()
    pretty = json.dumps(output, indent=2, ensure_ascii=False, default=str)
    if len(pretty) > _GENERIC_MD_MAX_CHARS:
        pretty = pretty[:_GENERIC_MD_MAX_CHARS] + "\n…"
    return f"```json\n{pretty}\n```"


# ── Other format renderers ─────────────────────────────────────────────────


def _wrap_pr_comment(markdown: str, output: dict[str, Any] | Any) -> str:
    """Add a one-line verdict header so reviewers see the conclusion first."""
    verdict = _pr_verdict(output)
    body = f"<!-- aztea: {verdict['kind']} -->\n**{verdict['headline']}**\n\n{markdown}".strip()
    if len(body) > 60_000:  # GitHub PR comment limit is ~64KB
        body = body[: 60_000 - 12] + "\n\n_…truncated_"
    return body


def _pr_verdict(output: Any) -> dict[str, str]:
    if not isinstance(output, dict):
        return {"kind": "info", "headline": "Aztea review"}
    counts = output.get("severity_counts") or {}
    crit = int(counts.get("critical") or 0)
    high = int(counts.get("high") or 0)
    if crit:
        return {
            "kind": "critical",
            "headline": f"❌ {crit} critical issue{'s' if crit != 1 else ''} — block merge",
        }
    if high:
        return {
            "kind": "high",
            "headline": f"⚠ {high} high-severity issue{'s' if high != 1 else ''} — review before merge",
        }
    score = output.get("score")
    if isinstance(score, (int, float)):
        normalized = score * 10 if 0 <= score <= 10 else score
        if normalized >= 90:
            return {"kind": "ok", "headline": "✅ Looks good — no blocking issues"}
    return {"kind": "info", "headline": "Aztea review"}


def _render_text(output: Any, meta: dict[str, Any]) -> str:
    """Plaintext: like markdown but stripped of fences and table separators."""
    md = _render_markdown(output, meta)
    out = []
    for line in md.splitlines():
        if line.startswith("|---") or line == "| --- |":
            continue
        if line.startswith("```"):
            continue
        out.append(line.replace("**", "").replace("`", ""))
    return "\n".join(out).strip()


def _render_slack(output: Any, meta: dict[str, Any]) -> dict:
    """Slack Block Kit JSON. Returns the dict; callers POST it as `blocks`.

    Builds STRUCTURED Block Kit for known shapes (one section per issue +
    header + context blocks) so messages render with severity emoji,
    expandable details, and proper visual separation. Falls back to
    chunked-mrkdwn for unknown shapes.
    """
    if isinstance(output, dict):
        if "issues" in output and ("severity_counts" in output or "summary" in output):
            return {"blocks": _slack_code_review_blocks(output)}
        # 1.7.3 — k8s_manifest_validator emits resources[*].findings, not a
        # top-level findings array. Detect it before the generic findings
        # branch so it gets its own renderer (severity totals + per-resource
        # blocks + path) instead of falling through to the JSON code-fence
        # fallback the 1.7.1 eval flagged.
        if isinstance(output.get("resources"), list) and (
            "by_severity" in output or "total_findings" in output
        ):
            return {"blocks": _slack_k8s_blocks(output, meta)}
        # Secret scanner before linter so we don't mislabel "Linter" on a
        # leaked-credentials report.
        # Secret-scan and linter findings share the same `findings` list
        # shape, so they fall through to the same renderer for now. A
        # dedicated _slack_secret_scan_blocks (severity emoji + redacted
        # preview formatting) is a future polish item — until then the
        # linter renderer surfaces enough detail and is the closest
        # sibling. Removing the unreachable call to a never-implemented
        # function fixes a flake8 F821 that was blocking CI.
        if isinstance(output.get("findings"), list):
            return {"blocks": _slack_linter_blocks(output, meta)}
        if isinstance(output.get("vulnerabilities"), list):
            return {"blocks": _slack_dep_audit_blocks(output)}
    md = _render_markdown(output, meta)
    blocks: list[dict] = []
    for chunk in _split_for_slack(md):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
    if not blocks:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "_(no output)_"}}
        )
    return {"blocks": blocks}


def _slack_header(text: str) -> dict:
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": text[:150], "emoji": True},
    }


def _slack_section(md: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": md[:_SLACK_BLOCK_CHAR_LIMIT]}}


def _slack_context(md: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": md[:_SLACK_BLOCK_CHAR_LIMIT]}]}


_SLACK_CODE_REVIEW_ISSUE_CAP = 30


def _slack_score_chip(score: Any) -> str | None:
    """Pure: ``*Score:* N/10`` Slack-mrkdwn chip; ``None`` if non-numeric."""
    if not isinstance(score, (int, float)):
        return None
    denom = _SCORE_TEN_BOUNDARY if 0 <= score <= _SCORE_TEN_BOUNDARY else 100
    return f"*Score:* {score}/{denom}"


def _slack_issue_block(issue: dict[str, Any]) -> dict:
    """Pure: shape one code-review issue into a Slack mrkdwn section block."""
    sev = str(issue.get("severity") or "info").lower()
    cat = str(issue.get("category") or "")
    text = str(
        issue.get("description") or issue.get("title") or issue.get("message") or ""
    ).strip()
    line = issue.get("line") or issue.get("line_hint") or ""
    cwe = str(issue.get("cwe_id") or "").strip()
    fix = str(issue.get("fix") or issue.get("suggestion") or "").strip()
    head = f"{_count_emoji(sev)} *{sev.upper()}*"
    if cat:
        head += f" · _{cat}_"
    if cwe:
        head += f" · `{cwe}`"
    if line:
        head += f" · line `{line}`"
    body = f"{head}\n{text}"
    if fix:
        body += f"\n→ *Fix:* {fix}"
    return _slack_section(body)


def _slack_code_review_blocks(output: dict[str, Any]) -> list[dict]:
    """Pure: render the code-review output shape as a list of Slack Block Kit blocks."""
    blocks: list[dict] = [_slack_header("Code Review")]
    head_bits: list[str] = []
    score_chip = _slack_score_chip(output.get("score"))
    if score_chip:
        head_bits.append(score_chip)
    counts = output.get("severity_counts") or {}
    chips = " · ".join(f"{_count_emoji(sev)} {n} {sev}" for sev, n in counts.items() if n)
    if chips:
        head_bits.append(chips)
    if head_bits:
        blocks.append(_slack_context(" · ".join(head_bits)))
    summary = str(output.get("summary") or "").strip()
    if summary:
        blocks.append(_slack_section(summary))
    issues = output.get("issues") or []
    if issues:
        blocks.append({"type": "divider"})
        blocks.extend(_slack_issue_block(issue) for issue in issues[:_SLACK_CODE_REVIEW_ISSUE_CAP])
    return blocks


def _slack_linter_blocks(
    output: dict[str, Any], meta: dict[str, Any] | None = None,
) -> list[dict]:
    r"""Render a `findings`-shaped output as Slack blocks.

    1.7.0: pre-existing version hardcoded the header "Linter" and shipped
    `` `` `` for empty rule names. Both made secret_scanner / sast / k8s
    output (all of which share the findings shape) look like a Linter
    misfire. Now the header uses the agent's display name when available,
    and empty rule names are dropped from the bullet body cleanly.
    """
    findings = output.get("findings") or []
    total = (
        output.get("total") if isinstance(output.get("total"), int) else len(findings)
    )
    agent_name = ""
    if isinstance(meta, dict):
        agent_name = str(meta.get("name") or meta.get("agent_name") or "").strip()
    header_label = agent_name if agent_name else "Findings"
    blocks: list[dict] = [_slack_header(header_label)]
    blocks.append(
        _slack_context(
            "✓ No issues found."
            if not total
            else f"{total} issue{'s' if total != 1 else ''} found."
        )
    )
    if findings:
        rows = []
        for f in findings[:25]:
            sev = str(f.get("severity") or "warning").lower()
            rule = str(f.get("rule") or f.get("code") or "").strip()
            file_ = str(f.get("file") or "")
            line = f.get("line") or ""
            msg = str(f.get("message") or "").strip()
            loc = f"`{file_}:{line}`" if file_ else (f"line `{line}`" if line else "")
            rule_chip = f"`{rule}` " if rule else ""
            head = f"{_count_emoji(sev)} {rule_chip}{loc}".rstrip()
            rows.append(f"{head}\n{msg}" if msg else head)
        blocks.append(_slack_section("\n\n".join(rows)))
    return blocks


def _slack_dep_audit_blocks(output: dict[str, Any]) -> list[dict]:
    vulns = output.get("vulnerabilities") or []
    blocks: list[dict] = [_slack_header("Dependency Audit")]
    summary = str(output.get("summary") or "").strip()
    if summary:
        blocks.append(_slack_context(summary))
    elif not vulns:
        blocks.append(_slack_context("✓ No known vulnerabilities."))
    else:
        blocks.append(
            _slack_context(
                f"{len(vulns)} vulnerabilit{'ies' if len(vulns) != 1 else 'y'} found."
            )
        )
    if vulns:
        blocks.append({"type": "divider"})
        for v in vulns[:30]:
            sev = str(v.get("severity") or "unknown").lower()
            pkg = str(v.get("package") or v.get("name") or "")
            cve = str(v.get("cve_id") or v.get("id") or "")
            fix = str(v.get("fix_version") or v.get("fixed_in") or "—")
            desc = str(v.get("description") or v.get("summary") or "").strip()
            body = f"{_count_emoji(sev)} *{sev.upper()}* · `{pkg}` · `{cve}`\n*Fix in:* `{fix}`"
            if desc:
                body += f"\n{desc[:_DEP_AUDIT_DESC_PREVIEW_CHARS]}"
            blocks.append(_slack_section(body))
    return blocks


def _slack_k8s_blocks(output: dict[str, Any], meta: dict[str, Any]) -> list[dict]:
    """Slack Block Kit for k8s_manifest_validator output.

    1.7.3 — k8s findings nest under `resources[*].findings`, not at the
    top level, so before this renderer existed the output fell through
    to a raw JSON code-fence in Slack (eval B-14). Now: one block per
    resource with its findings + per-severity emoji + path.
    """
    agent_name = (meta or {}).get("agent_name") or "Kubernetes Manifest Validator"
    blocks: list[dict] = [_slack_header(str(agent_name))]
    by_severity = output.get("by_severity") or {}
    total = int(output.get("total_findings") or 0)
    valid = bool(output.get("valid"))
    parsed = int(output.get("resources_parsed") or 0)
    kubectl_available = bool(output.get("kubectl_available"))
    summary_parts: list[str] = []
    if valid:
        summary_parts.append("✅ Manifests *valid*")
    else:
        summary_parts.append("❌ Manifests *invalid*")
    summary_parts.append(f"{parsed} resource{'s' if parsed != 1 else ''} parsed")
    if total:
        sev_chips = []
        for sev_name, label in (("error", "errors"), ("warning", "warnings"), ("info", "info")):
            count = int(by_severity.get(sev_name) or 0)
            if count:
                sev_chips.append(f"{_count_emoji(sev_name)} {count} {label}")
        if sev_chips:
            summary_parts.append("·".join(sev_chips))
    else:
        summary_parts.append("no findings")
    if not kubectl_available:
        summary_parts.append("_(kubectl not available — local rules only)_")
    blocks.append(_slack_context(" · ".join(summary_parts)))
    resources = output.get("resources") or []
    for res in resources[:30]:
        findings = res.get("findings") or []
        if not findings:
            continue
        kind = str(res.get("kind") or "Resource")
        name = str(res.get("name") or "—")
        rows: list[str] = [f"*{kind}/{name}*"]
        for f in findings[:20]:
            sev = str(f.get("severity") or "info").lower()
            rule = str(f.get("rule") or "")
            msg = str(f.get("message") or "").strip()
            path = str(f.get("path") or "")
            rule_chip = f"`{rule}` " if rule else ""
            loc = f" · `{path}`" if path else ""
            rows.append(f"{_count_emoji(sev)} {rule_chip}{msg}{loc}")
        blocks.append(_slack_section("\n".join(rows)))
    return blocks


def _split_for_slack(text: str, *, limit: int = _SLACK_BLOCK_CHAR_LIMIT) -> list[str]:
    """Slack section blocks max ~3000 chars. Split on blank lines."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for paragraph in text.split("\n\n"):
        if len(buf) + len(paragraph) + 2 > limit and buf:
            chunks.append(buf.strip())
            buf = ""
        buf = (buf + "\n\n" + paragraph) if buf else paragraph
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


# ── Helpers ────────────────────────────────────────────────────────────────


def _count_emoji(severity: str) -> str:
    return {
        "critical": "🔴",
        "high": "🟠",
        "medium": "🟡",
        "low": "🟢",
        "info": "🔵",
        "warning": "🟡",
        "error": "🔴",
    }.get(severity.lower(), "•")


# Phase 0 (2026-05-28) refusal envelope renderer. The existing render()
# handles successful agent outputs; refusal envelopes (returned by
# do_specialist_task when the gates refuse) bypass it. Without this
# helper, a caller passing output_format=github_pr_comment on a refusal
# gets raw JSON. Per /autoplan D-5.


def render_refusal(
    reason: str | None,
    next_step: str | None,
    output_format: str,
    candidates: list[dict] | None = None,
) -> str | None:
    """Pure: render an auto-hire refusal in the requested output format.

    Returns None when ``output_format`` is unrecognized or refusal
    rendering doesn't add value (e.g. raw JSON — caller can read the
    structured fields directly). Returns a string when a human-readable
    rendering exists for the format.
    """
    fmt = (output_format or "").lower().strip()
    if fmt not in {"markdown", "github_pr_comment", "slack_blocks", "text"}:
        return None
    safe_reason = reason or "unspecified"
    safe_next = next_step or "(no next-step hint)"
    cand_preview = ""
    if candidates:
        names = []
        for c in candidates[:3]:
            slug = str(c.get("slug") or c.get("recipe_id") or "?")
            names.append(slug)
        if names:
            cand_preview = "Top candidates: " + ", ".join(names)
    if fmt == "text":
        parts = [
            f"Aztea refused — {safe_reason}",
            "",
            safe_next,
        ]
        if cand_preview:
            parts.append("")
            parts.append(cand_preview)
        return "\n".join(parts)
    if fmt in {"markdown", "github_pr_comment"}:
        parts = [
            f"### Aztea: `{safe_reason}`",
            "",
            safe_next,
        ]
        if cand_preview:
            parts.append("")
            parts.append(f"_{cand_preview}_")
        return "\n".join(parts)
    if fmt == "slack_blocks":
        # Slack expects an array of block-kit blocks serialized as JSON.
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Aztea refused* — `{safe_reason}`\n{safe_next}",
                },
            }
        ]
        if cand_preview:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": cand_preview}],
            })
        import json as _json
        return _json.dumps(blocks)
    return None
