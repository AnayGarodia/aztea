# OWNS: inspecting Unicode strings for codepoints, categories, security risks, and normalization
# NOT OWNS: Unicode database updates, font rendering, locale-specific rules
# INVARIANTS: never makes network requests; uses only stdlib unicodedata module
# DECISIONS: reports homoglyphs by category (confusable Latin lookalikes) rather than full confusables DB — avoids bundling a large dataset

import unicodedata

from agents._contracts import agent_error as _error

MAX_TEXT_LENGTH = 10_000
MAX_BATCH_SIZE = 20
MAX_CHARACTERS_LIST = 500

_INVISIBLE_CODEPOINTS = frozenset({
    0x00AD,  # SOFT HYPHEN
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0x2060,  # WORD JOINER
    0xFEFF,  # BOM / ZERO WIDTH NO-BREAK SPACE
    0x180E,  # MONGOLIAN VOWEL SEPARATOR
    0x034F,  # COMBINING GRAPHEME JOINER
})

_BIDI_CONTROLS = frozenset({
    0x200E, 0x200F,          # LRM, RLM
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # LRE, RLE, PDF, LRO, RLO
    0x2066, 0x2067, 0x2068, 0x2069,           # LRI, RLI, FSI, PDI
})

# Cyrillic characters that visually resemble Latin equivalents
_CYRILLIC_LATIN_LOOKALIKES = {
    'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c',
    'х': 'x', 'у': 'y', 'В': 'B', 'Е': 'E', 'М': 'M',
    'Н': 'H', 'О': 'O', 'Р': 'P', 'С': 'C', 'Т': 'T',
    'Х': 'X', 'А': 'A', 'К': 'K',
}

# Greek characters that visually resemble Latin equivalents
_GREEK_LATIN_LOOKALIKES = {
    'α': 'a', 'β': 'b', 'ε': 'e', 'ι': 'i', 'κ': 'k',
    'ο': 'o', 'υ': 'u', 'ν': 'v', 'Α': 'A', 'Β': 'B',
    'Ε': 'E', 'Ζ': 'Z', 'Η': 'H', 'Ι': 'I', 'Κ': 'K',
    'Μ': 'M', 'Ν': 'N', 'Ο': 'O', 'Ρ': 'P', 'Τ': 'T',
    'Υ': 'Y', 'Χ': 'X',
}

# Script name prefixes that map to "Common" rather than a named script
_COMMON_SCRIPT_PREFIXES = frozenset({
    'DIGIT', 'SPACE', 'HYPHEN', 'FULL', 'LEFT', 'RIGHT', 'PLUS',
    'EQUALS', 'LESS', 'GREATER', 'SOLIDUS', 'ASTERISK', 'NUMBER',
    'COMMERCIAL', 'AMPERSAND', 'PERCENT', 'DOLLAR', 'POUND', 'CURRENCY',
    'PUNCTUATION', 'QUOTATION', 'APOSTROPHE', 'COMMA', 'STOP', 'PERIOD',
    'COLON', 'SEMICOLON', 'EXCLAMATION', 'QUESTION', 'TILDE', 'GRAVE',
    'CIRCUMFLEX', 'VERTICAL', 'REVERSE', 'LOW', 'BULLET', 'PILCROW',
    'SECTION', 'COPYRIGHT', 'REGISTERED', 'DEGREE', 'MICRO', 'MIDDLE',
    'HORIZONTAL', 'VERTICAL', 'BOX', 'BLOCK', 'MATHEMATICAL', 'LATIN',
    'NO-BREAK', 'ZERO', 'EM', 'EN', 'FIGURE', 'THIN', 'HAIR',
})

_SECURITY_RELEVANT_CATEGORIES = frozenset({'Cf', 'Cs', 'Co', 'Cn', 'Cc'})


def _derive_script(char: str, name: str) -> str:
    """Derive script from the Unicode character name prefix."""
    if not name:
        return "Unknown"
    first_word = name.split()[0]
    if first_word in _COMMON_SCRIPT_PREFIXES:
        return "Common"
    return first_word


def _is_invisible(char: str) -> bool:
    """Return True if char is invisible: explicit list or format category (except SOFT HYPHEN handled separately)."""
    cp = ord(char)
    if cp in _INVISIBLE_CODEPOINTS:
        return True
    # Format chars (Cf) are invisible; SOFT HYPHEN (0x00AD) already in set above
    return unicodedata.category(char) == 'Cf' and cp not in _BIDI_CONTROLS


def _classify_char(char: str) -> dict:
    """Build per-character classification dict."""
    cp = ord(char)
    name = unicodedata.name(char, "UNKNOWN")
    category = unicodedata.category(char)
    script = _derive_script(char, name)
    return {
        "char": char,
        "codepoint": f"U+{cp:04X}",
        "name": name,
        "category": category,
        "script": script,
        "is_printable": char.isprintable(),
        "is_invisible": _is_invisible(char),
    }


def _build_normalization(text: str) -> dict:
    """Report which normalization forms the text satisfies and where it differs."""
    nfc = unicodedata.normalize("NFC", text)
    nfd = unicodedata.normalize("NFD", text)
    nfkc = unicodedata.normalize("NFKC", text)
    nfkd = unicodedata.normalize("NFKD", text)
    return {
        "is_nfc": text == nfc,
        "is_nfd": text == nfd,
        "is_nfkc": text == nfkc,
        "is_nfkd": text == nfkd,
        "nfc_differs": text != nfc,
        "nfkc_differs": text != nfkc,
    }


