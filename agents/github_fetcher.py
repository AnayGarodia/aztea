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

import posixpath

import httpx

from core.llm import CompletionRequest, Message, run_with_fallback

_RAW_BASE = "https://raw.githubusercontent.com"
_TIMEOUT = 10
_MAX_PATHS = 20
_SUMMARY_TRUNCATE = 800

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
    repo = str(payload.get("repo", "")).strip()
    if not repo:
        return {"error": "repo is required (format: owner/repo)"}
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return {"error": "repo must be in 'owner/repo' format"}
    owner, repo_name = parts[0], parts[1]

    raw_paths = payload.get("paths")
    if not raw_paths or not isinstance(raw_paths, list):
        return {"error": "paths must be a non-empty list of file path strings"}
    if len(raw_paths) > _MAX_PATHS:
        return {"error": f"paths list exceeds maximum of {_MAX_PATHS} entries"}

    branch = str(payload.get("branch", "main")).strip() or "main"

    # Validate branch contains no special characters
    if any(c in branch for c in ("?", "#", "..", "/")):
        return {"error": "branch name contains invalid characters"}

    # Sanitize each path: normalize to prevent traversal
    sanitized_paths = []
    for p in raw_paths:
        normalized = posixpath.normpath("/" + str(p).strip()).lstrip("/")
        if not normalized or normalized == ".":
            return {"error": f"invalid path: {p!r}"}
        sanitized_paths.append(normalized)
    paths = sanitized_paths

    if not paths:
        return {"error": "paths list contains no valid entries"}

    summarize = bool(payload.get("summarize", False))

    files: list[dict] = []
    for path in paths:
        url = f"{_RAW_BASE}/{owner}/{repo_name}/{branch}/{path}"
        try:
            resp = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True)
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
        raw = run_with_fallback(req)
        summary = raw.text.strip()

    return {
        "repo": f"{owner}/{repo_name}",
        "branch": branch,
        "files": files,
        "summary": summary,
        "billing_units_actual": len(successful),
    }
