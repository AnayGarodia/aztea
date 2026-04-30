"""
git_diff_analyzer.py — Parse a unified git diff and produce structured
risk classification: file/hunk/line counts, language breakdown, sensitive-
surface flags (auth, money, migrations, public API), and removed-test or
removed-error-handling detection.

Pure parsing, no LLM. Useful for Claude Code in pre-PR triage flows where
the goal is "tell me what I'm about to ship and how risky it is".

Owns:
  - Splitting a unified diff into files and hunks.
  - Counting added/removed/binary changes.
  - Heuristic risk surfacing on filename + content patterns.

Does NOT own:
  - Fetching the diff. Caller passes the raw text.
  - Reviewing code semantics. That's `code_review_agent`.
  - Running tests / linters on the diff.

Input:
  {
    "diff": str,                # required, max 500 KB
    "extra_risk_paths": [str]   # optional caller-defined globs to flag
  }

Output:
  {
    "file_count": int,
    "hunk_count": int,
    "added_lines": int,
    "removed_lines": int,
    "binary_files": int,
    "files": [
      {
        "path": str,
        "old_path": str | None,
        "change_type": "added" | "removed" | "modified" | "renamed" | "binary",
        "language": str,
        "added": int,
        "removed": int,
        "hunks": int,
        "risk_tags": [str],
        "warnings": [str]
      }
    ],
    "risk_summary": {
      "auth_changes": int,
      "money_changes": int,
      "migration_changes": int,
      "public_api_changes": int,
      "test_files": int,
      "tests_removed": bool,
      "error_handling_removed": bool,
      "secret_pattern_added": bool,
      "todos_added": int
    },
    "summary": str
  }
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any

_MAX_DIFF_CHARS = 500_000

_LANGUAGE_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sql": "sql",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".html": "html",
    ".css": "css",
    ".sh": "shell",
    ".dockerfile": "dockerfile",
    "Dockerfile": "dockerfile",
}

_AUTH_PATH_RE = re.compile(r"(?i)(auth|login|signin|session|jwt|oauth|password|token|permission|rbac|acl)")
_MONEY_PATH_RE = re.compile(r"(?i)(payment|stripe|charge|billing|invoice|wallet|ledger|payout|refund)")
_MIGRATION_PATH_RE = re.compile(r"(?i)(migrations?/|alembic/|schema\.sql$|\.sql$)")
_PUBLIC_API_PATH_RE = re.compile(r"(?i)(routes?/|controllers?/|api/|handlers?/|views?/|endpoints?/)")
_TEST_PATH_RE = re.compile(r"(?i)(^|/)(tests?|__tests__|spec|specs)/|(_|^)test_|\.test\.|\.spec\.")
_DOCKERFILE_RE = re.compile(r"(^|/)Dockerfile(\..+)?$")

_SECRET_INLINE_RE = re.compile(
    r"\b(?:AKIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{36}|sk_(?:live|test)_[A-Za-z0-9]{16,}|"
    r"AIza[0-9A-Za-z_\-]{35}|sk-ant-[A-Za-z0-9_\-]{32,})\b"
)
_ERROR_HANDLING_RE = re.compile(r"^\s*(try:|except\b|raise\b|catch\s*\(|throw\s+)", re.MULTILINE)
_TODO_RE = re.compile(r"\b(?:TODO|FIXME|XXX|HACK)\b")
_DIFF_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_NEW_FILE_RE = re.compile(r"^new file mode")
_DELETED_FILE_RE = re.compile(r"^deleted file mode")
_RENAME_FROM_RE = re.compile(r"^rename from (.+)$")
_RENAME_TO_RE = re.compile(r"^rename to (.+)$")
_BINARY_RE = re.compile(r"^Binary files .+ differ$|^GIT binary patch$")
_HUNK_RE = re.compile(r"^@@ ")


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _detect_language(path: str) -> str:
    if not path:
        return "unknown"
    if _DOCKERFILE_RE.search(path):
        return "dockerfile"
    lower = path.lower()
    for ext, lang in _LANGUAGE_BY_EXT.items():
        if lower.endswith(ext.lower()):
            return lang
    return "other"


def _split_files(diff: str) -> list[list[str]]:
    files: list[list[str]] = []
    current: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if current:
                files.append(current)
            current = [line]
        else:
            if current:
                current.append(line)
    if current:
        files.append(current)
    return files


def _classify_file(file_lines: list[str], extra_risk_paths: list[str] | None = None) -> dict[str, Any]:
    header = file_lines[0] if file_lines else ""
    m = _DIFF_FILE_HEADER_RE.match(header)
    old_path = new_path = None
    if m:
        old_path, new_path = m.group(1), m.group(2)

    change_type = "modified"
    is_binary = False
    hunks = 0
    added = 0
    removed = 0
    added_blob: list[str] = []
    removed_blob: list[str] = []
    rename_from = rename_to = None

    for line in file_lines[1:]:
        if _NEW_FILE_RE.match(line):
            change_type = "added"
        elif _DELETED_FILE_RE.match(line):
            change_type = "removed"
        elif _BINARY_RE.match(line):
            change_type = "binary"
            is_binary = True
        elif (rm := _RENAME_FROM_RE.match(line)):
            rename_from = rm.group(1)
            change_type = "renamed"
        elif (rm := _RENAME_TO_RE.match(line)):
            rename_to = rm.group(1)
        elif _HUNK_RE.match(line):
            hunks += 1
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            added += 1
            added_blob.append(line[1:])
        elif line.startswith("-"):
            removed += 1
            removed_blob.append(line[1:])

    if rename_from and rename_to:
        old_path = rename_from
        new_path = rename_to

    path = new_path or old_path or ""
    language = _detect_language(path)

    risk_tags: list[str] = []
    if _AUTH_PATH_RE.search(path):
        risk_tags.append("auth")
    if _MONEY_PATH_RE.search(path):
        risk_tags.append("money")
    if _MIGRATION_PATH_RE.search(path):
        risk_tags.append("migration")
    if _PUBLIC_API_PATH_RE.search(path):
        risk_tags.append("public_api")
    if _TEST_PATH_RE.search(path):
        risk_tags.append("test")
    if language == "dockerfile":
        risk_tags.append("dockerfile")
    matched_custom_globs = [
        pattern
        for pattern in (extra_risk_paths or [])
        if fnmatch.fnmatch(path, pattern) or (old_path and fnmatch.fnmatch(old_path, pattern))
    ]
    if matched_custom_globs:
        risk_tags.append("custom_path_risk")

    warnings: list[str] = []
    added_text = "\n".join(added_blob)
    removed_text = "\n".join(removed_blob)

    if _SECRET_INLINE_RE.search(added_text):
        warnings.append("Possible credential pattern added in this diff.")

    added_eh = len(_ERROR_HANDLING_RE.findall(added_text))
    removed_eh = len(_ERROR_HANDLING_RE.findall(removed_text))
    if removed_eh > added_eh and removed_eh > 0:
        warnings.append(
            f"Net error-handling decrease: {removed_eh} removed vs {added_eh} added (try/except/raise/catch/throw)."
        )

    if "test" in risk_tags and change_type == "removed":
        warnings.append("Test file deleted entirely.")

    todos_added = len(_TODO_RE.findall(added_text))
    if todos_added:
        warnings.append(f"{todos_added} new TODO/FIXME/XXX/HACK comment(s) added.")
    if matched_custom_globs:
        warnings.append(
            "Matched caller-defined risk path pattern(s): "
            + ", ".join(sorted(dict.fromkeys(matched_custom_globs)))
        )

    return {
        "path": path,
        "old_path": old_path if (old_path and old_path != new_path) else None,
        "change_type": change_type,
        "language": language,
        "added": added,
        "removed": removed,
        "hunks": hunks,
        "is_binary": is_binary,
        "risk_tags": risk_tags,
        "warnings": warnings,
        "_added_text": added_text,
        "_removed_text": removed_text,
        "_todos_added": todos_added,
        "_custom_glob_matches": matched_custom_globs,
    }


def run(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return _err("git_diff_analyzer.invalid_payload", "payload must be an object")

    diff = payload.get("diff")
    if not isinstance(diff, str) or not diff.strip():
        return _err("git_diff_analyzer.missing_diff", "'diff' is required and must be a non-empty unified-diff string")
    if len(diff) > _MAX_DIFF_CHARS:
        return _err(
            "git_diff_analyzer.diff_too_large",
            f"diff exceeds {_MAX_DIFF_CHARS} chars (got {len(diff)})",
        )
    if not diff.lstrip().startswith("diff --git"):
        return _err(
            "git_diff_analyzer.invalid_format",
            "diff does not appear to be in unified `git diff` format (must start with 'diff --git ...')",
        )
    extra_risk_paths = payload.get("extra_risk_paths") or []
    if not isinstance(extra_risk_paths, list):
        return _err("git_diff_analyzer.invalid_extra_risk_paths", "extra_risk_paths must be a list of glob strings")
    normalized_risk_paths: list[str] = []
    for index, pattern in enumerate(extra_risk_paths):
        if not isinstance(pattern, str) or not pattern.strip():
            return _err(
                "git_diff_analyzer.invalid_extra_risk_paths",
                f"extra_risk_paths[{index}] must be a non-empty string",
            )
        normalized_risk_paths.append(pattern.strip())

    file_blocks = _split_files(diff)
    files_out: list[dict[str, Any]] = []
    total_added = total_removed = total_hunks = total_binary = 0
    risk_summary = {
        "auth_changes": 0,
        "money_changes": 0,
        "migration_changes": 0,
        "public_api_changes": 0,
        "test_files": 0,
        "tests_removed": False,
        "error_handling_removed": False,
        "secret_pattern_added": False,
        "todos_added": 0,
        "custom_risk_path_matches": 0,
    }

    for block in file_blocks:
        info = _classify_file(block, normalized_risk_paths)
        total_added += info["added"]
        total_removed += info["removed"]
        total_hunks += info["hunks"]
        if info["is_binary"]:
            total_binary += 1

        if "auth" in info["risk_tags"]:
            risk_summary["auth_changes"] += 1
        if "money" in info["risk_tags"]:
            risk_summary["money_changes"] += 1
        if "migration" in info["risk_tags"]:
            risk_summary["migration_changes"] += 1
        if "public_api" in info["risk_tags"]:
            risk_summary["public_api_changes"] += 1
        if "test" in info["risk_tags"]:
            risk_summary["test_files"] += 1
            if info["change_type"] == "removed":
                risk_summary["tests_removed"] = True

        if any("error-handling decrease" in w for w in info["warnings"]):
            risk_summary["error_handling_removed"] = True
        if any("credential pattern" in w for w in info["warnings"]):
            risk_summary["secret_pattern_added"] = True

        risk_summary["todos_added"] += int(info.pop("_todos_added", 0))
        risk_summary["custom_risk_path_matches"] += len(info.pop("_custom_glob_matches", []))
        info.pop("_added_text", None)
        info.pop("_removed_text", None)

        files_out.append(info)

    bullet_points: list[str] = []
    bullet_points.append(f"{len(files_out)} file(s), {total_hunks} hunk(s), +{total_added}/-{total_removed} lines.")
    if risk_summary["auth_changes"]:
        bullet_points.append(f"{risk_summary['auth_changes']} auth-surface file(s) touched.")
    if risk_summary["money_changes"]:
        bullet_points.append(f"{risk_summary['money_changes']} money-surface file(s) touched.")
    if risk_summary["migration_changes"]:
        bullet_points.append(f"{risk_summary['migration_changes']} migration file(s) included.")
    if risk_summary["secret_pattern_added"]:
        bullet_points.append("⚠ Possible credential added — review immediately.")
    if risk_summary["tests_removed"]:
        bullet_points.append("⚠ One or more test files deleted entirely.")
    if risk_summary["error_handling_removed"]:
        bullet_points.append("⚠ Net error-handling decrease detected.")
    if total_binary:
        bullet_points.append(f"{total_binary} binary file(s) changed.")

    summary = " ".join(bullet_points)

    return {
        "file_count": len(files_out),
        "hunk_count": total_hunks,
        "added_lines": total_added,
        "removed_lines": total_removed,
        "binary_files": total_binary,
        "files": files_out,
        "risk_summary": risk_summary,
        "summary": summary,
    }
