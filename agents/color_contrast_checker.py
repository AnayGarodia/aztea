# OWNS: computing WCAG 2.1 relative luminance and contrast ratios for color pairs
# NOT OWNS: CSS parsing, design system auditing, color scheme generation
# INVARIANTS: uses exact WCAG 2.1 formula for relative luminance; never approximates
# DECISIONS: sRGB gamma correction via the piecewise linear + power function as specified in WCAG 2.1
"""
Color Contrast Checker — WCAG 2.1 accessibility compliance.

Inputs: foreground/background CSS colors (#RRGGBB, #RGB, rgb(), named) or
a batch via pairs list (max 50). Outputs contrast ratio, luminances, AA/AAA
grades, and recommendations. External dependencies: none — pure math only.
"""

import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_PAIRS = 50

WCAG_AA_NORMAL = 4.5
WCAG_AA_LARGE = 3.0
WCAG_AAA_NORMAL = 7.0
WCAG_AAA_LARGE = 4.5

# Piecewise linearisation threshold per WCAG 2.1 spec
_SRGB_THRESHOLD = 0.04045
_SRGB_LINEAR_DIVISOR = 12.92
_SRGB_GAMMA_OFFSET = 0.055
_SRGB_GAMMA_DIVISOR = 1.055
_SRGB_GAMMA_EXP = 2.4

# Luminance coefficients per WCAG 2.1 spec (ITU-R BT.709)
_LUM_R = 0.2126
_LUM_G = 0.7152
_LUM_B = 0.0722

# Contrast ratio offset per WCAG 2.1 spec
_CONTRAST_OFFSET = 0.05

_NAMED_COLORS: dict[str, str] = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000",
    "green": "#008000", "blue": "#0000ff", "yellow": "#ffff00",
    "orange": "#ffa500", "purple": "#800080", "pink": "#ffc0cb",
    "gray": "#808080", "grey": "#808080", "cyan": "#00ffff",
    "magenta": "#ff00ff", "lime": "#00ff00", "navy": "#000080",
    "teal": "#008080", "silver": "#c0c0c0", "maroon": "#800000",
    "olive": "#808000", "aqua": "#00ffff",
}


# ---------------------------------------------------------------------------
# WCAG 2.1 math — pure functions, no side effects
# ---------------------------------------------------------------------------

def _linearize(c: float) -> float:
    """Convert sRGB channel value (0.0–1.0) to linear light value per WCAG 2.1."""
    if c <= _SRGB_THRESHOLD:
        return c / _SRGB_LINEAR_DIVISOR
    return ((c + _SRGB_GAMMA_OFFSET) / _SRGB_GAMMA_DIVISOR) ** _SRGB_GAMMA_EXP


def _relative_luminance(r: int, g: int, b: int) -> float:
    """WCAG 2.1 relative luminance from 8-bit RGB channels."""
    rl = _linearize(r / 255)
    gl = _linearize(g / 255)
    bl = _linearize(b / 255)
    return _LUM_R * rl + _LUM_G * gl + _LUM_B * bl


def _contrast_ratio(lum1: float, lum2: float) -> float:
    """WCAG 2.1 contrast ratio; always returns a value >= 1."""
    lighter = max(lum1, lum2)
    darker = min(lum1, lum2)
    return (lighter + _CONTRAST_OFFSET) / (darker + _CONTRAST_OFFSET)


# ---------------------------------------------------------------------------
# Color parsing
# ---------------------------------------------------------------------------

def _parse_hex(raw: str) -> tuple[int, int, int] | None:
    """Parse #RGB, #RRGGBB, or #RRGGBBAA hex strings."""
    h = raw.lstrip("#")
    if len(h) == 3:
        r, g, b = (int(c * 2, 16) for c in h)
        return r, g, b
    if len(h) in (6, 8):
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return None


def _parse_rgb_channel(token: str) -> int:
    """Parse a single rgb() channel — integer 0-255 or percentage string."""
    token = token.strip()
    if token.endswith("%"):
        return round(float(token[:-1]) / 100 * 255)
    return int(token)


def _parse_rgb_func(raw: str) -> tuple[int, int, int] | None:
    """Parse rgb(R,G,B) or rgba(R,G,B,A) — ignores alpha."""
    match = re.match(r"rgba?\(\s*(.+?)\s*\)", raw, re.IGNORECASE)
    if not match:
        return None
    parts = match.group(1).split(",")
    if len(parts) < 3:
        return None
    try:
        r = _parse_rgb_channel(parts[0])
        g = _parse_rgb_channel(parts[1])
        b = _parse_rgb_channel(parts[2])
        return r, g, b
    except (ValueError, IndexError):
        return None


def _parse_color(raw: str) -> tuple[int, int, int] | None:
    """
    Resolve a CSS color string to (r, g, b) integers.

    Supported: #RRGGBB, #RGB, #RRGGBBAA, rgb(), rgba(), named colors (20 common).
    Returns None when the string cannot be unambiguously resolved.
    """
    s = raw.strip().lower()
    if s in _NAMED_COLORS:
        return _parse_hex(_NAMED_COLORS[s])
    if s.startswith("#"):
        return _parse_hex(s)
    if s.startswith("rgb"):
        return _parse_rgb_func(s)
    return None


def _normalize_hex(r: int, g: int, b: int) -> str:
    """Format RGB channels as lowercase 6-digit hex string."""
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# Grading and recommendations
# ---------------------------------------------------------------------------

