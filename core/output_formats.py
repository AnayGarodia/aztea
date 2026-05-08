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


def _render_markdown(output: Any, meta: dict[str, Any]) -> str:
    if not isinstance(output, dict):
        return _generic_md(output)
    sections: list[str] = []
    rendered = False

    # Code-review shape: {score, summary, issues, severity_counts, ...}
    if "issues" in output and ("severity_counts" in output or "summary" in output):
        sections.append(_md_code_review(output))
        rendered = True

    # Secret-scanner shape: {findings:[{rule_id,redacted_preview,...}], total_findings, findings_by_severity}
    # Detect this BEFORE the generic linter shape so secret-scan output isn't
    # mislabeled as "## Linter" in markdown / slack rendering.
    if isinstance(output.get("findings"), list) and (
        "findings_by_severity" in output
        or "total_findings" in output
        or any(
            isinstance(f, dict)
            and ("redacted_preview" in f or "rule_id" in f or "entropy" in f)
            for f in output.get("findings") or []
        )
    ):
        sections.append(_md_secret_scan(output))
        rendered = True
    # Linter shape: {findings | issues, total | issue_count, fixed_code?}
    elif isinstance(output.get("findings"), list):
        sections.append(_md_linter(output))
        rendered = True

    # Type-checker shape: {errors | diagnostics, total, passed}
    if isinstance(output.get("diagnostics"), list) or (
        isinstance(output.get("errors"), list) and "passed" in output
    ):
        sections.append(_md_type_check(output))
        rendered = True

    # Dependency auditor: {vulnerabilities | findings | packages, ...}
    if isinstance(output.get("vulnerabilities"), list) or isinstance(output.get("packages"), list):
        sections.append(_md_dep_audit(output))
        rendered = True

    # Git-diff analyzer: {file_count, files, risk_summary, summary}
    if "risk_summary" in output and isinstance(output.get("files"), list):
        sections.append(_md_git_diff(output))
        rendered = True

    # Recipe / pipeline: {steps | step_results: {<id>: {output: {...}}}}
    if isinstance(output.get("steps"), list) or isinstance(
        output.get("step_results"), dict
    ):
        sections.append(_md_pipeline(output))
        rendered = True

    if not rendered:
        return _generic_md(output)
    return "\n\n".join(s for s in sections if s.strip())


def _md_code_review(output: dict[str, Any]) -> str:
    score = output.get("score")
    summary = str(output.get("summary") or "").strip()
    issues = output.get("issues") or []
    counts = output.get("severity_counts") or {}
    lines: list[str] = ["## Code Review"]
    if isinstance(score, (int, float)):
        # code_review_agent scores 1-10; some external review agents
        # score 1-100. Disambiguate by magnitude — anything ≤ 10 is the
        # 1-10 scale and gets a /10 denominator.
        denom = 10 if 0 <= score <= 10 else 100
        lines.append(f"**Score:** {score}/{denom}")
    if summary:
        lines.append(f"\n{summary}")
    if counts:
        chip = " · ".join(
            f"{_count_emoji(sev)} {count} {sev}"
            for sev, count in counts.items()
            if count
        )
        if chip:
            lines.append(f"\n{chip}")
    if issues:
        lines.append("\n### Issues")
        for issue in issues[:50]:
            sev = str(issue.get("severity") or "info").lower()
            cat = issue.get("category") or ""
            # Code-review agents use `description`; linters use `message`;
            # generic shapes use `title`. Try all three before falling back.
            text = str(
                issue.get("description")
                or issue.get("title")
                or issue.get("message")
                or ""
            ).strip()
            location = ""
            file_ = issue.get("file") or issue.get("filename")
            line = issue.get("line") or issue.get("line_hint")
            if file_ and line:
                location = f" · `{file_}:{line}`"
            elif file_:
                location = f" · `{file_}`"
            elif line:
                location = f" · line `{line}`"
            cwe = str(issue.get("cwe_id") or "").strip()
            cwe_chip = f" [{cwe}]" if cwe else ""
            head = (
                f"- {_count_emoji(sev)} **{sev}** _{cat}_{cwe_chip}{location} — {text}"
                if cat
                else f"- {_count_emoji(sev)} **{sev}**{cwe_chip}{location} — {text}"
            )
            lines.append(head)
            fix = str(issue.get("fix") or issue.get("suggestion") or "").strip()
            if fix:
                lines.append(f"  - Fix: {fix}")
    positives = output.get("positive_aspects") or []
    if positives:
        lines.append("\n### What's good")
        for p in positives[:10]:
            lines.append(f"- {p}")
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


def _md_linter(output: dict[str, Any]) -> str:
    findings = output.get("findings") or []
    total = (
        output.get("total") if isinstance(output.get("total"), int) else len(findings)
    )
    summary = str(output.get("summary") or "").strip()
    lines: list[str] = ["## Linter"]
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
    if len(pretty) > 4000:
        pretty = pretty[:4000] + "\n…"
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
        # Secret scanner before linter so we don't mislabel "Linter" on a
        # leaked-credentials report.
        if isinstance(output.get("findings"), list) and (
            "findings_by_severity" in output
            or "total_findings" in output
            or any(
                isinstance(f, dict)
                and ("redacted_preview" in f or "rule_id" in f or "entropy" in f)
                for f in output.get("findings") or []
            )
        ):
            return {"blocks": _slack_secret_scan_blocks(output)}
        if isinstance(output.get("findings"), list):
            return {"blocks": _slack_linter_blocks(output)}
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
    return {"type": "section", "text": {"type": "mrkdwn", "text": md[:2900]}}


def _slack_context(md: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": md[:2900]}]}


def _slack_code_review_blocks(output: dict[str, Any]) -> list[dict]:
    blocks: list[dict] = [_slack_header("Code Review")]
    score = output.get("score")
    counts = output.get("severity_counts") or {}
    summary = str(output.get("summary") or "").strip()
    chips = " · ".join(
        f"{_count_emoji(sev)} {n} {sev}" for sev, n in counts.items() if n
    )
    head_bits = []
    if isinstance(score, (int, float)):
        denom = 10 if 0 <= score <= 10 else 100
        head_bits.append(f"*Score:* {score}/{denom}")
    if chips:
        head_bits.append(chips)
    if head_bits:
        blocks.append(_slack_context(" · ".join(head_bits)))
    if summary:
        blocks.append(_slack_section(summary))
    issues = output.get("issues") or []
    if issues:
        blocks.append({"type": "divider"})
        for issue in issues[:30]:
            sev = str(issue.get("severity") or "info").lower()
            cat = str(issue.get("category") or "")
            text = str(
                issue.get("description")
                or issue.get("title")
                or issue.get("message")
                or ""
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
            blocks.append(_slack_section(body))
    return blocks


def _slack_linter_blocks(output: dict[str, Any]) -> list[dict]:
    findings = output.get("findings") or []
    total = (
        output.get("total") if isinstance(output.get("total"), int) else len(findings)
    )
    blocks: list[dict] = [_slack_header("Linter")]
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
            rule = str(f.get("rule") or f.get("code") or "")
            file_ = str(f.get("file") or "")
            line = f.get("line") or ""
            msg = str(f.get("message") or "")
            loc = f"`{file_}:{line}`" if file_ else (f"line `{line}`" if line else "")
            rows.append(f"{_count_emoji(sev)} `{rule}` {loc}\n{msg}")
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
                body += f"\n{desc[:400]}"
            blocks.append(_slack_section(body))
    return blocks


def _split_for_slack(text: str, *, limit: int = 2900) -> list[str]:
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
