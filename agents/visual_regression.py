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
    )
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