def _grade(ratio: float) -> str:
    """Assign WCAG grade label based on contrast ratio."""
    if ratio >= WCAG_AAA_NORMAL:
        return "AAA"
    if ratio >= WCAG_AA_NORMAL:
        return "AA"
    if ratio >= WCAG_AA_LARGE:
        return "AA Large"
    return "Fail"


def _build_wcag_flags(ratio: float) -> dict:
    """Return WCAG AA and AAA pass/fail flags for all text and UI categories."""
    return {
        "wcag_aa": {
            "normal_text": ratio >= WCAG_AA_NORMAL,
            "large_text": ratio >= WCAG_AA_LARGE,
            "ui_components": ratio >= WCAG_AA_LARGE,
        },
        "wcag_aaa": {
            "normal_text": ratio >= WCAG_AAA_NORMAL,
            "large_text": ratio >= WCAG_AAA_LARGE,
        },
    }


def _recommendations(ratio: float, fg_lum: float, bg_lum: float) -> list[str]:
    """
    Produce actionable suggestions for color pairs that fail WCAG thresholds.

    Only populated when the pair does not reach AA Large (ratio < 3.0) or AA (ratio < 4.5).
    """
    if ratio >= WCAG_AA_NORMAL:
        return []
    recs: list[str] = []
    if ratio < WCAG_AA_LARGE:
        recs.append(
            "This combination fails all WCAG standards — significant redesign needed"
        )
        if fg_lum < 0.18 and bg_lum < 0.18:
            recs.append("Try a lighter foreground color")
    else:
        recs.append(
            "Passes AA for large text (≥18pt or 14pt bold) only"
        )
        recs.append(
            "Increase contrast to at least 4.5:1 for WCAG AA compliance on normal text"
        )
    return recs


# ---------------------------------------------------------------------------
# Single-pair analysis
# ---------------------------------------------------------------------------

def _error(code: str, message: str, extra: dict | None = None) -> dict:
    """Return a structured error envelope."""
    payload: dict = {"code": code, "message": message}
    if extra:
        payload.update(extra)
    return {"error": payload}


def _analyze_pair(fg_raw: str, bg_raw: str) -> dict:
    """
    Compute contrast metrics for a single foreground/background pair.

    Raises no exceptions — returns an error envelope on parse failure.
    """
    fg_rgb = _parse_color(fg_raw)
    if fg_rgb is None:
        return _error(
            "color_contrast_checker.invalid_color",
            f"Cannot parse foreground color: {fg_raw!r}",
            {"color": fg_raw},
        )
    bg_rgb = _parse_color(bg_raw)
    if bg_rgb is None:
        return _error(
            "color_contrast_checker.invalid_color",
            f"Cannot parse background color: {bg_raw!r}",
            {"color": bg_raw},
        )

    fg_lum = _relative_luminance(*fg_rgb)
    bg_lum = _relative_luminance(*bg_rgb)
    ratio = _contrast_ratio(fg_lum, bg_lum)
    ratio_rounded = round(ratio, 2)

    result = {
        "foreground": _normalize_hex(*fg_rgb),
        "background": _normalize_hex(*bg_rgb),
        "foreground_luminance": round(fg_lum, 6),
        "background_luminance": round(bg_lum, 6),
        "contrast_ratio": ratio_rounded,
        "contrast_ratio_str": f"{ratio_rounded}:1",
        **_build_wcag_flags(ratio),
        "grade": _grade(ratio),
        "recommendations": _recommendations(ratio, fg_lum, bg_lum),
    }
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(payload: dict) -> dict:
    """
    Compute WCAG 2.1 contrast ratios and accessibility grades for one or more color pairs.

    Accepts either a single {foreground, background} or a batch via {pairs: [...]}.
    Returns structured error envelopes on invalid input — never raises.
    """
    has_single = "foreground" in payload or "background" in payload
    has_batch = "pairs" in payload

    if has_single and has_batch:
        return _error(
            "color_contrast_checker.ambiguous_input",
            "Provide either 'foreground'/'background' for a single pair or 'pairs' for batch — not both.",
        )

    if not has_single and not has_batch:
        return _error(
            "color_contrast_checker.missing_colors",
            "Provide 'foreground' and 'background', or a 'pairs' list.",
        )

    if has_single:
        fg = payload.get("foreground", "")
        bg = payload.get("background", "")
        if not fg or not bg:
            return _error(
                "color_contrast_checker.missing_colors",
                "Both 'foreground' and 'background' are required for a single pair.",
            )
        return _analyze_pair(fg, bg)

    # Batch path
    pairs = payload["pairs"]
    if not isinstance(pairs, list):
        return _error(
            "color_contrast_checker.missing_colors",
            "'pairs' must be a list of {foreground, background} objects.",
        )
    if len(pairs) > MAX_PAIRS:
        return _error(
            "color_contrast_checker.too_many_pairs",
            f"Batch size {len(pairs)} exceeds the maximum of {MAX_PAIRS}.",
        )

    results = []
    for entry in pairs:
        fg = entry.get("foreground", "")
        bg = entry.get("background", "")
        result = _analyze_pair(fg, bg)
        if "label" in entry:
            result["label"] = entry["label"]
        results.append(result)

    passing_aa = sum(
        1 for r in results
        if "error" not in r and r.get("wcag_aa", {}).get("normal_text", False)
    )
    passing_aaa = sum(
        1 for r in results
        if "error" not in r and r.get("wcag_aaa", {}).get("normal_text", False)
    )

    return {
        "results": results,
        "pairs_checked": len(results),
        "passing_aa": passing_aa,
        "passing_aaa": passing_aaa,
    }
