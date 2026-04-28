from __future__ import annotations

import base64

from core.output_shaping import shape_output


def test_shape_output_truncates_large_strings_in_summary_mode():
    payload = {"summary": "x" * 5000}
    shaped, truncated = shape_output(payload, "summary")
    assert truncated is True
    assert len(shaped["summary"]) < 2500
    assert "[truncated" in shaped["summary"]


def test_shape_output_replaces_large_base64_blobs():
    # Use varied bytes so the base64 string has enough unique characters to pass
    # the entropy check in _looks_like_base64_blob (requires >= 8 unique chars).
    blob = base64.b64encode(bytes(range(256)) * 12).decode("ascii")
    shaped, truncated = shape_output({"image_base64": blob}, "summary")
    assert truncated is True
    assert shaped["image_base64"]["_artifact_id"]
    assert shaped["image_base64"]["size_bytes"] >= 3000


def test_shape_output_full_mode_returns_original_payload():
    payload = {"items": list(range(100)), "text": "y" * 4000}
    shaped, truncated = shape_output(payload, "full")
    assert truncated is False
    assert shaped == payload
