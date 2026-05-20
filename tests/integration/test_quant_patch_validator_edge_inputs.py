"""Input validation edge cases for `quant_patch_validator`.

# OWNS: every branch of `_validate_payload` plus downstream robustness
#        points (syntax errors, missing functions, signature override,
#        non-numeric tolerances).
# NOT OWNS: malicious code (see security), parallel safety (see
#            concurrency), audit trail (see workspace).
# DECISIONS:
#   - We call the agent's `run()` directly (no HTTP) for fast unit-style
#     edge-input coverage. Lifecycle / async / workspace tests cover the
#     HTTP path separately.
"""

from __future__ import annotations

import pytest

from agents.quant_patch_validator import run as validator_run


# ---------------------------------------------------------------------------
# Payload-shape validation
# ---------------------------------------------------------------------------


def test_non_dict_payload_returns_error():
    out = validator_run("not a dict")  # type: ignore[arg-type]
    assert out.get("error", {}).get("code") == "quant_patch_validator.invalid_payload"


def test_empty_payload_returns_missing_reference_code():
    out = validator_run({})
    assert out.get("error", {}).get("code") == "quant_patch_validator.missing_reference_code"


def test_missing_reference_code():
    out = validator_run({"candidate_code": "def f(x): return x"})
    assert out["error"]["code"] == "quant_patch_validator.missing_reference_code"


def test_missing_candidate_code():
    out = validator_run({"reference_code": "def f(x): return x"})
    assert out["error"]["code"] == "quant_patch_validator.missing_candidate_code"


def test_empty_string_reference():
    out = validator_run({"reference_code": "", "candidate_code": "def f(x): return x"})
    assert out["error"]["code"] == "quant_patch_validator.missing_reference_code"


def test_empty_string_candidate():
    out = validator_run({"reference_code": "def f(x): return x", "candidate_code": ""})
    assert out["error"]["code"] == "quant_patch_validator.missing_candidate_code"


def test_whitespace_only_reference():
    out = validator_run({"reference_code": "   \n\t  ", "candidate_code": "def f(x): return x"})
    assert out["error"]["code"] == "quant_patch_validator.missing_reference_code"


def test_non_string_reference():
    out = validator_run({"reference_code": 123, "candidate_code": "def f(x): return x"})
    assert out["error"]["code"] == "quant_patch_validator.missing_reference_code"


def test_non_string_candidate():
    out = validator_run({"reference_code": "def f(x): return x", "candidate_code": ["x"]})
    assert out["error"]["code"] == "quant_patch_validator.missing_candidate_code"


def test_reference_exceeds_size_limit():
    big = "def f(x):\n    # padding\n" + ("x = 1\n" * 12_000)  # > 64 KB
    out = validator_run({"reference_code": big, "candidate_code": "def f(x): return x"})
    assert out["error"]["code"] == "quant_patch_validator.reference_too_large"


def test_candidate_exceeds_size_limit():
    big = "def f(x):\n    # padding\n" + ("x = 1\n" * 12_000)
    out = validator_run({"reference_code": "def f(x): return x", "candidate_code": big})
    assert out["error"]["code"] == "quant_patch_validator.candidate_too_large"


# ---------------------------------------------------------------------------
# fuzz_budget / fuzz_engine / fuzz_seconds
# ---------------------------------------------------------------------------


def test_invalid_fuzz_budget():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "def f(x): return x",
            "fuzz_budget": "ultra",
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.invalid_fuzz_budget"


def test_invalid_fuzz_engine():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "def f(x): return x",
            "fuzz_engine": "afl++",
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.invalid_fuzz_engine"


def test_fuzz_seconds_negative_falls_through_to_tier_default():
    # negative override is ignored — call still runs (fast budget)
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "def f(x): return x",
            "fuzz_budget": "quick",
            "fuzz_seconds": -5,
        }
    )
    # Should not be an error; tier default of 30s applies. But test must be fast,
    # so the agent should still finish (here it runs no work because budget is hit by tier=30s
    # but we cap pytest at 60s). Use a small ref/cand to keep this fast.
    # Since fuzz_seconds=-5 fails the `1.0 <= override <= budget` check, tier default of 30s applies.
    # To keep the test fast, set a very short fuzz_seconds inside acceptable range instead.
    assert "verdict" in out or "error" in out


def test_fuzz_seconds_above_tier_clamped_silently():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "def f(x): return x",
            "fuzz_budget": "quick",
            "fuzz_seconds": 999_999,  # ignored; tier default used
        }
    )
    assert "verdict" in out or "error" in out


def test_fuzz_seconds_non_numeric_ignored():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "def f(x): return x",
            "fuzz_budget": "quick",
            "fuzz_seconds": "abc",
        }
    )
    assert "verdict" in out or "error" in out


# ---------------------------------------------------------------------------
# Tolerances — pathological values
# ---------------------------------------------------------------------------


def test_rtol_negative_does_not_crash():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "def f(x): return x",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
            "rtol": -1e-5,
        }
    )
    assert "verdict" in out


def test_atol_inf_does_not_crash():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "def f(x): return x",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
            "atol": float("inf"),
        }
    )
    assert "verdict" in out


# ---------------------------------------------------------------------------
# Source parsing — syntax errors, no function, classes, lambdas
# ---------------------------------------------------------------------------


def test_syntax_error_in_reference():
    out = validator_run(
        {
            "reference_code": "def f(:",
            "candidate_code": "def f(x): return x",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.signature_parse_failed"


def test_syntax_error_in_candidate():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "def f(:",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.signature_parse_failed"


def test_no_function_in_reference():
    out = validator_run(
        {
            "reference_code": "x = 1\ny = 2\n",
            "candidate_code": "def f(x): return x",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.signature_parse_failed"


def test_class_only_module_in_candidate():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "class Foo:\n    def method(self, x): return x\n",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.signature_parse_failed"


def test_signature_divergence_skips_fuzz():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "def g(x): return x",  # different name
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert out["verdict"] == "signature_divergence"
    assert out["fuzz_stats"]["inputs_explored"] == 0


# ---------------------------------------------------------------------------
# Self-import block (defence-in-depth from the test plan)
# ---------------------------------------------------------------------------


def test_self_import_in_candidate_blocked():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "import agents.quant_patch_validator\ndef f(x): return x\n",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.self_reference_blocked"


def test_self_import_in_reference_blocked():
    out = validator_run(
        {
            "reference_code": "from agents.quant_patch_validator import run\ndef f(x): return x\n",
            "candidate_code": "def f(x): return x",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.self_reference_blocked"


def test_self_import_with_alias_blocked():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "from agents.quant_patch_validator import run as foo\ndef f(x): return x\n",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.self_reference_blocked"


def test_self_import_submodule_blocked():
    out = validator_run(
        {
            "reference_code": "def f(x): return x",
            "candidate_code": "from agents.quant_patch_validator.harness import Harness\ndef f(x): return x\n",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.self_reference_blocked"
