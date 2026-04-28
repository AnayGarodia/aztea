"""
media_generation.py — model-backed image/video generation helpers.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any

import requests

_OPENAI_BASE_URL = "https://api.openai.com/v1"
_REPLICATE_BASE_URL = "https://api.replicate.com/v1"


def _to_float_env(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return float(default)
    try:
        parsed = float(raw)
    except ValueError:
        return float(default)
    return parsed if parsed > 0 else float(default)


def _to_int_env(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return int(default)
    try:
        parsed = int(raw)
    except ValueError:
        return int(default)
    return parsed if parsed > 0 else int(default)


def _is_http_url(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized.startswith("http://") or normalized.startswith("https://")


def _size_bucket(width: int, height: int) -> str:
    if width >= int(height * 1.25):
        return "1536x1024"
    if height >= int(width * 1.25):
        return "1024x1536"
    return "1024x1024"


def _build_image_prompt(prompt: str, style: str, input_images: list[dict[str, str]]) -> tuple[str, list[str]]:
    normalized_prompt = prompt.strip()
    warnings: list[str] = []
    if style.strip():
        normalized_prompt = f"{normalized_prompt}\n\nVisual style: {style.strip()}."
    if input_images:
        warnings.append("Reference-image conditioning is provider-limited; references were converted into text guidance.")
        refs = []
        for idx, item in enumerate(input_images[:4], start=1):
            role = str(item.get("role") or "reference").strip()
            refs.append(f"[reference {idx}: role={role}]")
        normalized_prompt = f"{normalized_prompt}\n\nInspiration references: {' '.join(refs)}."
    return normalized_prompt, warnings


def _openai_image_generation(
    *,
    prompt: str,
    style: str,
    width: int,
    height: int,
    input_images: list[dict[str, str]],
) -> dict[str, Any]:
    api_key = str(os.environ.get("OPENAI_API_KEY", "")).strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for model-backed image generation.")
    model = str(os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")).strip() or "gpt-image-1"
    quality = str(os.environ.get("OPENAI_IMAGE_QUALITY", "high")).strip() or "high"
    timeout_seconds = _to_float_env("OPENAI_IMAGE_TIMEOUT_SECONDS", 120)
    final_prompt, warnings = _build_image_prompt(prompt, style, input_images)
    payload = {
        "model": model,
        "prompt": final_prompt,
        "size": _size_bucket(width, height),
        "quality": quality,
        "response_format": "b64_json",
    }
    response = requests.post(
        f"{_OPENAI_BASE_URL}/images/generations",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout_seconds,
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise ValueError(f"OpenAI image generation failed ({response.status_code}): {detail}")
    body = response.json()
    rows = body.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("OpenAI image generation returned no image payload.")
    first = rows[0] if isinstance(rows[0], dict) else {}
    b64 = str(first.get("b64_json") or "").strip()
    image_url = str(first.get("url") or "").strip()
    if b64:
        binary = base64.b64decode(b64, validate=False)
        artifact = {
            "name": "generated.png",
            "mime": "image/png",
            "url_or_base64": f"data:image/png;base64,{b64}",
            "size_bytes": len(binary),
        }
    elif _is_http_url(image_url):
        artifact = {
            "name": "generated.png",
            "mime": "image/png",
            "url_or_base64": image_url,
            "size_bytes": 0,
        }
    else:
        raise ValueError("OpenAI image generation response did not include b64_json or url.")
    return {
        "provider": "openai",
        "model": model,
        "artifact": artifact,
        "warnings": warnings,
        "generation_prompt": final_prompt,
    }


def _replicate_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }


def _replicate_prediction_request(model_spec: str, model_input: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    normalized = str(model_spec or "").strip()
    if not normalized:
        raise ValueError("Replicate model spec is empty.")
    if ":" in normalized:
        owner_name, version = normalized.split(":", 1)
        if "/" not in owner_name or not version.strip():
            raise ValueError("Replicate model with version must look like 'owner/model:version'.")
        return f"{_REPLICATE_BASE_URL}/predictions", {"version": version.strip(), "input": model_input}
    if "/" not in normalized:
        raise ValueError("Replicate model must look like 'owner/model' or 'owner/model:version'.")
    return f"{_REPLICATE_BASE_URL}/models/{normalized}/predictions", {"input": model_input}


def _replicate_poll_result(*, token: str, status_url: str, timeout_seconds: float) -> dict[str, Any]:
    poll_interval = _to_float_env("REPLICATE_POLL_INTERVAL_SECONDS", 2)
    started = time.time()
    while True:
        response = requests.get(status_url, headers=_replicate_headers(token), timeout=timeout_seconds)
        if response.status_code >= 400:
            raise ValueError(
                f"Replicate polling failed ({response.status_code}): {response.text[:500]}"
            )
        body = response.json()
        status = str(body.get("status") or "").strip().lower()
        if status == "succeeded":
            return body
        if status in {"failed", "canceled", "cancelled"}:
            error_message = str(body.get("error") or "prediction failed").strip()
            raise ValueError(f"Replicate prediction failed: {error_message}")
        if time.time() - started > timeout_seconds:
            raise ValueError("Replicate prediction timed out before completion.")
        time.sleep(poll_interval)


def _extract_replicate_output_url(output: Any) -> str | None:
    if isinstance(output, str) and _is_http_url(output):
        return output
    if isinstance(output, list):
        for item in output:
            url = _extract_replicate_output_url(item)
            if url:
                return url
    if isinstance(output, dict):
        for key in ("url", "video", "image", "output", "file"):
            value = output.get(key)
            if isinstance(value, str) and _is_http_url(value):
                return value
        for value in output.values():
            url = _extract_replicate_output_url(value)
            if url:
                return url
    return None


def _replicate_run(*, model_spec: str, model_input: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    token = str(os.environ.get("REPLICATE_API_TOKEN", "")).strip()
    if not token:
        raise ValueError("REPLICATE_API_TOKEN is required for model-backed video/image generation via Replicate.")
    create_url, payload = _replicate_prediction_request(model_spec, model_input)
    create = requests.post(
        create_url,
        headers=_replicate_headers(token),
        json=payload,
        timeout=timeout_seconds,
    )
    if create.status_code >= 400:
        raise ValueError(f"Replicate prediction create failed ({create.status_code}): {create.text[:500]}")
    created = create.json()
    status = str(created.get("status") or "").strip().lower()
    if status == "succeeded":
        return created
    status_url = str(created.get("urls", {}).get("get") or "").strip()
    if not _is_http_url(status_url):
        raise ValueError("Replicate create response did not include a valid polling URL.")
    return _replicate_poll_result(token=token, status_url=status_url, timeout_seconds=timeout_seconds)


def generate_image(
    *,
    prompt: str,
    style: str,
    width: int,
    height: int,
    input_images: list[dict[str, str]],
) -> dict[str, Any]:
    """Generate an image via OpenAI DALL-E (preferred) or Replicate.

    Returns a dict with ``provider``, ``model``, ``artifact`` (name/mime/url_or_base64),
    ``warnings``, and ``generation_prompt``. Raises ``ValueError`` if no provider is configured.
    """
    if str(os.environ.get("OPENAI_API_KEY", "")).strip():
        return _openai_image_generation(
            prompt=prompt,
            style=style,
            width=width,
            height=height,
            input_images=input_images,
        )

    replicate_model = str(os.environ.get("REPLICATE_IMAGE_MODEL", "")).strip()
    if replicate_model:
        timeout_seconds = _to_float_env("REPLICATE_TIMEOUT_SECONDS", 300)
        final_prompt, warnings = _build_image_prompt(prompt, style, input_images)
        prediction = _replicate_run(
            model_spec=replicate_model,
            model_input={"prompt": final_prompt},
            timeout_seconds=timeout_seconds,
        )
        image_url = _extract_replicate_output_url(prediction.get("output"))
        if not image_url:
            raise ValueError("Replicate image prediction completed but no output URL was returned.")
        return {
            "provider": "replicate",
            "model": replicate_model,
            "artifact": {
                "name": "generated.png",
                "mime": "image/png",
                "url_or_base64": image_url,
                "size_bytes": 0,
            },
            "warnings": warnings,
            "generation_prompt": final_prompt,
        }

    raise ValueError(
        "No image generation model configured. Set OPENAI_API_KEY (recommended) or REPLICATE_IMAGE_MODEL + REPLICATE_API_TOKEN."
    )


def generate_video(
    *,
    brief: str,
    style: str,
    duration_seconds: int,
    aspect_ratio: str,
    reference_images: list[dict[str, str]],
) -> dict[str, Any]:
    """Generate a video via Replicate from a text brief.

    Requires ``REPLICATE_VIDEO_MODEL`` and ``REPLICATE_API_TOKEN`` env vars.
    Returns a dict with ``provider``, ``model``, ``artifact`` (video URL), and ``warnings``.
    Raises ``ValueError`` if the model is not configured.
    """
    model = str(os.environ.get("REPLICATE_VIDEO_MODEL", "")).strip()
    if not model:
        raise ValueError("REPLICATE_VIDEO_MODEL is required for video generation.")
    timeout_seconds = _to_float_env("REPLICATE_TIMEOUT_SECONDS", 300)
    prompt = brief.strip()
    if style.strip():
        prompt = f"{prompt}\n\nVisual style: {style.strip()}."
    model_input: dict[str, Any] = {
        "prompt": prompt,
        "duration": max(3, min(int(duration_seconds), 20)),
        "aspect_ratio": aspect_ratio.strip() or "16:9",
    }
    if reference_images:
        first = str(reference_images[0].get("url_or_base64") or "").strip()
        if first:
            model_input["image"] = first

    prediction = _replicate_run(
        model_spec=model,
        model_input=model_input,
        timeout_seconds=timeout_seconds,
    )
    video_url = _extract_replicate_output_url(prediction.get("output"))
    if not video_url:
        raise ValueError("Replicate video prediction completed but no video URL was returned.")
    prediction_id = str(prediction.get("id") or "").strip()
    return {
        "provider": "replicate",
        "model": model,
        "artifact": {
            "name": "generated.mp4",
            "mime": "video/mp4",
            "url_or_base64": video_url,
            "size_bytes": 0,
        },
        "prediction_id": prediction_id,
        "generation_prompt": prompt,
    }
