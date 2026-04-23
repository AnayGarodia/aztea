"""
video_storyboard.py — text brief to storyboard + generated video artifact.
"""

from __future__ import annotations

from typing import Any

from agents import media_generation


def _to_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def _normalize_ratio(value: Any) -> str:
    raw = str(value or "").strip()
    if raw in {"16:9", "9:16", "1:1", "4:5", "21:9"}:
        return raw
    return "16:9"


def _normalize_refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mime") or "").strip()
        ref = str(item.get("url_or_base64") or "").strip()
        if not mime or not ref:
            continue
        refs.append({"mime": mime, "url_or_base64": ref})
    return refs


def _extract_lines(brief: str) -> list[str]:
    chunks = [part.strip(" .") for part in brief.replace("\n", " ").split(".")]
    lines = [chunk for chunk in chunks if chunk]
    return lines or [brief.strip()]


def _build_shot_plan(brief: str, duration_seconds: int) -> list[dict[str, Any]]:
    lines = _extract_lines(brief)
    shot_count = min(8, max(3, len(lines)))
    seconds_per_shot = max(1, duration_seconds // shot_count)
    shot_plan: list[dict[str, Any]] = []
    current_start = 0
    for idx in range(shot_count):
        prompt = lines[idx % len(lines)]
        start_second = current_start
        end_second = duration_seconds if idx == shot_count - 1 else min(duration_seconds, current_start + seconds_per_shot)
        current_start = end_second
        shot_plan.append(
            {
                "shot_id": idx + 1,
                "start_second": start_second,
                "end_second": end_second,
                "visual_prompt": prompt,
                "motion": "cinematic movement",
                "onscreen_text": prompt[:80],
                "voiceover_line": f"{prompt.capitalize()}.",
            }
        )
    return shot_plan


def _generate_video_artifact(
    *,
    brief: str,
    style: str,
    duration_seconds: int,
    aspect_ratio: str,
    reference_images: list[dict[str, str]],
) -> dict[str, Any]:
    return media_generation.generate_video(
        brief=brief,
        style=style,
        duration_seconds=duration_seconds,
        aspect_ratio=aspect_ratio,
        reference_images=reference_images,
    )


def run(payload: dict[str, Any]) -> dict[str, Any]:
    brief = str(payload.get("brief") or "").strip()
    if not brief:
        return {"error": "brief is required"}

    duration_seconds = _to_int(payload.get("duration_seconds"), 8, 3, 20)
    aspect_ratio = _normalize_ratio(payload.get("aspect_ratio"))
    style = str(payload.get("style") or "cinematic").strip()[:120]
    references = _normalize_refs(payload.get("reference_images"))
    shot_plan = _build_shot_plan(brief, duration_seconds)
    generated = _generate_video_artifact(
        brief=brief,
        style=style,
        duration_seconds=duration_seconds,
        aspect_ratio=aspect_ratio,
        reference_images=references,
    )

    return {
        "title": f"Storyboard: {brief[:72]}",
        "duration_seconds": duration_seconds,
        "aspect_ratio": aspect_ratio,
        "style": style,
        "shot_plan": shot_plan,
        "voiceover_script": " ".join(str(shot.get("voiceover_line") or "") for shot in shot_plan).strip(),
        "render_recipe": {
            "target_fps": 24,
            "transition_style": "cinematic cuts",
            "music_style": "ambient inspirational",
            "reference_images_used": len(references),
            "prompt_pack": [str(shot.get("visual_prompt") or "") for shot in shot_plan],
            "provider": str(generated.get("provider") or ""),
            "model": str(generated.get("model") or ""),
            "prediction_id": str(generated.get("prediction_id") or ""),
        },
        "artifacts": [generated["artifact"]],
        # Report the actual seconds rendered so the registry can refund
        # callers who pre-charged for a longer duration than we produced.
        "billing_units_actual": duration_seconds,
    }
