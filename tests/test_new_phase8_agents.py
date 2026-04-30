"""Contract tests for the Phase 8 utility agents.

These tests focus on what an Aztea caller actually depends on:
  - structured error envelopes (no raw exceptions to the framework),
  - deterministic core algorithms (regex/secret/diff parsing),
  - schema-fit (presence of required keys),
  - safety bounds (timeout, redaction, refusal of DML in sql_explainer).
"""
from __future__ import annotations

import json

from agents import (
    git_diff_analyzer,
    json_schema_validator,
    regex_tester,
    secret_scanner,
    shell_executor,
    sql_explainer,
)


# ---------- shell_executor regression -----------------------------------

def test_shell_executor_rejects_chained_commands() -> None:
    """The original bug: `cmd1 && cmd2` silently dropped cmd2. Must now reject."""
    try:
        shell_executor.run({"command": "python3 --version && uname -s"})
    except ValueError as exc:
        assert "not permitted" in str(exc).lower()
    else:
        raise AssertionError("shell_executor must reject chained commands")


def test_shell_executor_rejects_semicolons_and_pipes() -> None:
    for bad in ("python3 --version; ls", "python3 -c 'print(1)' | cat", "python3 -V & true"):
        try:
            shell_executor.run({"command": bad})
        except ValueError:
            continue
        raise AssertionError(f"shell_executor must reject {bad!r}")


# ---------- secret_scanner ----------------------------------------------

def test_secret_scanner_finds_aws_key() -> None:
    out = secret_scanner.run({"content": "AWS_KEY=AKIAIOSFODNN7EXAMPLE\n", "filename": ".env"})
    assert out["total_findings"] >= 1
    rule_ids = [f["rule_id"] for f in out["findings"]]
    assert "aws-access-key-id" in rule_ids
    assert out["findings_by_severity"]["critical"] >= 1
    # never echo the full secret back
    for f in out["findings"]:
        assert "AKIAIOSFODNN7EXAMPLE" not in f["redacted_preview"]


def test_secret_scanner_clean_input() -> None:
    out = secret_scanner.run({"content": "x = 1\nprint(x)\n", "min_entropy": 0})
    assert out["total_findings"] == 0
    assert "Clean" in out["summary"]


def test_secret_scanner_rejects_missing_content() -> None:
    out = secret_scanner.run({})
    assert "error" in out
    assert out["error"]["code"] == "secret_scanner.missing_content"


# ---------- json_schema_validator ---------------------------------------

def test_json_schema_validator_valid_doc() -> None:
    out = json_schema_validator.run(
        {
            "document": {"name": "a", "age": 1},
            "schema": {"type": "object", "required": ["name", "age"], "properties": {"age": {"type": "integer"}}},
        }
    )
    assert "error" not in out
    assert out["valid"] is True
    assert out["error_count"] == 0


def test_json_schema_validator_invalid_doc_returns_path() -> None:
    out = json_schema_validator.run(
        {
            "document": {"name": "a", "age": "thirty"},
            "schema": {"type": "object", "properties": {"age": {"type": "integer"}}},
        }
    )
    assert out["valid"] is False
    assert out["error_count"] == 1
    err = out["errors"][0]
    assert err["path"] == "/age"
    assert err["json_path"] == "$.age"
    assert err["validator"] == "type"


def test_json_schema_validator_accepts_string_document() -> None:
    out = json_schema_validator.run(
        {"document": json.dumps({"x": 1}), "schema": {"type": "object", "required": ["x"]}}
    )
    assert out["valid"] is True


def test_json_schema_validator_invalid_schema_returns_error_envelope() -> None:
    out = json_schema_validator.run({"document": {}, "schema": {"type": "not-a-real-type"}})
    assert "error" in out
    assert out["error"]["code"] == "json_schema_validator.invalid_schema"


def test_json_schema_validator_rejects_remote_ref() -> None:
    out = json_schema_validator.run(
        {
            "document": {"x": 1},
            "schema": {"$ref": "https://example.com/schema.json"},
        }
    )
    assert "error" in out
    assert out["error"]["code"] == "json_schema_validator.remote_ref_not_supported"


# ---------- regex_tester -------------------------------------------------

def test_regex_tester_findall() -> None:
    out = regex_tester.run({"pattern": r"\d+", "samples": ["a1b22"], "operation": "findall"})
    assert out["compiled"] is True
    assert out["results"][0]["match_count"] == 2


def test_regex_tester_compile_error_returns_structured() -> None:
    out = regex_tester.run({"pattern": "(unclosed", "samples": ["x"]})
    assert out["compiled"] is False
    assert out["compile_error"]
    assert out["catastrophic_risk"] is False


def test_regex_tester_catastrophic_backtracking_terminates() -> None:
    # Classic ReDoS pattern.
    out = regex_tester.run(
        {
            "pattern": r"^(a+)+$",
            "samples": ["a" * 28 + "b"],
            "timeout_ms_per_sample": 150,
        }
    )
    assert out["compiled"] is True
    assert out["catastrophic_risk"] is True
    assert out["results"][0]["timed_out"] is True


