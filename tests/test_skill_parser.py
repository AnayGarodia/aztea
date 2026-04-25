"""
Tests for core/skill_parser.py using real OpenClaw SKILL.md content.

Real files fetched from github.com/openclaw/openclaw on 2026-04-25.
Each fixture is lightly trimmed (body truncated after ~30 lines) but the
frontmatter is verbatim — that's where all the interesting edge cases live.
"""

from __future__ import annotations

import pytest

from core.skill_parser import (
    InstallEntry,
    OpenClawRequires,
    ParsedSkill,
    SkillParseError,
    parse_skill_md,
)


# ---------------------------------------------------------------------------
# Fixtures — real SKILL.md content
# ---------------------------------------------------------------------------

# 1. Minimal: name + description only, no metadata block.
SKILL_CREATOR = """\
---
name: skill-creator
description: Create, edit, improve, tidy, review, audit, or restructure AgentSkills and SKILL.md files.
---

# Skill Creator

This skill provides guidance for creating effective skills.

Every SKILL.md consists of:

- **Frontmatter** (YAML): Contains `name` and `description` fields.
- **Body** (Markdown): Instructions and guidance for using the skill.
"""

# 2. Multi-line metadata with install block.  Verbatim frontmatter.
SKILL_GITHUB = """\
---
name: github
description: "Use gh for GitHub issues, PR status, CI/logs, comments, reviews, releases, and API queries."
metadata:
  {
    "openclaw":
      {
        "emoji": "🐙",
        "requires": { "bins": ["gh"] },
        "install":
          [
            {
              "id": "brew",
              "kind": "brew",
              "formula": "gh",
              "bins": ["gh"],
              "label": "Install GitHub CLI (brew)"
            },
            {
              "id": "apt",
              "kind": "apt",
              "formula": "gh",
              "bins": ["gh"],
              "label": "Install GitHub CLI (apt)",
              "os": ["linux"]
            }
          ]
      }
  }
---

# GitHub (gh CLI)

Use `gh` for all GitHub operations.

## Issues

```
gh issue list --state open --limit 30
gh issue view 123
```
"""

# 3. Env-var requires + trailing comma in flow block.  Verbatim frontmatter.
SKILL_NOTION = """\
---
name: notion
description: Notion API for creating and managing pages, databases, and blocks.
homepage: https://developers.notion.com
metadata:
  {
    "openclaw":
      { "emoji": "📝", "requires": { "env": ["NOTION_API_KEY"] }, "primaryEnv": "NOTION_API_KEY" },
  }
---

# notion

Use the Notion API to create/read/update pages, data sources (databases), and blocks.
"""

# 4. Single-line inline metadata.  Verbatim frontmatter.
SKILL_SLACK = """\
---
name: slack
description: Use the Slack tool to react, pin/unpin, send, edit, delete messages, or fetch Slack member info.
metadata: { "openclaw": { "emoji": "💬", "requires": { "config": ["channels.slack"] } } }
---

# Slack Actions

Use `slack` to react, manage pins, send/edit/delete messages, and look up users.
"""

# 5. No frontmatter at all.  Verbatim from canvas/SKILL.md.
SKILL_CANVAS = """\
# Canvas Skill

Display HTML content on connected OpenClaw nodes (Mac app, iOS, Android).

## Overview

The canvas tool lets you present web content on any connected node's canvas view. Great for:

- Displaying games, visualizations, dashboards
- Showing generated HTML content
- Interactive demos
"""

# 6. user-invocable + multi-bin requires + install entries.  Verbatim frontmatter.
SKILL_GH_ISSUES = """\
---
name: gh-issues
description: "Fetch GitHub issues, delegate fixes to subagents, open PRs, watch reviews, or run /gh-issues workflows."
user-invocable: true
metadata:
  {
    "openclaw":
      {
        "requires": { "bins": ["curl", "git", "gh"] },
        "primaryEnv": "GH_TOKEN",
        "install":
          [
            {
              "id": "brew",
              "kind": "brew",
              "formula": "gh",
              "bins": ["gh"],
              "label": "Install GitHub CLI (brew)"
            }
          ]
      }
  }
---

# GitHub Issues Workflow

Fetch issues, delegate fixes to sub-agents, and open pull requests.
"""

