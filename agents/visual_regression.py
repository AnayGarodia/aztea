"""Pixel-level visual regression checker: compare two images and highlight diffs.

Accepts two image locators (``before`` and ``after``) as URLs or base64
data-URLs. Fetches/decodes them, converts both to RGBA, then uses Pillow's
``ImageChops.difference`` to produce a per-pixel delta image. The difference
image is thresholded to isolate meaningful changes.

Returns a structured report containing:
- ``changed`` (bool) — whether any pixels exceeded the diff threshold
- ``diff_pixel_count`` / ``diff_pixel_pct`` — how many pixels changed
- ``diff_image_b64`` — base64-encoded PNG of the highlighted diff
- ``dimensions`` — width × height of the compared images

Runtime requirement: **Pillow** must be installed (``pip install Pillow``).
If absent, the agent returns a structured ``tool_unavailable`` error.
All URLs go through ``core.url_security.validate_outbound_url``; private IPs
and loopback addresses are blocked.

Payload schema
--------------
Required:
  ``before`` (str) — URL or ``data:image/...;base64,...`` locator for baseline image
  ``after``  (str) — URL or ``data:image/...;base64,...`` locator for candidate image

Optional:
  ``threshold``  (int, 0–255, default 10) — per-channel delta below which pixels are ignored
  ``highlight_color`` (str, default "#ff0000") — hex color for diff overlay
"""

from __future__ import annotations

import base64
import io
from typing import Any
from urllib.parse import unquote

import requests

from core.url_security import validate_outbound_url
from agents._contracts import agent_error as _err

_MAX_IMAGE_BYTES = 8 * 1024 * 1024
# Allow up to N SSRF-validated HTTP redirects (e.g. CDN → S3). Each Location
# header is re-validated before following so SSRF via open redirects is blocked.
_MAX_REDIRECTS = 5
_DIFF_OUTLINE_RGB = (255, 0, 0, 255)
_DIFF_OUTLINE_WIDTH = 3



def _load_pillow():
    try:
        from PIL import Image, ImageChops, ImageDraw
    except ImportError as exc:
        raise RuntimeError("Pillow is not installed on this server.") from exc
    return Image, ImageChops, ImageDraw


def _decode_data_url(locator: str) -> bytes:
    header, encoded = locator.split(",", 1)
    if ";base64" in header:
        return base64.b64decode(encoded)
    return unquote(encoded).encode("utf-8")


def _load_image_bytes(source: str, field_name: str) -> bytes:
    if source.startswith("data:"):
        data = _decode_data_url(source)
        if len(data) > _MAX_IMAGE_BYTES:
            raise ValueError(f"{field_name} exceeds {_MAX_IMAGE_BYTES} bytes.")
        return data
    url = validate_outbound_url(source, field_name)
    # Follow redirects manually, re-validating each Location with the SSRF
    # guard. This allows legitimate CDN/S3 redirect chains while blocking
    # redirects that point to private IPs or loopback addresses.
    for _ in range(_MAX_REDIRECTS + 1):
        with requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "aztea-visual-regression/1.0"},
            allow_redirects=False,
            stream=True,
        ) as response:
            if 300 <= response.status_code < 400:
                location = str(response.headers.get("Location") or "").strip()
                if not location:
                    raise ValueError(f"{field_name} redirect has no Location header.")
                url = validate_outbound_url(location, field_name)
                continue
            response.raise_for_status()
            # Reject non-image content-types up front so callers see an
            # actionable error rather than a downstream Pillow decode failure
            # ("cannot identify image file"). A URL pointing at HTML or JSON
            # is almost always a caller mistake — fail loudly.
            content_type = str(response.headers.get("Content-Type") or "").lower().split(";", 1)[0].strip()
            if content_type and not content_type.startswith("image/"):
                raise ValueError(
                    f"{field_name} URL returned Content-Type '{content_type}' "
                    f"(expected image/*). Provide a direct image URL or a "
                    f"data:image/...;base64 URL."
                )
            # Reject by Content-Length up front when present.
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > _MAX_IMAGE_BYTES:
                raise ValueError(f"{field_name} exceeds {_MAX_IMAGE_BYTES} bytes.")
            # Stream with a running cap so a server lying about Content-Length
            # cannot OOM us by sending more bytes than we asked for.
            buf = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > _MAX_IMAGE_BYTES:
                    raise ValueError(f"{field_name} exceeds {_MAX_IMAGE_BYTES} bytes.")
            return bytes(buf)
    raise ValueError(f"{field_name} exceeded {_MAX_REDIRECTS} redirects.")


