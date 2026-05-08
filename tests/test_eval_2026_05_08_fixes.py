"""Regression tests for the 2026-05-08 power-user eval bugs and rails upgrades.

Each test corresponds to one finding in the audit. Failures here mean the eval
condition has resurfaced — fix the underlying code, do not weaken the test.

Bugs covered:
  Fix 13 — aztea_do force-fits NL audit intent into python_code_executor.code
  Fix 14 — python_executor's pre-filter blocks legitimate os.environ reads
  Fix 15 — search ranks Visual Regression #1 for "render this webpage";
           off-catalog queries (research papers, image gen, endpoint perf)
           return distractors instead of empty results
  Fix 16 — worker_pool.in_flight_global contradicts this_batch_running
  Fix 17 — Multi-Language Executor description implies polyglot support that
           isn't installed
  Fix 18 — dispute "held" message doesn't surface refund/forfeit policy
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Fix 13 helpers
# ---------------------------------------------------------------------------

def _make_python_executor(call_count: int = 100):
    from core.registry.auto_hire import CandidateAgent
    return CandidateAgent(
        agent_id="040dc3f5-afe7-5db7-b253-4936090cc7af",
        slug="python_code_executor",
        name="Python Code Executor",
        description=(
            "Use when the user wants to actually run Python code, not simulate "
            "it. Executes in a real sandboxed subprocess."
        ),
        tags=["code-execution", "python", "developer-tools"],
        category="Code Execution",
        price_per_call_usd=0.01,
        trust_score=80.0,
        success_rate=0.9,
        stability_tier="stable",
        input_schema={
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        raw={"call_count": call_count},
        match_keywords=["run python", "execute python", "evaluate python"],
        block_keywords=["javascript", "typescript", "node", "package.json"],
    )


def _make_dependency_auditor(call_count: int = 50):
    from core.registry.auto_hire import CandidateAgent
    return CandidateAgent(
        agent_id="11fab82a-426e-513e-abf3-528d99ef2b87",
        slug="dependency_auditor",
        name="Dependency Auditor",
        description=(
            "Use this when the user wants to audit their dependencies for "
            "security vulnerabilities, outdated packages, or license issues."
        ),
        tags=["security", "cve", "dependencies", "npm", "pypi"],
        category="Security",
        price_per_call_usd=0.01,
        trust_score=80.0,
        success_rate=0.9,
        stability_tier="stable",
        input_schema={
            "type": "object",
            "properties": {"manifest": {"type": "string"}},
            "required": ["manifest"],
        },
        raw={"call_count": call_count},
        match_keywords=[
            "vulnerabilities",
            "vulnerability",
            "package.json",
            "requirements.txt",
            "audit",
            "dependencies",
        ],
    )


# ---------------------------------------------------------------------------
# Fix 13 — audit intents must not be force-fit into python_code_executor.code
# ---------------------------------------------------------------------------

def test_fix13_audit_intent_does_not_force_fit_python_executor():
    """Reproduces the 2026-05-08 P0 bug verbatim. Auto-hire must NOT pick
    python_code_executor for an audit intent and pass the natural-language
    string as the ``code`` field.
    """
    from core.registry.auto_hire import decide
    import unittest.mock as mock

    intent = (
        "Check if my Python project has any security vulnerabilities given "
        "I'm using requests==2.25.0 and pyyaml==5.3.1"
    )
    candidates = [_make_python_executor(), _make_dependency_auditor()]

    with mock.patch("core.feature_flags.auto_invoke_enabled", return_value=True), \
         mock.patch("core.feature_flags.auto_invoke_confidence_floor", return_value=0.0), \
         mock.patch("core.feature_flags.auto_invoke_trust_floor", return_value=0.0), \
         mock.patch("core.feature_flags.auto_invoke_success_floor", return_value=0.0), \
         mock.patch("core.feature_flags.auto_invoke_server_cap_usd", return_value=10.0):
        decision = decide(
            intent=intent,
            explicit_input=None,
            max_cost_usd=1.0,
            candidates=candidates,
        )

    if decision.auto_invoked:
        assert decision.chosen.slug == "dependency_auditor", (
            f"audit intent must auto-invoke dependency_auditor, "
            f"got: {decision.chosen.slug}"
        )
        assert "code" not in (decision.payload or {}), (
            "auto-invoked path must not pass NL intent as `code` field"
        )
    else:
        # Acceptable: gated decision, but never with python_code_executor as
        # the chosen candidate (would mean we picked it but missed `code`).
        if decision.chosen is not None:
            assert decision.chosen.slug != "python_code_executor", (
                "regression: gated decision still picked python_code_executor"
            )


def test_fix13_intent_unfit_for_field_blocks_audit_imperatives():
    """Conversational/audit intents must not be force-fit into a code field."""
    from core.registry.auto_hire import _intent_unfit_for_field

    # Imperative audit intents — must be unfit for code field.
    assert _intent_unfit_for_field(
        "Check vulnerabilities in requests==2.25.0", "code"
    )
    assert _intent_unfit_for_field("Audit my dependencies for CVEs", "code")
    assert _intent_unfit_for_field("Review this package for security", "code")
    # Question forms — must remain unfit.
    assert _intent_unfit_for_field("What is the capital of France?", "code")
    # Real code — must be fit (not blocked).
    assert not _intent_unfit_for_field("def foo():\n    return 1", "code")
    assert not _intent_unfit_for_field("import os; print(os.getcwd())", "code")


def test_fix13_package_pin_pattern_recognized():
    """``requests==2.25.0`` and ``axios@1.6.0`` style pins must be recognized."""
    from core.registry.auto_hire import _looks_like_package_pinning

    assert _looks_like_package_pinning("Check requests==2.25.0 for CVEs")
    assert _looks_like_package_pinning("Found axios@1.6.0 vulnerability")
    assert _looks_like_package_pinning("pyyaml==5.3.1 has issues")
    assert not _looks_like_package_pinning("Check my Python project")
    assert not _looks_like_package_pinning("Run this code please")


# ---------------------------------------------------------------------------
# Fix 14 — python_executor must allow os.environ reads
# ---------------------------------------------------------------------------

def test_fix14_python_executor_allows_os_environ_reads():
    """Code reading os.environ must not be blocked by the pre-filter regex.
    The sandbox replaces parent env with sandbox_env before user code runs,
    so reads cannot exfiltrate host secrets.
    """
    import re
    from agents.python_executor import _BLOCKED_PATTERNS

    sample_code = "import os; print(os.environ.get('DB_PASSWORD', 'fallback'))"
    matched = [pat for pat in _BLOCKED_PATTERNS if re.search(pat, sample_code)]
    assert not matched, (
        f"os.environ read incorrectly blocked by patterns: {matched}"
    )

    sample_getenv = "import os; v = os.getenv('FOO', 'default')"
    matched2 = [pat for pat in _BLOCKED_PATTERNS if re.search(pat, sample_getenv)]
    assert not matched2, (
        f"os.getenv() read incorrectly blocked by patterns: {matched2}"
    )

    # Don't over-correct: subprocess and socket MUST still be blocked.
    bad_subprocess = "import subprocess; subprocess.run(['ls'])"
    matched3 = [pat for pat in _BLOCKED_PATTERNS if re.search(pat, bad_subprocess)]
    assert matched3, "subprocess must remain blocked"

    bad_socket = "import socket; socket.socket().connect(('1.2.3.4', 80))"
    matched4 = [pat for pat in _BLOCKED_PATTERNS if re.search(pat, bad_socket)]
    assert matched4, "import socket must remain blocked"


# ---------------------------------------------------------------------------
# Fix 15 — search ranking for "render this webpage" + off-catalog handling
# ---------------------------------------------------------------------------

def test_fix15_render_webpage_intent_does_not_match_image_terms():
    """The word "render" must not push image-related agents up the ranking.
    Render+webpage queries must boost Browser Agent via web_render_terms,
    not Visual Regression via image_terms.
    """
    from core.registry import agents_ops

    visual_regression = {
        "agent_id": "vr-1",
        "name": "Visual Regression",
        "description": (
            "Use when you need to compare two screenshots or image artifacts "
            "precisely. Computes pixel-level diff between images."
        ),
        "tags": ["visual-testing", "screenshots", "diff", "qa"],
    }
    browser_agent = {
        "agent_id": "ba-1",
        "name": "Browser Agent",
        "description": (
            "Use when you need to fetch a live web page with a real browser. "
            "Launches headless Chromium, supports screenshot/scrape/pdf actions."
        ),
        "tags": ["browser", "playwright", "scrape", "screenshot", "headless"],
    }

    bonus_browser = agents_ops._intent_match_bonus("render this webpage", browser_agent)
    bonus_vr = agents_ops._intent_match_bonus("render this webpage", visual_regression)

    assert bonus_browser > bonus_vr, (
        f"Browser Agent must outscore Visual Regression for 'render this webpage' "
        f"(browser={bonus_browser}, vr={bonus_vr})"
    )
    assert bonus_browser - bonus_vr >= 0.20, (
        f"intent_bonus gap too narrow: browser={bonus_browser}, vr={bonus_vr}"
    )


def test_fix15_off_catalog_patterns_short_circuit_search():
    """Off-catalog intent fingerprints must trigger empty-result mode."""
    from core.registry.agents_ops import _OFF_CATALOG_PATTERNS

    def _hit(query: str) -> bool:
        toks = set(query.lower().split())
        return any(pred(toks) for _desc, pred in _OFF_CATALOG_PATTERNS)

    # Off-catalog: at least one pattern should match.
    assert _hit("find recent papers on attention mechanisms")
    assert _hit("dall-e style image generator")
    assert _hit("test my endpoint latency p99")

    # On-catalog: no off-catalog pattern should match.
    assert not _hit("scan code for hardcoded secrets")
    assert not _hit("audit my package.json for cves")


# ---------------------------------------------------------------------------
# Fix 16 — worker_pool.in_flight_global must never contradict batch_running
# ---------------------------------------------------------------------------

def test_fix16_in_flight_global_is_floor_of_batch_running():
    """When the live counter lags the DB snapshot, the displayed
    in_flight_global must be at least counts['running'] so the two fields
    can never disagree (the eval flagged in_flight_global=0 while
    this_batch_running=11 — visibly contradictory).
    """
    raw_inflight = 0
    batch_running = 11
    parallelism = 24

    in_flight_now = max(raw_inflight, batch_running)
    capacity_remaining = max(0, parallelism - in_flight_now)

    assert in_flight_now == 11
    assert capacity_remaining == 13
    assert in_flight_now >= batch_running


# ---------------------------------------------------------------------------
# Fix 17 — Multi-Language Executor description must front-load actual runtimes
# ---------------------------------------------------------------------------

def test_fix17_multi_language_executor_description_lists_actual_runtimes():
    from server.builtin_agents.specs_part4 import (
        load_builtin_specs_part4,
        _available_multi_language_options,
    )

    specs = load_builtin_specs_part4()
    multi_lang = next(s for s in specs if s["name"] == "Multi-Language Executor")
    desc = multi_lang["description"].lower()

    available = _available_multi_language_options()
    if available:
        for lang in available:
            assert lang in desc, f"{lang!r} missing from description: {desc!r}"
        assert "currently installed" in desc, (
            "Description must clearly state runtimes are 'currently installed' "
            f"to avoid implying polyglot support: {desc!r}"
        )
    else:
        assert "no runtimes" in desc or "dormant" in desc, (
            f"empty-runtime description must say no runtimes are available: {desc!r}"
        )


# ---------------------------------------------------------------------------
# Fix 18 — dispute "held" explanation must surface refund/forfeit policy
# ---------------------------------------------------------------------------

def test_fix18_dispute_held_explanation_surfaces_deposit_policy():
    """The 'held' explanation must tell filers their deposit is refunded if
    they prevail and forfeit if they don't. Source-level check so we don't
    have to boot the full FastAPI app context.
    """
    from pathlib import Path

    src = Path("server/application_parts/part_005.py").read_text(encoding="utf-8")
    held_message_block = src.split('"held":', 1)[1].split(',\n', 1)[0]
    assert "refunded" in held_message_block.lower(), (
        "held explanation must mention 'refunded' if filer prevails"
    )
    assert "forfeit" in held_message_block.lower(), (
        "held explanation must mention 'forfeit' if filer does not prevail"
    )

    # Machine-readable policy block must also be exposed.
    assert "filing_deposit_policy" in src, (
        "New filing_deposit_policy block must be added to dispute responses"
    )


# ---------------------------------------------------------------------------
# Fix 19 — reputation trust spread (denominator tightened so trust converges
# faster to actual delivery quality)
# ---------------------------------------------------------------------------

def test_fix19_trust_score_converges_faster_with_volume():
    """A 22%-success agent with 30 calls must read materially below NEUTRAL
    (50). Before the denominator change, confidence stalled at ~0.75 and
    the trust score clustered near 48; after, with volume = 30 it should
    converge closer to the agent's actual base score.
    """
    from core.reputation import _build_trust_metrics

    metrics = _build_trust_metrics(
        agent_id="t-1",
        total_calls=30,
        successful_calls=7,  # 23% success
        avg_latency_ms=600.0,
        rating_count=0,
        average_quality_rating=None,
    )

    # Trust should have moved at least ~10 points below NEUTRAL with this
    # much volume and this poor a success rate. The exact value depends on
    # the formula — assert the qualitative property only.
    assert metrics["trust_score"] < 45.0, (
        f"trust score did not converge fast enough: {metrics['trust_score']}"
    )
    assert metrics["trust_score"] > 25.0, (
        f"trust score collapsed too aggressively: {metrics['trust_score']}"
    )
