"""
skill_parser.py — Parse OpenClaw SKILL.md files into Aztea agent registration payloads.

SKILL.md format: YAML frontmatter (between --- delimiters) + Markdown body.
Required frontmatter fields: name, description.
Optional: homepage, metadata (openclaw block), allowed-tools, user-invocable.

Edge cases discovered from real ClawHub skills:

- canvas has NO frontmatter at all; parser infers name from the first H1 heading
  and description from the first non-empty paragraph that follows it.

- YAML flow-style blocks (the metadata field) may contain trailing commas, e.g.
      metadata: { "openclaw": { "emoji": "📝", ... }, }
  Standard PyYAML rejects trailing commas in flow mappings. We strip them before
  the first parse attempt, which is safe since we never re-serialise the raw YAML.

- requires.bins uses AND logic (all must be present); requires.anyBins uses OR logic
  (at least one must be present). Easy to swap — both are normalised on ParsedSkill.

- {baseDir} in a body references the skill's on-disk bundle directory. A hosted skill
  running on Aztea's infrastructure has no bundle on disk; we emit a warning so the
  builder knows these references will be inert.

- Body length can exceed 800 lines (gh-issues). The OpenClaw spec recommends ≤500;
  we warn but do not reject.

- allowed-tools and user-invocable use hyphenated keys in YAML — yaml.safe_load
  returns them with hyphens, not underscores.

- description may be quoted or unquoted in YAML; both are valid and parsed identically.

- The metadata block can appear on one line or across many lines; PyYAML normalises both.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SkillParseError(ValueError):
    """Raised when a SKILL.md cannot be parsed into a usable agent spec."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class OpenClawRequires:
    bins: list[str] = field(default_factory=list)       # AND — all must be present
    any_bins: list[str] = field(default_factory=list)   # OR  — at least one required
    env: list[str] = field(default_factory=list)        # required environment variables
    config: list[str] = field(default_factory=list)     # required OpenClaw config keys


@dataclass
class InstallEntry:
    id: str
    kind: str       # brew | apt | node | go | uv | download
    label: str
    bins: list[str] = field(default_factory=list)
    formula: str | None = None          # brew
    tap: str | None = None              # brew
    package: str | None = None          # node / uv
    module: str | None = None           # go
    url: str | None = None              # download
    archive: str | None = None          # download
    extract: bool | None = None         # download
    strip_components: int | None = None # download
    target_dir: str | None = None       # download
    os: list[str] | None = None         # platform restriction on this entry


@dataclass
class ParsedSkill:
    name: str
    description: str
    body: str                                # Markdown body — used as the LLM system prompt
    emoji: str | None = None
    homepage: str | None = None
    os_constraints: list[str] = field(default_factory=list)
    requires: OpenClawRequires = field(default_factory=OpenClawRequires)
    install: list[InstallEntry] = field(default_factory=list)
    primary_env: str | None = None
    skill_key: str | None = None
    user_invocable: bool = False
    allowed_tools: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_aztea_registration(self) -> dict[str, Any]:
        """Return a partial Aztea POST /registry/register payload.

        Omits endpoint_url (assigned by the hosted skill runner) and
        price_per_call_usd (set by the builder during the upload wizard).
        """
        payload: dict[str, Any] = {
            "name": _slug_to_display_name(self.name),
            "description": self.description,
            "tags": self._derive_tags(),
            "input_schema": _NATURAL_LANGUAGE_INPUT_SCHEMA,
            "output_schema": _TEXT_OUTPUT_SCHEMA,
        }
        if self.homepage:
            payload["homepage"] = self.homepage
        return payload

    def _derive_tags(self) -> list[str]:
        tags: set[str] = set()

        # Service name from env vars: NOTION_API_KEY → notion, GH_TOKEN → gh
        for env_var in self.requires.env:
            service = (
                env_var.lower()
                .replace("_api_key", "")
                .replace("_token", "")
                .replace("_key", "")
                .strip("_")
            )
            if len(service) > 1:
                tags.add(service)

        # Tool names from required binaries
        for bin_name in self.requires.bins + self.requires.any_bins:
            tags.add(bin_name)

        # Domain from homepage: https://developers.notion.com → notion
        if self.homepage:
            host = urllib.parse.urlparse(self.homepage).hostname or ""
            domain = host.removeprefix("www.").split(".")[0]
            if len(domain) > 1:
                tags.add(domain)

        # Skill slug itself is always a tag
        tags.add(self.name)

        # Stable order; cap at 10 (Aztea registration limit)
        return sorted(tags)[:10]