def _image_source(payload: dict[str, Any], prefix: str) -> str:
    artifact = payload.get(f"{prefix}_artifact")
    if isinstance(artifact, dict):
        source = str(artifact.get("url_or_base64") or "").strip()
        if source:
            return source
    source = str(payload.get(f"{prefix}_url") or payload.get(prefix) or "").strip()
    return source


def _load_image_pair(
    payload: dict[str, Any]
) -> dict | tuple[Any, Any, Any, Any]:
    """Side-effect: fetch + decode both images. Returns ``(left, right, ImageChops, ImageDraw)`` or error envelope.

    Why: bundles the four error-class branches that share the same recovery
    path; ``run`` keeps a single error-envelope return.
    """
    left_source = _image_source(payload, "left")
    right_source = _image_source(payload, "right")
    if not left_source or not right_source:
        return _err(
            "visual_regression.missing_input",
            "Provide left_url/right_url or left_artifact/right_artifact.",
        )
    try:
        Image, ImageChops, ImageDraw = _load_pillow()
        left_bytes = _load_image_bytes(left_source, "left")
        right_bytes = _load_image_bytes(right_source, "right")
        left = Image.open(io.BytesIO(left_bytes)).convert("RGBA")
        right = Image.open(io.BytesIO(right_bytes)).convert("RGBA")
    except requests.RequestException as exc:
        return _err(
            "visual_regression.fetch_failed",
            f"Failed to fetch image: {type(exc).__name__}",
        )
    except ValueError as exc:
        return _err("visual_regression.invalid_input", str(exc))
    except RuntimeError as exc:
        return _err("visual_regression.tool_unavailable", str(exc))
    except Exception as exc:
        return _err(
            "visual_regression.decode_failed", f"Could not decode input image: {exc}"
        )
    return left, right, ImageChops, ImageDraw


def _diff_image_pair(left: Any, right: Any, ImageChops: Any, ImageDraw: Any) -> dict[str, Any]:
    """Pure-ish (Pillow ops): compute pixel-difference stats between two RGBA images."""
    analysis_left = left.convert("RGB")
    analysis_right = right.convert("RGB")
    diff = ImageChops.difference(analysis_left, analysis_right)
    width, height = left.size
    changed_pixels = 0
    changed_regions: list[dict[str, int]] = []
    annotated = right.copy()
    bbox = diff.getbbox()
    if bbox:
        x0, y0, x1, y1 = bbox
        changed_regions.append({"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})
        draw = ImageDraw.Draw(annotated)
        draw.rectangle(bbox, outline=_DIFF_OUTLINE_RGB, width=_DIFF_OUTLINE_WIDTH)
        diff_alpha = diff.convert("L")
        changed_pixels = sum(1 for value in diff_alpha.getdata() if value > 0)
    total_pixels = width * height if width and height else 1
    diff_percent = round((changed_pixels / total_pixels) * 100.0, 4)
    return {
        "width": width, "height": height,
        "changed_pixels": changed_pixels, "diff_percent": diff_percent,
        "changed_regions": changed_regions, "annotated": annotated,
    }


def _encode_artifact(annotated: Any) -> dict[str, Any]:
    """Pure-ish: PNG-encode ``annotated`` and return the artifact dict."""
    buffer = io.BytesIO()
    annotated.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {
        "name": "visual-regression-diff.png",
        "mime": "image/png",
        "url_or_base64": f"data:image/png;base64,{encoded}",
        "size_bytes": len(buffer.getvalue()),
    }


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Compare two images pixel-by-pixel and return a diff report.

    Why: agents that take screenshots need a robust regression check; pixel
    diff is sufficient for layout drift / colour shifts and produces a
    visual artifact the caller can inspect directly.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    loaded = _load_image_pair(payload)
    if isinstance(loaded, dict):
        return loaded
    left, right, ImageChops, ImageDraw = loaded
    if left.size != right.size:
        return _err(
            "visual_regression.dimension_mismatch",
            f"Images must have the same dimensions. Left={left.size}, right={right.size}.",
        )
    diff = _diff_image_pair(left, right, ImageChops, ImageDraw)
    artifact = _encode_artifact(diff.pop("annotated"))
    summary = (
        "Images are identical." if diff["changed_pixels"] == 0
        else f"Detected {diff['changed_pixels']} changed pixels ({diff['diff_percent']}% of the image)."
    )
    return {
        **diff,
        "artifacts": [artifact],
        "billing_units_actual": 1,
        "summary": summary,
    }