# 7. anyBins (OR logic).  Verbatim frontmatter.
SKILL_SPOTIFY = """\
---
name: spotify-player
description: Terminal Spotify playback/search via spogo (preferred) or spotify_player.
homepage: https://www.spotify.com
metadata:
  {
    "openclaw":
      {
        "emoji": "🎵",
        "requires": { "anyBins": ["spogo", "spotify_player"] },
        "install":
          [
            {
              "id": "brew-spogo",
              "kind": "brew",
              "formula": "spogo",
              "bins": ["spogo"],
              "label": "Install spogo (brew)"
            }
          ]
      }
  }
---

# Spotify Player

Control Spotify from the terminal.
"""

# 8. allowed-tools field + config requires.  Verbatim frontmatter.
SKILL_DISCORD = """\
---
name: discord
description: "Discord ops via the message tool (channel=discord)."
metadata: { "openclaw": { "emoji": "🎮", "requires": { "config": ["channels.discord.token"] } } }
allowed-tools: ["message"]
---

# Discord (Via `message`)

Use the `message` tool with `channel=discord`.
"""

# 9. Emoji-only metadata (no requires).  Verbatim frontmatter.
SKILL_TASKFLOW = """\
---
name: taskflow
description: Coordinate multi-step detached tasks as one durable TaskFlow job with owner context, state, waits, and child tasks.
metadata: { "openclaw": { "emoji": "🪝" } }
---

# TaskFlow

Use TaskFlow when a job needs to outlive one prompt or one detached run.
"""

# 10. {baseDir} template variable in body.
SKILL_WITH_BASEDIR = """\
---
name: video-frames
description: Extract frames from video files using bundled ffmpeg scripts.
---

# Video Frame Extraction

Run the bundled script:

```bash
{baseDir}/scripts/extract_frames.sh input.mp4 --fps 1
```
"""

# 11. Body over 500 lines (synthesised — we generate it).
SKILL_LONG_BODY = (
    "---\nname: verbose-skill\ndescription: A skill with a very long body.\n---\n\n"
    + "# Verbose Skill\n\n"
    + "\n".join(f"Line {i} of documentation." for i in range(510))
)

# 12. os constraint on the skill itself.
SKILL_MACOS_ONLY = """\
---
name: screen-recorder
description: Record the screen on macOS using the built-in screencaptureui.
metadata:
  {
    "openclaw":
      {
        "emoji": "🎥",
        "os": ["darwin"],
        "requires": { "bins": ["screencapture"] }
      }
  }
---

# Screen Recorder

Uses `screencapture` to record the display.
"""