# ---------------------------------------------------------------------------
# Default schemas for hosted skills
# ---------------------------------------------------------------------------

_NATURAL_LANGUAGE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "The task or question for the skill to handle.",
        }
    },
    "required": ["task"],
}

_TEXT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "result": {
            "type": "string",
            "description": "The skill's response.",
        }
    },
    "required": ["result"],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_skill_md(content: str, *, source: str = "<unknown>") -> ParsedSkill:
    """Parse a SKILL.md string and return a ParsedSkill.

    Args:
        content: Raw SKILL.md text.
        source:  Filename or URL for error messages.

    Raises:
        SkillParseError: If the content cannot be parsed into a valid skill.
    """
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    frontmatter_text, body = _split_frontmatter(content)

    if frontmatter_text is None:
        # canvas-style: no frontmatter at all
        return _parse_no_frontmatter(body, source=source)

    fm = _parse_yaml(frontmatter_text, source=source)
    name = _require_string(fm, "name", source)
    description = _require_string(fm, "description", source)

    warnings: list[str] = []
    openclaw = _extract_openclaw_block(fm)
    requires, install = _parse_openclaw_metadata(openclaw, source=source)

    skill = ParsedSkill(
        name=name,
        description=description,
        body=body.strip(),
        emoji=openclaw.get("emoji"),
        homepage=_optional_string(fm, "homepage"),
        os_constraints=_coerce_list(openclaw.get("os")),
        requires=requires,
        install=install,
        primary_env=openclaw.get("primaryEnv"),
        skill_key=openclaw.get("skillKey"),
        user_invocable=bool(fm.get("user-invocable", False)),
        allowed_tools=_coerce_list(fm.get("allowed-tools")),
        warnings=warnings,
    )

    _collect_body_warnings(skill)
    return skill


# ---------------------------------------------------------------------------
# Frontmatter splitting
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"^---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)",
    re.DOTALL,
)


def _split_frontmatter(content: str) -> tuple[str | None, str]:
    """Return (frontmatter_text, body). frontmatter_text is None if absent."""
    if not content.startswith("---"):
        return None, content
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return None, content
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------
# YAML parsing with trailing-comma tolerance
# ---------------------------------------------------------------------------

_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _fix_trailing_commas(text: str) -> str:
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def _parse_yaml(text: str, *, source: str) -> dict[str, Any]:
    try:
        result = yaml.safe_load(_fix_trailing_commas(text))
    except yaml.YAMLError as exc:
        raise SkillParseError(
            f"{source}: YAML frontmatter could not be parsed: {exc}"
        ) from exc
    if not isinstance(result, dict):
        raise SkillParseError(f"{source}: YAML frontmatter must be a mapping, got {type(result).__name__}")
    return result


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def _extract_openclaw_block(fm: dict[str, Any]) -> dict[str, Any]:
    metadata = fm.get("metadata") or {}
    if not isinstance(metadata, dict):
        return {}
    return metadata.get("openclaw") or {}


def _parse_openclaw_metadata(
    block: dict[str, Any],
    *,
    source: str,
) -> tuple[OpenClawRequires, list[InstallEntry]]:
    requires_raw = block.get("requires") or {}
    requires = OpenClawRequires(
        bins=_coerce_list(requires_raw.get("bins")),
        any_bins=_coerce_list(requires_raw.get("anyBins")),
        env=_coerce_list(requires_raw.get("env")),
        config=_coerce_list(requires_raw.get("config")),
    )
    install = [_parse_install_entry(e, source=source) for e in _coerce_list(block.get("install"))]
    return requires, install


def _parse_install_entry(raw: Any, *, source: str) -> InstallEntry:
    if not isinstance(raw, dict):
        raise SkillParseError(f"{source}: install entry must be a mapping, got {type(raw).__name__}")
    try:
        entry = InstallEntry(
            id=str(raw["id"]),
            kind=str(raw["kind"]),
            label=str(raw["label"]),
            bins=_coerce_list(raw.get("bins")),
            formula=raw.get("formula"),
            tap=raw.get("tap"),
            package=raw.get("package"),
            module=raw.get("module"),
            url=raw.get("url"),
            archive=raw.get("archive"),
            extract=raw.get("extract"),
            strip_components=raw.get("stripComponents"),
            target_dir=raw.get("targetDir"),
            os=_coerce_list(raw.get("os")) or None,
        )
    except KeyError as exc:
        raise SkillParseError(f"{source}: install entry missing required field {exc}") from exc
    return entry


