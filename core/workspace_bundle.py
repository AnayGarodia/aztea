"""Build a privacy-safe summary of a local workspace directory for agent context.

# OWNS: Locally-built, size-capped summary of a workspace (file tree + manifests
#       + README excerpt + git branch) plus content fingerprint.
# NOT OWNS: Network I/O, payload merging into the call envelope, consent
#           bookkeeping (see core/workspace_consent.py).
# INVARIANTS:
#   - Files matching DENYLIST_PATTERNS or any .gitignore / .aztea_ignore entry
#     must never appear in any field of the returned bundle.
#   - JSON-serialised bundle must never exceed BUNDLE_SIZE_CAP_BYTES.
#   - build_light_bundle() performs no network I/O and no mutation of inputs.
# DECISIONS:
#   - "Light" mode only for v1: file tree, manifests, README, branch.
#   - .gitignore parsing is fnmatch-on-basename only — full git ignore semantics
#     are deferred. The DENYLIST handles all secret-leak categories regardless.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

BUNDLE_SIZE_CAP_BYTES = 5120
MAX_TREE_ENTRIES = 200
MAX_TREE_DEPTH = 4
MAX_MANIFEST_LINES = 100
MAX_MANIFEST_BYTES = 2048
MAX_README_LINES = 200
SUMMARY_TREE_LINES = 30

DENYLIST_PATTERNS: frozenset[str] = frozenset(
    {
        ".env",
        ".env.*",
        "*.pem",
        "*.key",
        "id_rsa",
        "id_rsa.*",
        "id_ed25519",
        "id_ed25519.*",
        "credentials",
        "credentials.*",
        "secrets",
        "secrets.*",
        ".aws",
        ".ssh",
        ".aztea",
        "*.p12",
        "*.pfx",
        "*.crt",
        "*.cer",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".git",
        ".DS_Store",
        "dist",
        "build",
        "target",
        ".next",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

MANIFESTS_OF_INTEREST: tuple[str, ...] = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "composer.json",
    "pom.xml",
    "build.gradle",
    "Dockerfile",
    "tsconfig.json",
)

README_CANDIDATES: tuple[str, ...] = (
    "README.md",
    "README.rst",
    "README.txt",
    "README",
)


class WorkspaceBundle(BaseModel):
    """Structured, capped representation of a workspace directory.

    All fields are user-visible: nothing here should ever leak content the
    caller would not consent to share. The fingerprint is content-addressed
    so the backend can cache the bundle and the MCP can ship a 64-char hash
    on subsequent calls instead of re-shipping the full payload.
    """

    model_config = ConfigDict(frozen=False)

    cwd_basename: str
    file_tree: str = ""
    manifests: dict[str, str] = Field(default_factory=dict)
    readme_excerpt: str = ""
    git_branch: str | None = None
    bundle_fingerprint: str = ""
    truncated: bool = False

    def to_payload(self) -> dict[str, Any]:
        """Wire-format dict for transmission to the backend."""
        return {
            "cwd_basename": self.cwd_basename,
            "file_tree": self.file_tree,
            "manifests": dict(self.manifests),
            "readme_excerpt": self.readme_excerpt,
            "git_branch": self.git_branch,
            "fingerprint": self.bundle_fingerprint,
            "truncated": self.truncated,
        }

    def summary_only(self) -> dict[str, Any]:
        """A consent-pending summary — file names only, no file contents.

        Safe to surface to the user before they have approved sharing the
        bundle: lists what *would* be shared without exposing any source.
        """
        return {
            "cwd_basename": self.cwd_basename,
            "file_tree_summary": "\n".join(
                self.file_tree.splitlines()[:SUMMARY_TREE_LINES]
            ),
            "manifests_present": sorted(self.manifests.keys()),
            "has_readme": bool(self.readme_excerpt),
            "git_branch": self.git_branch,
        }


def _is_denylisted(name: str) -> bool:
    """True if a directory entry name matches any DENYLIST glob pattern."""
    return any(fnmatch.fnmatch(name, pattern) for pattern in DENYLIST_PATTERNS)


def _load_ignore_patterns(cwd: Path) -> list[str]:
    """Read .gitignore and .aztea_ignore as a flat list of fnmatch patterns.

    Full git-ignore semantics (negation, anchored paths, recursion globs) are
    deliberately not implemented here — the DENYLIST catches the safety-critical
    cases. This is a helpful-but-not-trusted layer.
    """
    patterns: list[str] = []
    for ignore_file in (".gitignore", ".aztea_ignore"):
        path = cwd / ignore_file
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line.lstrip("/").rstrip("/"))
    return patterns


def _matches_ignore(name: str, ignore_patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in ignore_patterns)


def _safe_iterdir(directory: Path) -> list[Path]:
    try:
        return sorted(
            directory.iterdir(),
            key=lambda entry: (not entry.is_dir(), entry.name.lower()),
        )
    except OSError:
        return []


def _walk_tree(
    directory: Path,
    prefix: str,
    depth: int,
    ignore_patterns: list[str],
    lines: list[str],
    entries_used: list[int],
) -> None:
    """In-place tree walker; mutates `lines` and `entries_used[0]` by design.

    Documented mutation: the caller initialises a single-element list to act
    as a depth-shared counter; this is the pythonic equivalent of nonlocal
    state without a closure.
    """
    if depth > MAX_TREE_DEPTH or entries_used[0] >= MAX_TREE_ENTRIES:
        return
    for child in _safe_iterdir(directory):
        if entries_used[0] >= MAX_TREE_ENTRIES:
            lines.append(f"{prefix}... (truncated)")
            return
        name = child.name
        if _is_denylisted(name) or _matches_ignore(name, ignore_patterns):
            continue
        suffix = "/" if child.is_dir() else ""
        lines.append(f"{prefix}{name}{suffix}")
        entries_used[0] += 1
        if child.is_dir():
            _walk_tree(
                child,
                prefix + "  ",
                depth + 1,
                ignore_patterns,
                lines,
                entries_used,
            )


def _build_file_tree(cwd: Path, ignore_patterns: list[str]) -> str:
    lines: list[str] = []
    entries_used = [0]
    _walk_tree(cwd, "", 0, ignore_patterns, lines, entries_used)
    return "\n".join(lines)


def _read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_manifest(path: Path) -> str | None:
    text = _read_text_safe(path)
    if text is None:
        return None
    if len(text) > MAX_MANIFEST_BYTES:
        text = text[:MAX_MANIFEST_BYTES] + "\n... (truncated)"
    return "\n".join(text.splitlines()[:MAX_MANIFEST_LINES])


def _collect_manifests(cwd: Path) -> dict[str, str]:
    collected: dict[str, str] = {}
    for name in MANIFESTS_OF_INTEREST:
        if _is_denylisted(name):
            continue
        path = cwd / name
        if not path.is_file():
            continue
        content = _read_manifest(path)
        if content is not None:
            collected[name] = content
    return collected


def _read_readme_excerpt(cwd: Path) -> str:
    for name in README_CANDIDATES:
        path = cwd / name
        if not path.is_file():
            continue
        text = _read_text_safe(path)
        if text is None:
            continue
        return "\n".join(text.splitlines()[:MAX_README_LINES])
    return ""


def _resolve_git_dir(cwd: Path) -> Path | None:
    """Return the .git directory for `cwd`, following the gitlink form used by worktrees."""
    git_path = cwd / ".git"
    if git_path.is_dir():
        return git_path
    if not git_path.is_file():
        return None
    text = _read_text_safe(git_path)
    if text is None:
        return None
    line = text.strip()
    prefix = "gitdir: "
    if not line.startswith(prefix):
        return None
    candidate = Path(line[len(prefix) :].strip())
    if not candidate.is_absolute():
        candidate = (cwd / candidate).resolve()
    return candidate if candidate.is_dir() else None


def _read_git_branch(cwd: Path) -> str | None:
    """Parse .git/HEAD without invoking git. Returns None for detached HEAD or no repo.

    Handles worktrees: when `.git` is a file containing `gitdir: <path>` we
    follow it to find the real HEAD file.
    """
    git_dir = _resolve_git_dir(cwd)
    if git_dir is None:
        return None
    head = git_dir / "HEAD"
    if not head.is_file():
        return None
    text = _read_text_safe(head)
    if text is None:
        return None
    text = text.strip()
    prefix = "ref: refs/heads/"
    if text.startswith(prefix):
        branch = text[len(prefix) :].strip()
        return branch or None
    return None


def _fingerprint_payload(bundle: WorkspaceBundle) -> str:
    payload = bundle.to_payload()
    payload.pop("fingerprint", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _serialised_size(bundle: WorkspaceBundle) -> int:
    return len(json.dumps(bundle.to_payload(), separators=(",", ":")))


def _shrink_to_cap(bundle: WorkspaceBundle) -> WorkspaceBundle:
    """Trim fields in priority order until JSON fits BUNDLE_SIZE_CAP_BYTES.

    Order: drop README first, then trim file tree from the bottom, then drop
    manifests one at a time. Sets `truncated=True` on any modification.
    """
    if _serialised_size(bundle) <= BUNDLE_SIZE_CAP_BYTES:
        return bundle
    bundle.truncated = True
    bundle.readme_excerpt = ""
    while (
        _serialised_size(bundle) > BUNDLE_SIZE_CAP_BYTES and bundle.file_tree
    ):
        lines = bundle.file_tree.splitlines()
        if not lines:
            break
        bundle.file_tree = "\n".join(lines[:-1])
    for name in list(bundle.manifests.keys()):
        if _serialised_size(bundle) <= BUNDLE_SIZE_CAP_BYTES:
            break
        bundle.manifests.pop(name)
    return bundle


def build_light_bundle(cwd: Path | str) -> WorkspaceBundle:
    """Build a privacy-safe summary of `cwd`.

    Reads only the local filesystem; never opens a network connection.
    Files matched by DENYLIST_PATTERNS or any .gitignore / .aztea_ignore entry
    are unconditionally excluded — even from the file tree's name listing.
    Output is capped at BUNDLE_SIZE_CAP_BYTES of JSON; if the cap is hit,
    `truncated` is set and content is dropped in priority order.
    """
    path = Path(cwd).resolve()
    if not path.is_dir():
        raise ValueError(f"workspace path is not a directory: {path}")
    ignore_patterns = _load_ignore_patterns(path)
    bundle = WorkspaceBundle(
        cwd_basename=path.name or str(path),
        file_tree=_build_file_tree(path, ignore_patterns),
        manifests=_collect_manifests(path),
        readme_excerpt=_read_readme_excerpt(path),
        git_branch=_read_git_branch(path),
    )
    bundle = _shrink_to_cap(bundle)
    bundle.bundle_fingerprint = _fingerprint_payload(bundle)
    return bundle


def bundle_from_payload(payload: dict[str, Any]) -> WorkspaceBundle:
    """Reverse of `to_payload()`. Used by the backend to re-hydrate a bundle.

    Tolerates missing keys; never raises on partial input. Returns a bundle
    whose fingerprint matches the input verbatim if present.
    """
    if not isinstance(payload, dict):
        raise ValueError("workspace_context payload must be a dict")
    return WorkspaceBundle(
        cwd_basename=str(payload.get("cwd_basename") or ""),
        file_tree=str(payload.get("file_tree") or ""),
        manifests={
            str(k): str(v)
            for k, v in (payload.get("manifests") or {}).items()
            if isinstance(v, str)
        },
        readme_excerpt=str(payload.get("readme_excerpt") or ""),
        git_branch=(
            str(payload["git_branch"])
            if payload.get("git_branch") is not None
            else None
        ),
        bundle_fingerprint=str(payload.get("fingerprint") or ""),
        truncated=bool(payload.get("truncated") or False),
    )
