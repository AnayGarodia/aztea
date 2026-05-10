"""File-kind detection for `aztea publish`.

Given a path on disk, decide whether the author is publishing:
  - a hosted SKILL.md (zero-server, LLM-backed),
  - an agent.md manifest (author-hosted external endpoint), or
  - a Python handler (author wires up AzteaServer themselves).

Detection is intentionally cheap and offline. We never read more of a Python
file than we need to fingerprint it, and we never execute any of it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ListingKind = Literal["skill_md", "agent_md", "python_handler"]


@dataclass(frozen=True)
class DetectionResult:
    kind: ListingKind
    path: Path
    raw: str
    reason: str  # one-line explanation for the CLI receipt


# Match either a SKILL.md frontmatter block or a body that starts with the
# canonical OpenClaw markers. Cheap signal: top-of-file YAML front-matter or
# an explicit `# skill:` heading.
_SKILL_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\bname\s*:", re.DOTALL)
_SKILL_TITLE_RE = re.compile(r"\A\s*#\s*skill[:\s]", re.IGNORECASE)

# agent.md headings — borrowed from core/onboarding._REQUIRED_SECTIONS without
# importing it (we do not want to drag the server into the SDK CLI's deps).
_AGENT_MD_HEADINGS = (
    "registry endpoint",
    "registration flow",
    "settlement flow expectations",
    "registration metadata",
)


class DetectionError(ValueError):
    """Raised when the path is missing, unreadable, or doesn't match any kind."""


def detect(path: Path) -> DetectionResult:
    """Inspect `path` and classify it.

    Raises DetectionError with a human-readable message on failure. Does not
    read more than 256 KiB; larger files are rejected (matches the existing
    /skills upload cap).
    """
    expanded = Path(path).expanduser().resolve()
    if not expanded.exists():
        raise DetectionError(f"No such file: {expanded}")
    if not expanded.is_file():
        raise DetectionError(f"Not a file: {expanded}")
    size = expanded.stat().st_size
    if size > 256 * 1024:
        raise DetectionError(
            f"File is {size // 1024} KiB; the publish flow caps at 256 KiB. "
            "Trim the file or split your skill before retrying."
        )

    suffix = expanded.suffix.lower()
    text = expanded.read_text(encoding="utf-8", errors="replace")

    # Highest-confidence signal first: file extension + content shape.
    if suffix == ".py":
        return DetectionResult(
            kind="python_handler",
            path=expanded,
            raw=text,
            reason="Python file (.py); will register an external-endpoint listing.",
        )

    if suffix == ".md" or suffix == ".markdown":
        # Explicit kind: declaration wins over heuristics. Lets users force
        # the agent.md path even when their file uses ## headings that the
        # hosted-skill parser would otherwise grab. Without this the
        # SSRF-aware /onboarding/ingest path was bypassed because /skills
        # accepts any `## section` form (eval finding 2026-05-09).
        explicit_kind = _explicit_kind_from_frontmatter(text)
        if explicit_kind == "agent":
            return DetectionResult(
                kind="agent_md",
                path=expanded,
                raw=text,
                reason="explicit `kind: agent` declared in frontmatter.",
            )
        if explicit_kind == "skill":
            return DetectionResult(
                kind="skill_md",
                path=expanded,
                raw=text,
                reason="explicit `kind: skill` declared in frontmatter.",
            )

        looks_like_agent_md = _matches_agent_md(text)
        if looks_like_agent_md:
            return DetectionResult(
                kind="agent_md",
                path=expanded,
                raw=text,
                reason="agent.md manifest (≥ 3 of the canonical sections present).",
            )
        if _matches_skill_md(expanded.name, text):
            return DetectionResult(
                kind="skill_md",
                path=expanded,
                raw=text,
                reason="SKILL.md (frontmatter or `# skill:` heading detected).",
            )
        # Default for .md: treat as SKILL.md. The hosted-skills parser is the
        # most permissive and will surface a clean error if the body is
        # actually unusable.
        return DetectionResult(
            kind="skill_md",
            path=expanded,
            raw=text,
            reason=".md without explicit markers; treating as SKILL.md.",
        )

    raise DetectionError(
        f"Unsupported file type: {suffix or '(no extension)'}. "
        "Pass a .md (SKILL.md / agent.md) or .py (handler) file."
    )


def _matches_skill_md(filename: str, text: str) -> bool:
    if filename.lower().endswith(".skill.md"):
        return True
    head = text[:2048]
    if _SKILL_FRONTMATTER_RE.search(head):
        return True
    if _SKILL_TITLE_RE.match(head):
        return True
    return False


def _matches_agent_md(text: str) -> bool:
    head = text[:8192].lower()
    hits = sum(1 for heading in _AGENT_MD_HEADINGS if heading in head)
    return hits >= 3


_FRONTMATTER_KIND_RE = re.compile(
    r"^---\s*$(?P<body>.*?)^---\s*$",
    re.MULTILINE | re.DOTALL,
)
_FRONTMATTER_KIND_LINE_RE = re.compile(
    r"^kind\s*:\s*['\"]?(?P<kind>skill|agent)['\"]?\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _explicit_kind_from_frontmatter(text: str) -> str | None:
    """Return 'skill' or 'agent' when YAML frontmatter declares ``kind:``.

    Why: heading-shape heuristics confused agent.md files that use
    ``## endpoint`` style sections with hosted skills, silently bypassing
    the SSRF-aware ingest path. An explicit ``kind:`` lets callers be
    unambiguous.
    """
    m = _FRONTMATTER_KIND_RE.search(text[:4096])
    if not m:
        return None
    body = m.group("body") or ""
    km = _FRONTMATTER_KIND_LINE_RE.search(body)
    if not km:
        return None
    return km.group("kind").lower()


__all__ = ["DetectionError", "DetectionResult", "ListingKind", "detect"]