def _collect_homoglyphs(text: str) -> list[dict]:
    """Find characters that are confusable Cyrillic/Greek lookalikes for Latin chars."""
    seen: set[str] = set()
    results = []
    all_lookalikes = {**_CYRILLIC_LATIN_LOOKALIKES, **_GREEK_LATIN_LOOKALIKES}
    for char in text:
        if char in all_lookalikes and char not in seen:
            seen.add(char)
            results.append({
                "char": char,
                "looks_like": all_lookalikes[char],
                "codepoint": f"U+{ord(char):04X}",
            })
    return results


def _mixed_script_risk(text: str, scripts: set[str]) -> str:
    """Assess mixed-script risk: high for Latin+Cyrillic in a word, low for other cross-script combos."""
    meaningful = scripts - {"Common", "Unknown"}
    if len(meaningful) < 2:
        return "none"
    # High risk: Latin and Cyrillic together in the same word
    if "LATIN" in meaningful and "CYRILLIC" in meaningful:
        for word in text.split():
            word_scripts = set()
            for ch in word:
                name = unicodedata.name(ch, "")
                word_scripts.add(_derive_script(ch, name))
            if "LATIN" in word_scripts and "CYRILLIC" in word_scripts:
                return "high"
    return "low"


def _build_security(text: str, char_data: list[dict]) -> dict:
    """Aggregate all security-relevant observations about the text."""
    invisible = [d["codepoint"] for d in char_data if d["is_invisible"]]
    bidi = [f"U+{ord(c):04X}" for c in text if ord(c) in _BIDI_CONTROLS]
    scripts = {d["script"] for d in char_data}
    homoglyphs = _collect_homoglyphs(text)
    private_use = [f"U+{ord(c):04X}" for c in text if unicodedata.category(c) == "Co"]
    replacement = any(c == "�" for c in text)
    null_bytes = any(c == "\x00" for c in text)
    suspicious_cats = sorted(
        {d["category"] for d in char_data if d["category"] in _SECURITY_RELEVANT_CATEGORIES}
    )
    mixed_scripts = len({s for s in scripts if s not in ("Common", "Unknown")}) > 1
    risk = _mixed_script_risk(text, scripts)
    return {
        "has_invisible_chars": bool(invisible),
        "invisible_chars": invisible,
        "has_bidi_controls": bool(bidi),
        "bidi_controls": bidi,
        "has_mixed_scripts": mixed_scripts,
        "mixed_script_risk": risk,
        "homoglyph_suspicious_pairs": homoglyphs,
        "has_private_use_chars": bool(private_use),
        "private_use_chars": private_use,
        "has_replacement_chars": replacement,
        "has_null_bytes": null_bytes,
        "suspicious_categories": suspicious_cats,
    }


def _analyze_text(text: str) -> dict:
    """Produce the full inspection report for a single text string."""
    char_data_all = [_classify_char(c) for c in text]
    truncated = len(char_data_all) > MAX_CHARACTERS_LIST
    char_data = char_data_all[:MAX_CHARACTERS_LIST]

    categories: dict[str, int] = {}
    scripts_set: set[str] = set()
    for d in char_data_all:
        categories[d["category"]] = categories.get(d["category"], 0) + 1
        scripts_set.add(d["script"])

    result = {
        "length_chars": len(text),
        "length_bytes_utf8": len(text.encode("utf-8")),
        "scripts_detected": sorted(scripts_set),
        "categories": categories,
        "characters": char_data,
        "normalization": _build_normalization(text),
        "security": _build_security(text, char_data_all),
    }
    if truncated:
        result["truncated_at"] = MAX_CHARACTERS_LIST
    return result


def _validate_inputs(payload: dict) -> tuple[str | None, list[str] | None, dict | None]:
    """
    Validate and extract text/texts from payload.

    Returns (text, texts, error_dict) — exactly one of (text, texts) is non-None on success.
    """
    has_text = "text" in payload
    has_texts = "texts" in payload
    if has_text and has_texts:
        return None, None, _error(
            "unicode_inspector.ambiguous_input",
            "Provide either 'text' or 'texts', not both.",
        )
    if not has_text and not has_texts:
        return None, None, _error(
            "unicode_inspector.missing_text",
            "Provide 'text' (str) or 'texts' (list of str).",
        )
    if has_text:
        text = payload["text"]
        if len(text) > MAX_TEXT_LENGTH:
            return None, None, _error(
                "unicode_inspector.text_too_long",
                f"Text exceeds {MAX_TEXT_LENGTH} characters ({len(text)} given).",
            )
        return text, None, None
    texts = payload["texts"]
    if len(texts) > MAX_BATCH_SIZE:
        return None, None, _error(
            "unicode_inspector.too_many_texts",
            f"Batch exceeds {MAX_BATCH_SIZE} texts ({len(texts)} given).",
        )
    return None, texts, None


def run(payload: dict) -> dict:
    """
    Inspect Unicode strings for codepoints, categories, security risks, and normalization.

    Pure stdlib — no network, no external data. Returns structured analysis or a
    structured error envelope on bad input.
    """
    text, texts, err = _validate_inputs(payload)
    if err is not None:
        return err
    if text is not None:
        return _analyze_text(text)
    results = [_analyze_text(t) for t in texts]
    return {"results": results, "texts_analyzed": len(results)}
