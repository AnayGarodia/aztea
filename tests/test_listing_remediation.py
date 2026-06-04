"""CI guard: every publish-block code has actionable remediation text (DX2)."""
from __future__ import annotations

import pytest

from aztea.cli.wizard import remediation_for

# Every code that can refuse a publish must carry remediation copy so the CLI
# can tell the publisher what to fix. Keep this list in sync with the block
# codes emitted by the verification surface.
_BLOCK_CODES = [
    "listing.duplicate",
    "listing.unreliable.schema",
    "listing.probe_unreachable",
]


@pytest.mark.parametrize("code", _BLOCK_CODES)
def test_block_code_has_remediation(code):
    text = remediation_for(code)
    assert text and text.strip(), f"{code} has no remediation text"
    assert len(text) > 30  # a real sentence, not a stub
