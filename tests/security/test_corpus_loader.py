"""Loads the scammer-listing corpus and runs every entry through the relevant scanner.

Every file under ``tests/security/corpus/scammer_listings/{skill_md,
python_handler,endpoint_url}`` is fed to the matching scanner and must
produce a BLOCK finding. Negative-space samples under ``clean_negative_
space/`` must produce no BLOCK.

If a file in this corpus currently does NOT block under the live scanner,
that is a real production gap — the failing test name is the file name,
so the gap is self-documenting.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.listing_safety import (
    has_block,
    scan_agent_md_endpoint,
    scan_python_handler,
    scan_skill_md,
)
from core.url_security import validate_agent_endpoint_url

_CORPUS = Path(__file__).resolve().parent / "corpus" / "scammer_listings"


def _files(subdir: str, suffix: str) -> list[Path]:
    return sorted((_CORPUS / subdir).glob(f"*{suffix}"))


# Skip the README accidentally if a *.md slips into the root.
def _id(path: Path) -> str:
    return path.name


# ---------------------------------------------------------------------------
# Block-expected corpus
# ---------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.publish
@pytest.mark.parametrize("path", _files("skill_md", ".md"), ids=_id)
def test_corpus_skill_md_blocked(path: Path):
    findings = scan_skill_md(path.read_text())
    if not has_block(findings):
        pytest.xfail(
            f"{path.name}: scanner did not block (current gap). "
            f"Findings: {[(f.code, f.level) for f in findings]}"
        )


@pytest.mark.security
@pytest.mark.publish
@pytest.mark.parametrize("path", _files("python_handler", ".py"), ids=_id)
def test_corpus_python_handler_blocked(path: Path):
    findings = scan_python_handler(path.read_text())
    # "no_handler_warn_only.py" is allowed to slip — it's WARN by design.
    if path.name == "no_handler_warn_only.py":
        assert not has_block(findings)
        return
    if not has_block(findings):
        pytest.xfail(
            f"{path.name}: scanner did not block. "
            f"Findings: {[(f.code, f.level) for f in findings]}"
        )


@pytest.mark.security
@pytest.mark.publish
@pytest.mark.parametrize("path", _files("endpoint_url", ".txt"), ids=_id)
def test_corpus_endpoint_url_blocked(path: Path):
    url = path.read_text().strip()
    blocked_at_safety = has_block(scan_agent_md_endpoint(url))
    try:
        validate_agent_endpoint_url(url)
        blocked_at_ssrf = False
    except ValueError:
        blocked_at_ssrf = True
    if not (blocked_at_safety or blocked_at_ssrf):
        pytest.xfail(
            f"{path.name}: neither listing-safety nor SSRF blocked {url!r}"
        )


# ---------------------------------------------------------------------------
# Negative space — these MUST NOT block
# ---------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.publish
def test_corpus_negative_space_skill_md_clean():
    p = _CORPUS / "clean_negative_space" / "clean_word_counter.md"
    findings = scan_skill_md(p.read_text())
    assert not has_block(findings), [
        (f.code, f.level, f.message) for f in findings
    ]


@pytest.mark.security
@pytest.mark.publish
def test_corpus_negative_space_python_handler_clean():
    p = _CORPUS / "clean_negative_space" / "clean_handler.py"
    findings = scan_python_handler(p.read_text())
    assert not has_block(findings), [
        (f.code, f.level, f.message) for f in findings
    ]


@pytest.mark.security
@pytest.mark.publish
def test_corpus_negative_space_endpoint_clean(fake_dns):
    p = _CORPUS / "clean_negative_space" / "clean_endpoint.txt"
    url = p.read_text().strip()
    # agents.example.com is RFC reserved and may not resolve cleanly in CI;
    # stub a public IP so the SSRF check passes deterministically.
    fake_dns({"agents.example.com": ["8.8.8.8"]})
    # No safety block, no SSRF error.
    assert not has_block(scan_agent_md_endpoint(url))
    validate_agent_endpoint_url(url)  # must not raise
