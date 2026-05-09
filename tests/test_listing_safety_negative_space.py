"""L12 — false-positive prevention (negative-space corpus).

The cheapest way to break trust at YC is for the scanner to refuse a real
listing on stage. These fixtures look *plausibly* like things a scanner might
overshoot on, but are entirely benign. Every test here MUST stay green; a
red one means the scanner has tightened past usefulness.
"""
from __future__ import annotations

import pytest

from core.listing_safety import (
    has_block,
    has_warn,
    scan_agent_md_endpoint,
    scan_clone_against,
    scan_python_handler,
    scan_skill_md,
)


# ---------------------------------------------------------------------------
# SKILL.md — phrases that mention sensitive terms benignly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "Summarises the previous user turn into bullet points. Output as JSON.",
            id="word_previous_alone",
        ),
        pytest.param(
            "Explains what a system prompt is and how to write a good one.",
            id="discusses_system_prompts",
        ),
        pytest.param(
            "Walks the user through writing instructions for an LLM. Includes a "
            "checklist of common pitfalls.",
            id="discusses_instructions_writing",
        ),
        pytest.param(
            "Generates a release-note paragraph from a list of merged PR titles.",
            id="release_notes",
        ),
        pytest.param(
            "Reviews CSS for accessibility issues. Returns WCAG findings as JSON.",
            id="css_a11y_review",
        ),
        pytest.param(
            "Translates English prose into clear technical specifications.",
            id="prose_to_spec",
        ),
        pytest.param(
            "Annotates a markdown document with section headings and TOC links.",
            id="markdown_annotate",
        ),
        pytest.param(
            "Helpful skill that explains how prompt injection works without "
            "actually performing one. Educational only.",
            id="explains_prompt_injection_term",
        ),
        pytest.param(
            "Provides a concise digest of the previous chat turn for use in "
            "follow-up generation.",
            id="previous_chat_turn",
        ),
        pytest.param(
            "Lints YAML configuration files. Highlights duplicate keys, bad "
            "indentation, and missing required fields.",
            id="yaml_lint",
        ),
        pytest.param(
            "Analyses log files and surfaces error patterns. Reads up to 5 MB "
            "per call. Output is structured JSON with a severity histogram.",
            id="log_analysis",
        ),
    ],
)
def test_legitimate_skill_bodies_publish_clean(body):
    full = f"---\nname: test\ndescription: {body[:60]}\n---\n# test\n{body}\n"
    findings = scan_skill_md(full)
    assert not has_block(findings), (
        f"unexpected block on legit content: "
        f"{[(f.code, f.message) for f in findings if f.level == 'block']}"
    )


def test_legitimate_skill_with_short_base64_example_does_not_warn():
    # 199 chars — just under the 200-char threshold.
    body = (
        "---\nname: x\ndescription: shows base64 example\n---\n"
        "# x\nExample input: " + "A" * 199 + "\n"
    )
    findings = scan_skill_md(body)
    # Specifically: no base64_blob warning.
    assert not any(f.code == "skill.base64_blob" for f in findings)


def test_legitimate_skill_referencing_user_wallet_concept_does_not_warn():
    # The phrase "your wallet" (without slash-prefixed path) should not trip
    # the internal-path warn.
    body = (
        "---\nname: wallet-coach\ndescription: explains crypto wallet concepts\n---\n"
        "# wallet-coach\nEducates users about hardware wallets and seed phrases.\n"
    )
    findings = scan_skill_md(body)
    assert not any(f.code == "skill.references_internal_path" for f in findings)


# ---------------------------------------------------------------------------
# Endpoint URLs — third-party hosts that look like aztea but aren't
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://aztea.dev/run",  # staging suffix, not blocked
        "https://my.host.example.com/run",
        "https://api.example.com/agents/x",
        "https://az-tea.ai/run",  # different host, hyphenated
        "https://my-aztea-mirror.example.com/x",  # contains 'aztea' but not the suffix
    ],
)
def test_legitimate_endpoint_urls_pass(url):
    assert scan_agent_md_endpoint(url) == [], f"unexpected block on {url}"


# ---------------------------------------------------------------------------
# Python handlers — common legitimate import patterns must not block
# ---------------------------------------------------------------------------


_LEGIT_HANDLERS = [
    pytest.param(
        '"""HTTP echo handler."""\n'
        "import json\n"
        "import requests\n"
        "def handler(payload):\n"
        "    r = requests.post(payload['url'], json=payload['body'])\n"
        "    return {'status': r.status_code, 'body': r.text}\n",
        id="requests_post",
    ),
    pytest.param(
        '"""Markdown formatter."""\n'
        "import re\n"
        "import textwrap\n"
        "def handler(payload):\n"
        "    return {'out': textwrap.fill(payload['text'], 80)}\n",
        id="stdlib_text",
    ),
    pytest.param(
        '"""Anthropic-call wrapper."""\n'
        "import os\n"
        "def handler(payload):\n"
        "    key = os.environ.get('ANTHROPIC_API_KEY')\n"
        "    return {'configured': bool(key)}\n",
        id="os_environ_get",
    ),
    pytest.param(
        '"""Pure-stdlib JSON validator."""\n'
        "import json\n"
        "def handler(payload):\n"
        "    try:\n"
        "        json.loads(payload['text'])\n"
        "        return {'valid': True}\n"
        "    except json.JSONDecodeError as exc:\n"
        "        return {'valid': False, 'error': str(exc)}\n",
        id="json_validator",
    ),
    pytest.param(
        '"""dataclasses + typing legit handler."""\n'
        "import dataclasses\n"
        "import typing\n"
        "def handler(payload):\n"
        "    return {'count': len(payload)}\n",
        id="dataclasses_typing",
    ),
]


@pytest.mark.parametrize("source", _LEGIT_HANDLERS)
def test_legitimate_python_handlers_pass(source):
    findings = scan_python_handler(source)
    assert not has_block(findings), (
        f"unexpected block: "
        f"{[(f.code, f.message) for f in findings if f.level == 'block']}"
    )


# ---------------------------------------------------------------------------
# Clone detection — distinct agents must NOT trip near-duplicate warnings
# ---------------------------------------------------------------------------


def test_distinct_agents_do_not_trigger_clone_warning():
    existing = [
        {"name": "code-review", "description": "Reviews Python code for style and bugs."},
        {"name": "cve-lookup", "description": "Fetches NVD entries for a CVE id."},
    ]
    findings = scan_clone_against(
        candidate_name="markdown-toc",
        candidate_description="Generates a table of contents for markdown.",
        existing=existing,
    )
    assert not has_warn(findings)


def test_obvious_clone_does_warn():
    # Sanity-check the inverse — confirm clone detection is alive at all.
    existing = [
        {
            "name": "code review agent",
            "description": "Reviews Python code for style and bugs.",
        }
    ]
    findings = scan_clone_against(
        candidate_name="code review agent v2",
        candidate_description="Reviews Python code for style and bugs.",
        existing=existing,
    )
    assert has_warn(findings)