# 13. skillKey override.
SKILL_VOICE_CALL = """\
---
name: voice-call
description: Make and receive voice calls via the OpenClaw voice bridge.
metadata:
  {
    "openclaw":
      {
        "emoji": "☎️",
        "skillKey": "voice-call",
        "requires": { "config": ["plugins.entries.voice-call.enabled"] }
      }
  }
---

# Voice Call

Bridge phone calls through the OpenClaw voice module.
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMinimalSkill:
    def test_name_and_description_parsed(self):
        skill = parse_skill_md(SKILL_CREATOR)
        assert skill.name == "skill-creator"
        assert "AgentSkills" in skill.description

    def test_no_metadata_fields_set(self):
        skill = parse_skill_md(SKILL_CREATOR)
        assert skill.emoji is None
        assert skill.homepage is None
        assert skill.requires.bins == []
        assert skill.requires.env == []
        assert skill.install == []

    def test_body_contains_markdown(self):
        skill = parse_skill_md(SKILL_CREATOR)
        assert "Frontmatter" in skill.body

    def test_no_warnings(self):
        skill = parse_skill_md(SKILL_CREATOR)
        assert skill.warnings == []


class TestGithubSkill:
    def test_emoji_parsed(self):
        skill = parse_skill_md(SKILL_GITHUB)
        assert skill.emoji == "🐙"

    def test_required_bins(self):
        skill = parse_skill_md(SKILL_GITHUB)
        assert skill.requires.bins == ["gh"]

    def test_install_entries(self):
        skill = parse_skill_md(SKILL_GITHUB)
        assert len(skill.install) == 2
        brew = skill.install[0]
        assert isinstance(brew, InstallEntry)
        assert brew.id == "brew"
        assert brew.kind == "brew"
        assert brew.formula == "gh"
        assert brew.bins == ["gh"]
        assert brew.os is None

        apt = skill.install[1]
        assert apt.kind == "apt"
        assert apt.os == ["linux"]

    def test_tags_include_bin_and_slug(self):
        skill = parse_skill_md(SKILL_GITHUB)
        tags = skill.to_aztea_registration()["tags"]
        assert "gh" in tags
        assert "github" in tags


class TestNotionSkill:
    """Critical edge case: trailing comma in YAML flow-style block."""

    def test_parses_despite_trailing_comma(self):
        # Would raise yaml.YAMLError without the _fix_trailing_commas pre-pass.
        skill = parse_skill_md(SKILL_NOTION)
        assert skill.name == "notion"

    def test_env_requires(self):
        skill = parse_skill_md(SKILL_NOTION)
        assert "NOTION_API_KEY" in skill.requires.env

    def test_primary_env(self):
        skill = parse_skill_md(SKILL_NOTION)
        assert skill.primary_env == "NOTION_API_KEY"

    def test_homepage(self):
        skill = parse_skill_md(SKILL_NOTION)
        assert skill.homepage == "https://developers.notion.com"

    def test_tags_derived_from_env_and_homepage(self):
        skill = parse_skill_md(SKILL_NOTION)
        tags = skill.to_aztea_registration()["tags"]
        assert "notion" in tags  # from env var name AND homepage domain

    def test_emoji(self):
        skill = parse_skill_md(SKILL_NOTION)
        assert skill.emoji == "📝"


class TestSlackSkill:
    """Single-line inline metadata."""

    def test_parses_inline_metadata(self):
        skill = parse_skill_md(SKILL_SLACK)
        assert skill.emoji == "💬"

    def test_config_requires(self):
        skill = parse_skill_md(SKILL_SLACK)
        assert "channels.slack" in skill.requires.config

    def test_no_bins_or_env(self):
        skill = parse_skill_md(SKILL_SLACK)
        assert skill.requires.bins == []
        assert skill.requires.env == []


class TestCanvasSkill:
    """No frontmatter at all — the only real skill with this property."""

    def test_parses_without_frontmatter(self):
        skill = parse_skill_md(SKILL_CANVAS)
        assert skill.name  # some value inferred
        assert skill.description

    def test_name_inferred_from_h1(self):
        skill = parse_skill_md(SKILL_CANVAS)
        # H1 is "Canvas Skill" → slug "canvas-skill"
        assert skill.name == "canvas-skill"

    def test_description_inferred_from_first_paragraph(self):
        skill = parse_skill_md(SKILL_CANVAS)
        assert "HTML" in skill.description or "OpenClaw" in skill.description

    def test_warning_emitted(self):
        skill = parse_skill_md(SKILL_CANVAS)
        assert any("No YAML frontmatter" in w for w in skill.warnings)


class TestGhIssuesSkill:
    def test_user_invocable(self):
        skill = parse_skill_md(SKILL_GH_ISSUES)
        assert skill.user_invocable is True

    def test_multiple_required_bins(self):
        skill = parse_skill_md(SKILL_GH_ISSUES)
        assert set(skill.requires.bins) == {"curl", "git", "gh"}

    def test_primary_env(self):
        skill = parse_skill_md(SKILL_GH_ISSUES)
        assert skill.primary_env == "GH_TOKEN"

    def test_source_name_in_error_context(self):
        bad = SKILL_GH_ISSUES.replace("name: gh-issues", "name: ")
        with pytest.raises(SkillParseError, match="my_file.md"):
            parse_skill_md(bad, source="my_file.md")


class TestSpotifySkill:
    """anyBins uses OR logic — at least one must be present."""

    def test_any_bins_parsed(self):
        skill = parse_skill_md(SKILL_SPOTIFY)
        assert set(skill.requires.any_bins) == {"spogo", "spotify_player"}

    def test_bins_list_is_empty(self):
        skill = parse_skill_md(SKILL_SPOTIFY)
        assert skill.requires.bins == []  # not AND-required

    def test_homepage_contributes_to_tags(self):
        skill = parse_skill_md(SKILL_SPOTIFY)
        tags = skill.to_aztea_registration()["tags"]
        assert "spotify" in tags

    def test_any_bins_contribute_to_tags(self):
        skill = parse_skill_md(SKILL_SPOTIFY)
        tags = skill.to_aztea_registration()["tags"]
        assert "spogo" in tags or "spotify_player" in tags


class TestDiscordSkill:
    """allowed-tools field restricts which OpenClaw tools may be invoked."""

    def test_allowed_tools_parsed(self):
        skill = parse_skill_md(SKILL_DISCORD)
        assert skill.allowed_tools == ["message"]

    def test_config_requires(self):
        skill = parse_skill_md(SKILL_DISCORD)
        assert "channels.discord.token" in skill.requires.config

    def test_emoji(self):
        skill = parse_skill_md(SKILL_DISCORD)
        assert skill.emoji == "🎮"


class TestTaskflowSkill:
    """Emoji-only metadata with no requires or install."""

    def test_emoji_only_metadata(self):
        skill = parse_skill_md(SKILL_TASKFLOW)
        assert skill.emoji == "🪝"
        assert skill.requires.bins == []
        assert skill.requires.env == []
        assert skill.install == []

    def test_no_warnings(self):
        skill = parse_skill_md(SKILL_TASKFLOW)
        assert skill.warnings == []


class TestBaseDirWarning:
    def test_warns_on_basedir_reference(self):
        skill = parse_skill_md(SKILL_WITH_BASEDIR)
        assert any("{baseDir}" in w for w in skill.warnings)

    def test_body_preserved_verbatim(self):
        skill = parse_skill_md(SKILL_WITH_BASEDIR)
        assert "{baseDir}/scripts/extract_frames.sh" in skill.body


class TestLargeBodyWarning:
    def test_warns_when_body_exceeds_500_lines(self):
        skill = parse_skill_md(SKILL_LONG_BODY)
        assert any("500" in w for w in skill.warnings)

    def test_body_fully_preserved(self):
        skill = parse_skill_md(SKILL_LONG_BODY)
        assert "Line 509" in skill.body


class TestOsConstraint:
    def test_os_constraint_parsed(self):
        skill = parse_skill_md(SKILL_MACOS_ONLY)
        assert skill.os_constraints == ["darwin"]

    def test_os_constraint_not_in_tags(self):
        skill = parse_skill_md(SKILL_MACOS_ONLY)
        tags = skill.to_aztea_registration()["tags"]
        assert "darwin" not in tags


class TestSkillKeyOverride:
    def test_skill_key_parsed(self):
        skill = parse_skill_md(SKILL_VOICE_CALL)
        assert skill.skill_key == "voice-call"


class TestToAzteaRegistration:
    def test_name_is_display_name(self):
        skill = parse_skill_md(SKILL_GITHUB)
        payload = skill.to_aztea_registration()
        # "github" → "Github"
        assert payload["name"] == "Github"

    def test_hyphenated_slug_becomes_title_case(self):
        skill = parse_skill_md(SKILL_GH_ISSUES)
        payload = skill.to_aztea_registration()
        assert payload["name"] == "Gh Issues"

    def test_description_preserved(self):
        skill = parse_skill_md(SKILL_NOTION)
        payload = skill.to_aztea_registration()
        assert "Notion API" in payload["description"]

    def test_tags_capped_at_10(self):
        # Synthesise a skill with many env vars
        many_env = "---\nname: big-skill\ndescription: Many env vars.\n"
        many_env += 'metadata: { "openclaw": { "requires": { "env": ['
        many_env += ", ".join(f'"SERVICE_{i}_API_KEY"' for i in range(20))
        many_env += '] } } }\n---\nBody.\n'
        skill = parse_skill_md(many_env)
        tags = skill.to_aztea_registration()["tags"]
        assert len(tags) <= 10

    def test_input_schema_present(self):
        skill = parse_skill_md(SKILL_CREATOR)
        payload = skill.to_aztea_registration()
        assert payload["input_schema"]["type"] == "object"
        assert "task" in payload["input_schema"]["properties"]

    def test_output_schema_present(self):
        skill = parse_skill_md(SKILL_CREATOR)
        payload = skill.to_aztea_registration()
        assert "result" in payload["output_schema"]["properties"]

    def test_homepage_included_when_present(self):
        skill = parse_skill_md(SKILL_NOTION)
        payload = skill.to_aztea_registration()
        assert payload.get("homepage") == "https://developers.notion.com"

    def test_homepage_absent_when_not_set(self):
        skill = parse_skill_md(SKILL_CREATOR)
        payload = skill.to_aztea_registration()
        assert "homepage" not in payload

    def test_endpoint_url_not_in_payload(self):
        # endpoint_url is set by the hosted runner, not SKILL.md
        skill = parse_skill_md(SKILL_GITHUB)
        payload = skill.to_aztea_registration()
        assert "endpoint_url" not in payload

    def test_price_not_in_payload(self):
        # price is set by the builder in the upload wizard
        skill = parse_skill_md(SKILL_GITHUB)
        payload = skill.to_aztea_registration()
        assert "price_per_call_usd" not in payload


class TestErrorCases:
    def test_missing_name_raises(self):
        bad = "---\ndescription: Something.\n---\nBody.\n"
        with pytest.raises(SkillParseError, match="name"):
            parse_skill_md(bad)

    def test_missing_description_raises(self):
        bad = "---\nname: my-skill\n---\nBody.\n"
        with pytest.raises(SkillParseError, match="description"):
            parse_skill_md(bad)

    def test_no_frontmatter_and_no_h1_raises(self):
        bad = "Just some text with no heading.\n"
        with pytest.raises(SkillParseError, match="H1"):
            parse_skill_md(bad)

    def test_invalid_yaml_raises(self):
        bad = "---\nname: [unclosed bracket\ndescription: hi\n---\nBody.\n"
        with pytest.raises(SkillParseError):
            parse_skill_md(bad)

    def test_malformed_install_entry_raises(self):
        bad = (
            "---\nname: bad\ndescription: test.\n"
            'metadata: { "openclaw": { "install": [{"kind": "brew"}] } }\n'
            "---\nBody.\n"
        )
        with pytest.raises(SkillParseError, match="install"):
            parse_skill_md(bad)

    def test_source_name_in_error_message(self):
        bad = "---\ndescription: No name here.\n---\nBody.\n"
        with pytest.raises(SkillParseError, match="skills/my-skill/SKILL.md"):
            parse_skill_md(bad, source="skills/my-skill/SKILL.md")


class TestWindowsLineEndings:
    def test_crlf_normalised(self):
        crlf = SKILL_CREATOR.replace("\n", "\r\n")
        skill = parse_skill_md(crlf)
        assert skill.name == "skill-creator"


class TestTagDerivation:
    def test_env_var_name_stripped_correctly(self):
        md = "---\nname: mysk\ndescription: test.\n"
        md += 'metadata: { "openclaw": { "requires": { "env": ["ELEVENLABS_API_KEY"] } } }\n'
        md += "---\nBody.\n"
        skill = parse_skill_md(md)
        tags = skill.to_aztea_registration()["tags"]
        assert "elevenlabs" in tags
        assert "ELEVENLABS_API_KEY" not in tags

    def test_token_suffix_stripped(self):
        md = "---\nname: mysk\ndescription: test.\n"
        md += 'metadata: { "openclaw": { "requires": { "env": ["GH_TOKEN"] } } }\n'
        md += "---\nBody.\n"
        skill = parse_skill_md(md)
        tags = skill.to_aztea_registration()["tags"]
        assert "gh" in tags

    def test_skill_slug_always_in_tags(self):
        skill = parse_skill_md(SKILL_CREATOR)
        tags = skill.to_aztea_registration()["tags"]
        assert "skill-creator" in tags

    def test_tags_are_sorted(self):
        skill = parse_skill_md(SKILL_NOTION)
        tags = skill.to_aztea_registration()["tags"]
        assert tags == sorted(tags)