# ---------------------------------------------------------------------------
# No-frontmatter fallback (canvas-style)
# ---------------------------------------------------------------------------

_H1_RE = re.compile(r"^#\s+(.+)", re.MULTILINE)
_PARAGRAPH_RE = re.compile(r"(?m)^(?!#)(.{20,})")


def _parse_no_frontmatter(body: str, *, source: str) -> ParsedSkill:
    """Infer name and description from Markdown when frontmatter is absent.

    A canvas-style SKILL.md without a YAML frontmatter block must still describe
    a real skill: it needs an H1 (the name), a non-trivial description paragraph,
    *and* a Description / Input / Steps / Output section structure that the
    runner can follow. Earlier versions accepted any markdown containing a single
    "# bad" heading as valid; that allowed garbage skills to register and silently
    fail at execution time. The checks below enforce a minimum useful shape.
    """
    warnings = [
        "No YAML frontmatter found. Name and description were inferred from the Markdown body. "
        "Add a frontmatter block for reliable parsing."
    ]

    h1 = _H1_RE.search(body)
    name = h1.group(1).strip() if h1 else ""
    if not name:
        raise SkillParseError(f"{source}: No frontmatter and no H1 heading — cannot infer skill name.")

    # First substantive paragraph after the H1
    search_from = h1.end() if h1 else 0
    para = _PARAGRAPH_RE.search(body, search_from)
    description = para.group(1).strip() if para else ""

    if not description or len(description) < 10:
        raise SkillParseError(
            f"{source}: SKILL.md needs a description paragraph (at least 10 chars) "
            f"under the title, OR a YAML frontmatter block with a 'description' field."
        )
    if description == name:
        raise SkillParseError(
            f"{source}: description must be a real sentence, not just the skill title."
        )

    # Require at least one ``##`` (level-2) section heading after the H1.
    # Without any structural section the body is just an unstructured note,
    # not an executable skill. We deliberately don't enforce a specific name
    # (canvas/SKILL.md uses ``## Overview``, others use ``## Steps``,
    # ``## Description``, etc.) — any subsection demonstrates the author has
    # given the runner some structure to follow.
    has_section = bool(re.search(r"^##\s+\S", body, re.MULTILINE))
    if not has_section:
        raise SkillParseError(
            f"{source}: SKILL.md must contain at least one '## Section' heading "
            "after the H1 (e.g. ## Description, ## Steps, ## Overview, ## Input)."
        )

    skill = ParsedSkill(
        name=_display_name_to_slug(name),
        description=description,
        body=body.strip(),
        warnings=warnings,
    )
    _collect_body_warnings(skill)
    return skill


# ---------------------------------------------------------------------------
# Body warnings
# ---------------------------------------------------------------------------

_BASEDIR_RE = re.compile(r"\{baseDir\}")


def _collect_body_warnings(skill: ParsedSkill) -> None:
    if _BASEDIR_RE.search(skill.body):
        skill.warnings.append(
            "{baseDir} references appear in the skill body. These resolve to the skill's "
            "on-disk bundle directory in OpenClaw but have no equivalent on the Aztea hosted "
            "runner. Any bundled scripts will not be available."
        )
    lines = skill.body.count("\n") + 1
    if lines > 500:
        skill.warnings.append(
            f"Skill body is {lines} lines (OpenClaw recommends ≤500). Consider splitting "
            "large reference sections into separate files."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_string(fm: dict[str, Any], key: str, source: str) -> str:
    val = fm.get(key)
    if not val:
        raise SkillParseError(f"{source}: frontmatter missing required field '{key}'")
    if not isinstance(val, str):
        raise SkillParseError(f"{source}: frontmatter field '{key}' must be a string, got {type(val).__name__}")
    return val.strip()


def _optional_string(fm: dict[str, Any], key: str) -> str | None:
    val = fm.get(key)
    return str(val).strip() if val else None


def _coerce_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        return [val]
    return list(val)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _display_name_to_slug(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-")


def _slug_to_display_name(slug: str) -> str:
    if " " in slug or any(c.isupper() for c in slug):
        return slug
    return " ".join(word.capitalize() for word in slug.split("-"))
