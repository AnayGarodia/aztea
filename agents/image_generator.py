"""
image_generator.py — model-backed image generation with multimodal payload support.
"""

from __future__ import annotations

from typing import Any

from agents import media_generation


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _normalize_media_refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mime") or "").strip()
        ref = str(item.get("url_or_base64") or "").strip()
        role = str(item.get("role") or "reference").strip() or "reference"
        if not mime or not ref:
            continue
        refs.append({"mime": mime, "url_or_base64": ref, "role": role})
    return refs


def _generate_image_artifact(
    *,
    prompt: str,
    style: str,
    width: int,
    height: int,
    input_images: list[dict[str, str]],
) -> dict[str, Any]:
    return media_generation.generate_image(
        prompt=prompt,
        style=style,
        width=width,
        height=height,
        input_images=input_images,
    )


def run(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt is required"}

    style = str(payload.get("style") or "").strip()
    width = _clamp_int(payload.get("width"), 1024, 256, 2048)
    height = _clamp_int(payload.get("height"), 1024, 256, 2048)
    image_count = _clamp_int(payload.get("image_count"), 1, 1, 6)
    high_res = bool(payload.get("high_res"))
    requested_format = str(payload.get("output_format") or "png").strip().lower()
    input_images = _normalize_media_refs(payload.get("input_images"))

    artifacts: list[dict[str, Any]] = []
    provider = ""
    model = ""
    warnings: list[str] = []
    for _ in range(image_count):
        generated = _generate_image_artifact(
            prompt=prompt,
            style=style,
            width=width,
            height=height,
            input_images=input_images,
        )
        warnings.extend(list(generated.get("warnings") or []))
        artifacts.append(generated["artifact"])
        provider = str(generated.get("provider") or provider)
        model = str(generated.get("model") or model)

    if requested_format not in {"png", "image/png", "jpg", "jpeg", "webp"}:
        warnings.append(
            f"Requested output_format '{requested_format}' is unsupported by current model path; returned PNG artifact."
        )
    return {
        "summary": f"Generated {len(artifacts)} image artifact{'s' if len(artifacts) != 1 else ''} using a live model backend.",
        "generation_prompt": prompt,
        "artifacts": artifacts,
        "input_images_used": len(input_images),
        "high_res": high_res,
        "warnings": warnings,
        "provider": provider,
        "model": model,
        # Report the actual image count rendered so pricing refunds can
        # reconcile callers who requested more than we produced.
        "billing_units_actual": len(artifacts),
    }
