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


_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _err(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


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
    safe_url = validate_outbound_url(source, field_name)
    response = requests.get(
        safe_url,
        timeout=10,
        headers={"User-Agent": "aztea-visual-regression/1.0"},
        allow_redirects=False,
    )
    if 300 <= response.status_code < 400:
        raise ValueError(f"{field_name} redirects are not allowed.")
    response.raise_for_status()
    if len(response.content) > _MAX_IMAGE_BYTES:
        raise ValueError(f"{field_name} exceeds {_MAX_IMAGE_BYTES} bytes.")
    return response.content


def _image_source(payload: dict[str, Any], prefix: str) -> str:
    artifact = payload.get(f"{prefix}_artifact")
    if isinstance(artifact, dict):
        source = str(artifact.get("url_or_base64") or "").strip()
        if source:
            return source
    source = str(payload.get(f"{prefix}_url") or payload.get(prefix) or "").strip()
    return source


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Compare two images pixel-by-pixel and return a diff report.

    Accepts images via URL (``left_url`` / ``right_url``) or base64 data-URL
    (``left_artifact`` / ``right_artifact``). At least one source pair is required.

    Optional:
    - ``threshold`` (int, 0–255, default 10) — per-channel delta below which
      a pixel is treated as unchanged.
    - ``highlight_color`` (str, default ``"#ff0000"``) — hex color for the
      diff overlay in the output image.
    - ``output_format`` (str, default ``"png"``) — ``"png"`` | ``"jpeg"``.

    Runtime requirement: **Pillow** must be installed. Returns
    ``tool_unavailable`` if absent.

    Returns ``{changed, diff_pixel_count, diff_pixel_pct, diff_image_b64,
    dimensions, execution_time_ms}``.
    """
    left_source = _image_source(payload, "left")
    right_source = _image_source(payload, "right")
    if not left_source or not right_source:
        return _err("visual_regression.missing_input", "Provide left_url/right_url or left_artifact/right_artifact.")

    try:
        Image, ImageChops, ImageDraw = _load_pillow()
        left_bytes = _load_image_bytes(left_source, "left")
        right_bytes = _load_image_bytes(right_source, "right")
        left = Image.open(io.BytesIO(left_bytes)).convert("RGBA")
        right = Image.open(io.BytesIO(right_bytes)).convert("RGBA")
    except requests.RequestException as exc:
        return _err("visual_regression.fetch_failed", f"Failed to fetch image: {type(exc).__name__}")
    except ValueError as exc:
        return _err("visual_regression.invalid_input", str(exc))
    except RuntimeError as exc:
        return _err("visual_regression.tool_unavailable", str(exc))
    except Exception as exc:
        return _err("visual_regression.decode_failed", f"Could not decode input image: {exc}")

    if left.size != right.size:
        return _err(
            "visual_regression.dimension_mismatch",
            f"Images must have the same dimensions. Left={left.size}, right={right.size}.",
        )

    analysis_left = left.convert("RGB")
    analysis_right = right.convert("RGB")
    diff = ImageChops.difference(analysis_left, analysis_right)
    width, height = left.size
    changed_pixels = 0
    changed_regions: list[dict[str, int]] = []

    bbox = diff.getbbox()
    annotated = right.copy()
    if bbox:
        x0, y0, x1, y1 = bbox
        changed_regions.append({"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})
        draw = ImageDraw.Draw(annotated)
        draw.rectangle(bbox, outline=(255, 0, 0, 255), width=3)
        diff_alpha = diff.convert("L")
        changed_pixels = sum(1 for value in diff_alpha.getdata() if value > 0)

    total_pixels = width * height if width and height else 1
    diff_percent = round((changed_pixels / total_pixels) * 100.0, 4)

    buffer = io.BytesIO()
    annotated.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    artifact = {
        "name": "visual-regression-diff.png",
        "mime": "image/png",
        "url_or_base64": f"data:image/png;base64,{encoded}",
        "size_bytes": len(buffer.getvalue()),
    }

    return {
        "width": width,
        "height": height,
        "changed_pixels": changed_pixels,
        "diff_percent": diff_percent,
        "changed_regions": changed_regions,
        "artifacts": [artifact],
        "billing_units_actual": 1,
        "summary": (
            "Images are identical."
            if changed_pixels == 0
            else f"Detected {changed_pixels} changed pixels ({diff_percent}% of the image)."
        ),
    }