def test_regex_tester_substitution() -> None:
    out = regex_tester.run(
        {"pattern": r"\d+", "samples": ["a1 b22"], "operation": "sub", "replacement": "#"}
    )
    assert out["results"][0]["substitution"] == "a# b#"


# ---------- sql_explainer -----------------------------------------------

def test_sql_explainer_flags_full_scan() -> None:
    out = sql_explainer.run(
        {
            "schema_sql": "CREATE TABLE u(id INTEGER PRIMARY KEY, email TEXT); INSERT INTO u VALUES(1,'a');",
            "queries": ["SELECT * FROM u WHERE email = ?"],
            "params": [["a"]],
        }
    )
    assert out["total_issues"] >= 1
    assert any("Full scan" in i for i in out["queries"][0]["issues"])
    assert any("index" in s.lower() for s in out["queries"][0]["suggestions"])


def test_sql_explainer_rejects_dml() -> None:
    out = sql_explainer.run(
        {"schema_sql": "CREATE TABLE u(id INT);", "queries": ["INSERT INTO u VALUES(1)"]}
    )
    assert "error" in out
    assert out["error"]["code"] == "sql_explainer.dml_not_supported"


def test_sql_explainer_rejects_attach_database() -> None:
    out = sql_explainer.run(
        {
            "schema_sql": "ATTACH DATABASE '/tmp/aztea-test.db' AS x; CREATE TABLE x.t(id INT);",
            "queries": ["SELECT 1"],
        }
    )
    assert "error" in out
    assert out["error"]["code"] == "sql_explainer.unsafe_schema_sql"


def test_sql_explainer_does_not_flag_constant_row_scan() -> None:
    out = sql_explainer.run(
        {
            "schema_sql": "CREATE TABLE u(id INTEGER PRIMARY KEY);",
            "queries": ["SELECT 1"],
        }
    )
    assert out["total_issues"] == 0
    assert out["queries"][0]["issues"] == []


def test_sql_explainer_index_avoids_full_scan() -> None:
    out = sql_explainer.run(
        {
            "schema_sql": (
                "CREATE TABLE u(id INTEGER PRIMARY KEY, email TEXT);"
                "CREATE INDEX idx_u_email ON u(email);"
            ),
            "queries": ["SELECT * FROM u WHERE email = ?"],
            "params": [["a"]],
        }
    )
    # SCAN should be replaced by SEARCH USING INDEX → no full-scan issue.
    issues = out["queries"][0]["issues"]
    assert not any("Full scan" in i for i in issues), issues


# ---------- git_diff_analyzer -------------------------------------------

_AUTH_DIFF = """\
diff --git a/auth/login.py b/auth/login.py
--- a/auth/login.py
+++ b/auth/login.py
@@ -1,4 +1,3 @@
 def login(u, p):
-    try:
-        return verify(u, p)
-    except Exception:
-        return False
+    return verify(u, p)
+    # TODO: rate limit
"""


def test_git_diff_analyzer_classifies_auth_and_error_handling() -> None:
    out = git_diff_analyzer.run({"diff": _AUTH_DIFF})
    assert out["file_count"] == 1
    f = out["files"][0]
    assert "auth" in f["risk_tags"]
    assert out["risk_summary"]["auth_changes"] == 1
    assert out["risk_summary"]["error_handling_removed"] is True
    assert out["risk_summary"]["todos_added"] >= 1


def test_git_diff_analyzer_flags_inline_credential() -> None:
    diff = (
        "diff --git a/config.py b/config.py\n"
        "--- a/config.py\n+++ b/config.py\n"
        "@@ -1,1 +1,2 @@\n key = old\n+aws_key = 'AKIAIOSFODNN7EXAMPLE'\n"
    )
    out = git_diff_analyzer.run({"diff": diff})
    assert out["risk_summary"]["secret_pattern_added"] is True


def test_git_diff_analyzer_rejects_non_diff() -> None:
    out = git_diff_analyzer.run({"diff": "not a diff"})
    assert "error" in out
    assert out["error"]["code"] == "git_diff_analyzer.invalid_format"


def test_git_diff_analyzer_honors_extra_risk_paths() -> None:
    diff = (
        "diff --git a/payments/ledger.py b/payments/ledger.py\n"
        "--- a/payments/ledger.py\n+++ b/payments/ledger.py\n"
        "@@ -1 +1,2 @@\n-x=1\n+y=2\n+# TODO\n"
    )
    out = git_diff_analyzer.run({"diff": diff, "extra_risk_paths": ["payments/*"]})
    assert "custom_path_risk" in out["files"][0]["risk_tags"]
    assert out["risk_summary"]["custom_risk_path_matches"] == 1
