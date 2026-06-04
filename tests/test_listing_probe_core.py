"""Direct tests for the extracted probe core, incl. the schema_validator seam."""
from __future__ import annotations

import json

import core.listing_probe_core as pc

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"task": {"type": "string"}},
    "required": ["task"],
}


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.headers = {}


def _read(resp):
    return resp._body


# ---- probe_once ----------------------------------------------------------


def test_probe_once_transport_error_returns_none():
    def http_post(url, **kw):
        raise RuntimeError("boom")

    assert pc.probe_once(
        "u", {}, http_post=http_post, read_body=_read, timeout=1, headers={}, job_id="j",
    ) is None


def test_probe_once_5xx_is_not_ok():
    r = pc.probe_once(
        "u", {}, http_post=lambda url, **kw: _Resp(503, {}), read_body=_read,
        timeout=1, headers={}, job_id="j",
    )
    assert r is not None and r.ok is False


def test_probe_once_200_is_ok_with_body():
    r = pc.probe_once(
        "u", {}, http_post=lambda url, **kw: _Resp(200, {"a": 1}), read_body=_read,
        timeout=1, headers={}, job_id="j",
    )
    assert r.ok is True and r.body == {"a": 1}


# ---- run_probe_suite + schema_validator injection ------------------------


def test_schema_validator_runs_on_synthetic_probe_only():
    posts = []

    def http_post(url, **kw):
        posts.append(kw)
        return _Resp(200, {"result": "ok"})

    validator_calls = []

    def validator(body, schema):
        validator_calls.append((body, schema))
        return []

    out_schema = {"type": "object", "properties": {"result": {"type": "string"}}}
    result = pc.run_probe_suite(
        "https://x.test",
        input_schema=_INPUT_SCHEMA,
        output_schema=out_schema,
        http_post=http_post,
        read_body=_read,
        timeout=1,
        schema_validator=validator,
    )
    # 1 synthetic + 3 adversarial probes, all answered.
    assert result.payloads_attempted == 4
    assert result.successful_probes == 4
    # Validator fires exactly once — on the synthetic payload (index 0), not adversarial.
    assert len(validator_calls) == 1
    assert validator_calls[0] == ({"result": "ok"}, out_schema)


def test_schema_validator_findings_propagate():
    from core.listing_safety import LEVEL_BLOCK, VerificationFinding

    def validator(body, schema):
        return [VerificationFinding("listing.unreliable.schema", LEVEL_BLOCK, "bad")]

    result = pc.run_probe_suite(
        "https://x.test", input_schema=_INPUT_SCHEMA,
        output_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        http_post=lambda url, **kw: _Resp(200, {"wrong": 1}), read_body=_read,
        timeout=1, schema_validator=validator,
    )
    assert any(f.code == "listing.unreliable.schema" for f in result.findings)


def test_no_validator_means_no_schema_findings():
    result = pc.run_probe_suite(
        "https://x.test", input_schema=_INPUT_SCHEMA, output_schema={"type": "object"},
        http_post=lambda url, **kw: _Resp(200, {"ok": 1}), read_body=_read, timeout=1,
    )
    assert all("unreliable.schema" not in f.code for f in result.findings)


# ---- read_probe_body -----------------------------------------------------


class _StreamResp:
    def __init__(self, data: bytes):
        self._data = data
        self.text = data.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=8192, decode_unicode=False):
        yield self._data


def test_read_probe_body_parses_json():
    assert pc.read_probe_body(_StreamResp(json.dumps({"x": 1}).encode())) == {"x": 1}


def test_read_probe_body_falls_back_to_string():
    assert pc.read_probe_body(_StreamResp(b"plain text")) == "plain text"
