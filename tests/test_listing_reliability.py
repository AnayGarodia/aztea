"""Reliability collector: schema-validation block, repeat-probe flakiness, dry-run."""
from __future__ import annotations

import core.listing_reliability as reliability

_OBJECT_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


# ---- schema validation (inline, blocking) --------------------------------


def test_schema_valid_response_no_findings():
    assert reliability.validate_response_against_schema({"answer": "hi"}, _OBJECT_SCHEMA) == []


def test_schema_wrong_type_blocks():
    findings = reliability.validate_response_against_schema({"answer": 123}, _OBJECT_SCHEMA)
    assert [f.code for f in findings] == [reliability.CODE_SCHEMA_INVALID]
    assert findings[0].level == "block"


def test_schema_missing_required_blocks():
    findings = reliability.validate_response_against_schema({}, _OBJECT_SCHEMA)
    assert findings and findings[0].level == "block"


def test_empty_schema_skips_validation():
    assert reliability.validate_response_against_schema({"x": 1}, {}) == []
    assert reliability.validate_response_against_schema("not-json", None) == []


# ---- repeat-probe flakiness (advisory) -----------------------------------


class _Resp:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}

    def iter_content(self, chunk_size=8192, decode_unicode=False):
        yield b"{}"


def _post_factory(statuses):
    seq = iter(statuses)

    def _post(url, **kwargs):
        status = next(seq)
        if status == "error":
            raise RuntimeError("boom")
        return _Resp(status)

    return _post


def test_repeat_probe_all_succeed_no_finding():
    findings = reliability.probe_repeatability(
        "https://x.test", {}, http_post=_post_factory([200, 200, 200]), samples=3,
    )
    assert findings == []


def test_repeat_probe_flaky_warns():
    findings = reliability.probe_repeatability(
        "https://x.test", {}, http_post=_post_factory([200, "error", 500]), samples=3,
    )
    assert [f.code for f in findings] == [reliability.CODE_FLAKY]
    assert findings[0].level == "warn"  # advisory, never blocks


# ---- skill dry-run (advisory) --------------------------------------------


def test_skill_dry_run_no_llm_is_silent():
    # No provider configured -> degrade to no finding (never a false "flaky").
    findings = reliability.skill_dry_run({}, {}, llm_available=False)
    assert findings == []


def test_skill_dry_run_execution_error_warns():
    def boom_executor(skill, payload):
        raise RuntimeError("skill blew up")

    findings = reliability.skill_dry_run(
        {"system_prompt": "x"}, {}, executor=boom_executor, llm_available=True,
    )
    assert [f.code for f in findings] == [reliability.CODE_DRY_RUN_FAILED]
    assert findings[0].level == "warn"


def test_skill_dry_run_clean_run_no_finding():
    def ok_executor(skill, payload):
        return {"result": "ok"}

    findings = reliability.skill_dry_run(
        {"system_prompt": "x"}, {}, executor=ok_executor, llm_available=True,
    )
    assert findings == []
