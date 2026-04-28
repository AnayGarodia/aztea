"""
github_fetcher.py — Fetch files from public GitHub repositories

Input:  {
  "repo": "owner/repo",
  "paths": ["src/main.py", "README.md"],
  "branch": "main",         # optional, default "main"
  "summarize": false        # optional; if True, LLM synthesizes the fetched content
}
Output: {
  "repo": str,
  "branch": str,
  "files": [
    {
      "path": str,
      "content": str | None,
      "size_bytes": int,
      "encoding": "utf-8",   # only when content is not None
      "error": str           # only when content is None
    }
  ],
  "summary": str | None,
  "billing_units_actual": int   # count of successfully fetched files
}
"""

import os
import posixpath

import httpx

from agents._contracts import annotate_success
from core.llm import CompletionRequest, Message, run_with_fallback

_RAW_BASE = "https://raw.githubusercontent.com"
_API_BASE = "https://api.github.com"
_TIMEOUT = 10
_MAX_PATHS = 20
_SUMMARY_TRUNCATE = 800


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _github_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    h = {"User-Agent": "aztea-github-fetcher/1.0", "Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _detect_default_branch(owner: str, repo_name: str) -> str:
    try:
        resp = httpx.get(
            f"{_API_BASE}/repos/{owner}/{repo_name}",
            headers=_github_headers(),
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json().get("default_branch", "main")
    except Exception:
        pass
    return "main"

_SYSTEM = """\
You are a senior software engineer reviewing source files from a GitHub repository.
Given the contents of one or more files, explain the repository's purpose,
its high-level architecture, and key patterns used in the code.
Be concise and direct — 3-5 sentences maximum."""

_USER = """\
Repository: {repo} (branch: {branch})

Files fetched:
{files_block}

Describe the repository's purpose, architecture, and key patterns."""


def run(payload: dict) -> dict:
    """Deprecated LLM-only wrapper — sunset 2026-07-26.

    Fetches GitHub repo metadata and file contents for the given ``repo`` (owner/repo)
    and ``paths`` list, then returns an LLM-generated repo info dict.
    """
    repo = str(payload.get("repo", "")).strip()
    if not repo:
        return _err("github_fetcher.missing_repo", "repo is required (format: owner/repo)")
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return _err("github_fetcher.invalid_repo", "repo must be in 'owner/repo' format")
    owner, repo_name = parts[0], parts[1]

    raw_paths = payload.get("paths")
    if not raw_paths or not isinstance(raw_paths, list):
        return _err("github_fetcher.invalid_paths", "paths must be a non-empty list of file path strings")
    if len(raw_paths) > _MAX_PATHS:
        return _err("github_fetcher.too_many_paths", f"paths list exceeds maximum of {_MAX_PATHS} entries")

    branch_raw = str(payload.get("branch", "")).strip()
    branch = branch_raw if branch_raw else _detect_default_branch(owner, repo_name)

    # Validate branch contains no special characters
    if any(c in branch for c in ("?", "#", "..", "/")):
        return _err("github_fetcher.invalid_branch", "branch name contains invalid characters")

    # Sanitize each path: normalize to prevent traversal
    sanitized_paths = []
    for p in raw_paths:
        normalized = posixpath.normpath("/" + str(p).strip()).lstrip("/")
        if not normalized or normalized == ".":
            return _err("github_fetcher.invalid_path", f"invalid path: {p!r}")
        sanitized_paths.append(normalized)
    paths = sanitized_paths

    if not paths:
        return _err("github_fetcher.no_valid_paths", "paths list contains no valid entries")

    summarize = bool(payload.get("summarize", False))

    files: list[dict] = []
    for path in paths:
        url = f"{_RAW_BASE}/{owner}/{repo_name}/{branch}/{path}"
        try:
            resp = httpx.get(url, headers=_github_headers(), timeout=_TIMEOUT, follow_redirects=True)
            if resp.status_code == 200:
                files.append({
                    "path": path,
                    "content": resp.text,
                    "size_bytes": len(resp.content),
                    "encoding": "utf-8",
                })
            else:
                files.append({
                    "path": path,
                    "content": None,
                    "size_bytes": 0,
                    "error": f"HTTP {resp.status_code}",
                })
        except httpx.TimeoutException:
            files.append({
                "path": path,
                "content": None,
                "size_bytes": 0,
                "error": "Request timed out",
            })
        except Exception as exc:
            files.append({
                "path": path,
                "content": None,
                "size_bytes": 0,
                "error": f"{type(exc).__name__}: {exc}",
            })

    successful = [f for f in files if f.get("content") is not None]

    summary: str | None = None
    degraded_mode = False
    llm_used = False
    if summarize and successful:
        files_block = "\n\n".join(
            f"--- {f['path']} ---\n{f['content'][:_SUMMARY_TRUNCATE]}"
            for f in successful
        )
        req = CompletionRequest(
            model="",
            messages=[
                Message(role="system", content=_SYSTEM),
                Message(
                    role="user",
                    content=_USER.format(
                        repo=f"{owner}/{repo_name}",
                        branch=branch,
                        files_block=files_block[:8000],
                    ),
                ),
            ],
            temperature=0.15,
            max_tokens=600,
        )
        try:
            raw = run_with_fallback(req)
            summary = raw.text.strip()
            llm_used = True
        except Exception:
            summary = None
            degraded_mode = True

    return annotate_success({
        "repo": f"{owner}/{repo_name}",
        "branch": branch,
        "branch_auto_detected": not bool(branch_raw),
        "files": files,
        "summary": summary,
        "billing_units_actual": len(successful),
    }, llm_used=llm_used, degraded_mode=degraded_mode)
