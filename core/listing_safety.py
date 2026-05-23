"""
listing_safety.py — Pre-flight checks for new agent listings.

# OWNS: deterministic content scans (SKILL.md, Python handler, agent.md) and
#   the synthetic + adversarial endpoint probe used at listing time.
# NOT OWNS: SSRF / outbound-URL validation (lives in core/url_security.py),
#   the actual register/insert path (lives in core/registry/agents_ops.py),
#   or the LLM-backed dispute judge (core/judges.py).
# INVARIANTS:
#   - Every public entry point returns list[VerificationFinding]; never raises
#     on user content. Raises only on programmer error (wrong type passed in).
#   - A finding with level="block" MUST short-circuit the publish flow.
#     warn never short-circuits unless the caller passes strict=True.
#   - Scanners are pure: input bytes → list of findings. No network, no DB.
#     Only probe_endpoint() does I/O, and it is explicitly named.
# DECISIONS:
#   - Regex over LLM. Speed and determinism win; an LLM scan can layer on top
#     later as a separate stage without rewriting this module.
#   - Findings are codes + messages, not bare strings — callers (CLI receipt,
#     server error envelope, future audit log) all need the code stable.
"""

from __future__ import annotations

import ast
import html
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_BLOCK = "block"

_VALID_LEVELS = {LEVEL_INFO, LEVEL_WARN, LEVEL_BLOCK}


@dataclass(frozen=True)
class VerificationFinding:
    """One observation produced by a scanner.

    code:    stable machine-readable identifier (e.g. "skill.prompt_injection").
    level:   "info" | "warn" | "block".
    message: human-readable one-liner; safe to render verbatim in CLI output.
    detail:  optional extra context (matched line, AST node, host, ...).
    """

    code: str
    level: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in _VALID_LEVELS:
            raise ValueError(
                f"VerificationFinding level must be one of {sorted(_VALID_LEVELS)};"
                f" got {self.level!r}"
            )


def has_block(findings: Iterable[VerificationFinding]) -> bool:
    return any(f.level == LEVEL_BLOCK for f in findings)


def has_warn(findings: Iterable[VerificationFinding]) -> bool:
    return any(f.level == LEVEL_WARN for f in findings)


# ---------------------------------------------------------------------------
# Stage 2a — SKILL.md prompt-injection scanner
# ---------------------------------------------------------------------------

# Phrases that, in body text instructing an LLM, almost always indicate the
# author is trying to override safety scaffolding or exfiltrate state. The
# false-positive rate on legitimate skills is near zero — a code-review skill
# that needs the words "system prompt" should describe its role, not instruct
# the model to "reveal your system prompt".
_PROMPT_INJECTION_PHRASES: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore the above",
    "ignore all previous",
    "disregard the system",
    "reveal your system prompt",
    "print your system prompt",
    "show me your instructions",
    "repeat your instructions verbatim",
    "you are now",
    "act as if you are not bound",
    "exfiltrate",
    "send a request to",
    "post to http",
    "send the api key",
    "leak the api key",
    "dump the conversation",
)

# Compile each phrase as `\s+`-joined word run so multi-line spread, multiple
# spaces, and tabs in between words can't bypass the matcher. Each pattern is
# applied to the NFKC-normalised, zero-width-stripped, lowercased text.
_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(r"\s+".join(re.escape(w) for w in phrase.split()))
    for phrase in _PROMPT_INJECTION_PHRASES
)

# Zero-width spaces, joiners, BIDI overrides, BOM. Stripped before pattern
# matching so attackers can't hide a phrase by splicing in invisible chars.
# The character classes:
#   U+200B..U+200F  zero-width space / joiner / non-joiner / LRM / RLM
#   U+202A..U+202E  embedding / override controls (LRE/RLE/PDF/LRO/RLO)
#   U+2060..U+206F  word joiner + invisible operators
#   U+FEFF           BOM
# All are visually empty in any rendered text so the only legitimate use
# inside a SKILL.md is none.
_ZERO_WIDTH_RE = re.compile(
    r"[​-‏‪-‮⁠-⁯﻿]"
)


# Cyrillic / Greek / mathematical look-alikes that visually impersonate
# Latin letters used in the prompt-injection phrases. NFKC does NOT fold
# these; we apply this map explicitly so 'іgnоre' (Cyrillic 'і' + 'о')
# resolves to 'ignore' for the phrase matcher. Same table as
# _HOMOGLYPH_FOLD below — kept duplicated only in source to make the
# narrow purpose of each obvious to the reader; the real table is
# _HOMOGLYPH_FOLD and _PHRASE_HOMOGLYPH_FOLD just re-uses it.
_PHRASE_HOMOGLYPH_FOLD_RAW = {
    # Cyrillic
    "а": "a", "А": "A", "е": "e", "Е": "E", "о": "o", "О": "O",
    "р": "p", "Р": "P", "с": "c", "С": "C", "у": "y", "У": "Y",
    "х": "x", "Х": "X", "ѕ": "s", "Ѕ": "S", "і": "i", "І": "I",
    "ј": "j", "Ј": "J", "ԛ": "q", "Ԛ": "Q", "ԝ": "w", "Ԝ": "W",
    # Greek capital that visually = Latin
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I",
    "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T",
    "Υ": "Y", "Χ": "X",
    # Greek lowercase look-alikes
    "α": "a", "ο": "o", "ρ": "p", "ν": "v",
}
_PHRASE_HOMOGLYPH_FOLD = str.maketrans(_PHRASE_HOMOGLYPH_FOLD_RAW)


def _normalize_for_phrase_scan(text: str) -> str:
    """Canonicalise text before phrase matching.

    Order matters:

      1. ``html.unescape`` turns ``&#105;`` / ``&amp;`` into the underlying
         characters so attackers can't smuggle phrases as numeric character
         references that render in any markdown viewer.
      2. NFKD decomposes accented chars (e.g. "ó" → "o" + combining acute);
         dropping combining marks (category Mn) gives the base char alone.
      3. Zero-width + BIDI control chars are stripped (incl. U+202E RLO,
         which would otherwise let an attacker spell a phrase backwards).
      4. Cyrillic / Greek / math look-alikes are folded to their Latin
         equivalents via the shared _PHRASE_HOMOGLYPH_FOLD table.
      5. The result is lowercased once so all downstream matchers can
         assume case-folded input.

    Pure function — input bytes in, canonical bytes out.
    """
    unescaped = html.unescape(text)
    decomposed = unicodedata.normalize("NFKD", unescaped)
    no_marks = "".join(
        ch for ch in decomposed if unicodedata.category(ch) != "Mn"
    )
    no_invisible = _ZERO_WIDTH_RE.sub("", no_marks)
    folded = no_invisible.translate(_PHRASE_HOMOGLYPH_FOLD)
    return folded.lower()

# API-key-shaped substrings we never want hardcoded inside a published skill.
# Live keys here are an obvious leak; placeholder ones are a smell.
#
# When you add a new provider here, also add a corresponding entry to
# tests/test_listing_safety.py (positive sample) so the regex is exercised.
# The 2026-05-22 expansion added Google, Stripe, HuggingFace, SendGrid,
# Twilio, Mailgun, and the AWS *secret* (40-char base64) after a publish-
# robustness audit (tests/security/GAP_REPORT.md A4) showed these formats
# slipped past the scanner unrecognised.
_API_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI legacy "sk-..." (alphanumeric body, no internal hyphens).
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    # OpenAI modern scoped formats: sk-proj-..., sk-svcacct-..., sk-admin-...
    # The body uses base64url-ish chars including '-' and '_', so the legacy
    # pattern above fails on the very first internal '-'. Cover them
    # explicitly so embedded keys can't slip past the static scanner.
    re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),  # Anthropic-style
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),        # Groq
    re.compile(r"\bazk_[A-Za-z0-9]{20,}\b"),        # Aztea worker
    re.compile(r"\bazac_[A-Za-z0-9]{20,}\b"),       # Aztea agent-caller
    re.compile(r"\baz_[A-Za-z0-9]{32,}\b"),         # Aztea user/master
    re.compile(r"\bxoxb-[A-Za-z0-9\-]{20,}\b"),     # Slack bot
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),            # AWS access key ID
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),        # GitHub personal access
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),# GitHub fine-grained
    # Added 2026-05-22:
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),     # Google API key (typically 39 chars total)
    re.compile(r"\bsk_(?:live|test)_[0-9A-Za-z]{24,}\b"),  # Stripe secret
    re.compile(r"\brk_(?:live|test)_[0-9A-Za-z]{24,}\b"),  # Stripe restricted
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),         # HuggingFace
    re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{30,}\b"),  # SendGrid
    re.compile(r"\bAC[a-f0-9]{32}\b"),              # Twilio Account SID
    re.compile(r"\bSK[a-f0-9]{32}\b"),              # Twilio API Key SID
    re.compile(r"\bkey-[A-Za-z0-9]{30,}\b"),        # Mailgun (legacy hex-style key)
    # AWS *secret* (distinct from AKIA access key ID). 40 chars of
    # base64url + `/` and `+`. Anchored to "AWS_SECRET" / "aws_secret" /
    # "secret_access_key" tokens to avoid false positives on innocent
    # 40-char strings.
    re.compile(
        r"(?i)aws[_\- ]?(?:secret|secret[_\- ]?access[_\- ]?key)\s*[=:]\s*['\"]?"
        r"([A-Za-z0-9+/]{40})['\"]?"
    ),
)

# Long base64 blobs in instructions are a classic encoded-payload smell. 200
# chars + is well past anything legitimate (URLs, hashes, short examples).
_BASE64_RE = re.compile(r"[A-Za-z0-9+/=]{200,}")

_AZTEA_INTERNAL_PATH_RE = re.compile(
    r"/(wallet|payments|admin|ops|auth)/", re.IGNORECASE
)


_API_KEY_PREFIX_PREVIEW_CHARS = 8


def _scan_prompt_injection(skill_md: str) -> list[VerificationFinding]:
    """Pure: every prompt-injection phrase that matches in ``skill_md``.

    Runs the matcher against the canonical form AND the reversed canonical
    form. The reversal catches the RLO (U+202E) class of attack where the
    phrase is spelled backwards in the source so it renders forwards on
    screen. ``_normalize_for_phrase_scan`` strips the bidi override
    character itself but leaves the byte order alone; checking the reverse
    is the cheapest way to recover the intended phrase.
    """
    canonical = _normalize_for_phrase_scan(skill_md)
    reversed_canonical = canonical[::-1]
    out: list[VerificationFinding] = []
    for phrase, pattern in zip(_PROMPT_INJECTION_PHRASES, _PROMPT_INJECTION_PATTERNS):
        if pattern.search(canonical) or pattern.search(reversed_canonical):
            out.append(VerificationFinding(
                code="skill.prompt_injection",
                level=LEVEL_BLOCK,
                message=(
                    f"SKILL.md contains a prompt-injection phrase "
                    f"('{phrase}'). Refusing to publish."
                ),
                detail={"phrase": phrase},
            ))
    return out


# Targeted "split across whitespace" rejoiner. Matches when one of the
# real key prefixes is followed by whitespace and then an alphanumeric
# run that, glued back together, forms a key. This is much narrower than
# whitespace-stripping the whole document — which produced false
# positives on legitimate prose like "sk- which means selectorless key".
_KEY_PREFIXES_TO_REJOIN: tuple[str, ...] = (
    "sk-", "sk-proj-", "sk-svcacct-", "sk-admin-", "sk-ant-",
    "gsk_", "azk_", "azac_", "az_", "AIza", "hf_",
    "ghp_", "github_pat_", "AKIA",
    "sk_live_", "sk_test_", "rk_live_", "rk_test_",
    "SG.", "key-",
)


def _split_key_pattern() -> re.Pattern[str]:
    # Form: <prefix><whitespace><20+ alnum>
    prefixes = "|".join(re.escape(p) for p in _KEY_PREFIXES_TO_REJOIN)
    return re.compile(rf"\b(?:{prefixes})\s+[A-Za-z0-9][A-Za-z0-9_\-/+=.]{{19,}}")


_SPLIT_KEY_RE = _split_key_pattern()


def _scan_embedded_api_key(skill_md: str) -> VerificationFinding | None:
    """Pure: first embedded-key match.

    Two passes:
      1. Standard regex over the document as-written.
      2. Targeted "prefix + whitespace + long alnum run" rejoiner so a
         key split across a newline (e.g. ``sk-\\nABCDEFGH…``) is caught
         without also matching legitimate prose that happens to contain
         the prefix followed by a long sentence.
    """
    for pattern in _API_KEY_PATTERNS:
        match = pattern.search(skill_md)
        if match:
            return VerificationFinding(
                code="skill.embedded_api_key",
                level=LEVEL_BLOCK,
                message=(
                    "SKILL.md contains what looks like an embedded API "
                    "key. Remove it and store secrets in caller-supplied "
                    "input or your own backend."
                ),
                detail={"prefix": match.group(0)[:_API_KEY_PREFIX_PREVIEW_CHARS] + "..."},
            )
    split = _SPLIT_KEY_RE.search(skill_md)
    if split:
        return VerificationFinding(
            code="skill.embedded_api_key",
            level=LEVEL_BLOCK,
            message=(
                "SKILL.md appears to split an API key across whitespace. "
                "Remove it and store secrets in caller-supplied input."
            ),
            detail={"prefix": split.group(0)[:_API_KEY_PREFIX_PREVIEW_CHARS] + "..."},
        )
    # Mid-key whitespace split: ``sk-AAAA…<whitespace>…AAAA``. We look for
    # a prefix followed by short alnum runs separated by ≤2 whitespace
    # chars, and only fire when the total alnum content is ≥ 20 chars.
    # This catches the canonical bypass without matching loose prose.
    for prefix in _KEY_PREFIXES_TO_REJOIN:
        idx = 0
        while True:
            start = skill_md.find(prefix, idx)
            if start < 0:
                break
            # Greedy-grab the alnum/separator run that follows.
            cursor = start + len(prefix)
            collected = ""
            ws_runs = 0
            while cursor < len(skill_md):
                ch = skill_md[cursor]
                if ch.isalnum() or ch in "_-/+=.":
                    collected += ch
                elif ch.isspace() and ws_runs < 2:
                    # Allow up to two short whitespace separators.
                    ws_runs += 1
                    while cursor < len(skill_md) and skill_md[cursor].isspace():
                        cursor += 1
                    continue
                else:
                    break
                cursor += 1
            if len(collected) >= 20:
                return VerificationFinding(
                    code="skill.embedded_api_key",
                    level=LEVEL_BLOCK,
                    message=(
                        "SKILL.md appears to embed an API key, possibly split "
                        "across whitespace. Remove it and store secrets in "
                        "caller-supplied input."
                    ),
                    detail={"prefix": (prefix + collected)[:_API_KEY_PREFIX_PREVIEW_CHARS] + "..."},
                )
            idx = start + 1
    return None


def _scan_base64_blob(skill_md: str) -> VerificationFinding | None:
    """Pure: warn on long base64-shaped blobs (common exfiltration pattern)."""
    blob = _BASE64_RE.search(skill_md)
    if not blob:
        return None
    return VerificationFinding(
        code="skill.base64_blob",
        level=LEVEL_WARN,
        message=(
            "SKILL.md contains a >200-char base64-shaped blob. "
            "Encoded payloads in prompts are a common exfiltration "
            "pattern; if this is a hash or example, ignore."
        ),
        detail={"length": len(blob.group(0))},
    )


def _scan_internal_path(skill_md: str) -> VerificationFinding | None:
    """Pure: refuse references to Aztea-internal paths.

    Why: skills should not instruct the model to call platform endpoints
    (/wallet, /payments, /admin, /ops, /auth) directly. Pre-2026-05-22
    this was WARN; bumped to BLOCK because no legitimate listing has a
    reason to talk to /wallet or /admin, and a planted reference is
    indistinguishable from an injection attempt.
    """
    if not _AZTEA_INTERNAL_PATH_RE.search(skill_md):
        return None
    return VerificationFinding(
        code="skill.references_internal_path",
        level=LEVEL_BLOCK,
        message=(
            "SKILL.md references an Aztea-internal path "
            "(/wallet, /payments, /admin, /ops, /auth). Skills "
            "must not instruct the model to call platform "
            "endpoints directly."
        ),
    )


def scan_skill_md(skill_md: str) -> list[VerificationFinding]:
    """Pure: scan a SKILL.md body for prompt-injection / exfiltration markers.

    Why: the skill body is interpreted by an LLM at call time, so anything
    in here runs with the agent's privilege — we treat it as code.
    """
    if not isinstance(skill_md, str):
        raise TypeError("skill_md must be a str")
    findings: list[VerificationFinding] = []
    findings.extend(_scan_prompt_injection(skill_md))
    api_key = _scan_embedded_api_key(skill_md)
    if api_key is not None:
        findings.append(api_key)
    blob = _scan_base64_blob(skill_md)
    if blob is not None:
        findings.append(blob)
    internal = _scan_internal_path(skill_md)
    if internal is not None:
        findings.append(internal)
    return findings


# ---------------------------------------------------------------------------
# Stage 2b — Python handler AST scanner
# ---------------------------------------------------------------------------

_BLOCKED_IMPORTS: frozenset[str] = frozenset(
    {
        "subprocess",
        "pty",
        "ctypes",
        "_ctypes",
        "socket",
        "ssl",  # paired with raw socket; standalone usage rarely needed in handlers
        "telnetlib",
        "ftplib",
        "smtplib",
        "pickle",
        "shelve",
        "marshal",
    }
)

_BLOCKED_BUILTINS: frozenset[str] = frozenset(
    {"eval", "exec", "compile", "__import__", "globals", "vars", "breakpoint"}
)

# os.system, os.popen, os.execv, os.exec*, os.spawn* — anything that shells out.
_BLOCKED_OS_ATTRS: frozenset[str] = frozenset(
    {"system", "popen", "execv", "execvp", "execve", "execvpe", "spawnv", "spawnvp"}
)


def _check_import_node(
    node: ast.Import | ast.ImportFrom,
) -> list[VerificationFinding]:
    """Pure: emit one BLOCK finding per disallowed module referenced by an import."""
    out: list[VerificationFinding] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            root = (alias.name or "").split(".")[0]
            if root in _BLOCKED_IMPORTS:
                out.append(VerificationFinding(
                    code="python.blocked_import",
                    level=LEVEL_BLOCK,
                    message=(
                        f"Python handler imports '{alias.name}', "
                        "which is not allowed for in-process listings."
                    ),
                    detail={"module": alias.name, "line": node.lineno},
                ))
        return out
    root = (node.module or "").split(".")[0]
    if root in _BLOCKED_IMPORTS:
        out.append(VerificationFinding(
            code="python.blocked_import",
            level=LEVEL_BLOCK,
            message=(
                f"Python handler imports from '{node.module}', "
                "which is not allowed for in-process listings."
            ),
            detail={"module": node.module, "line": node.lineno},
        ))
    return out


def _fold_str_const(node: ast.AST) -> str | None:
    """Pure: best-effort fold of a small AST expression to a string literal.

    Covers:
      - ``ast.Constant("ex")`` → "ex"
      - ``ast.BinOp(Add, "ex", "ec")`` → "exec"  (chained recursively)
      - ``ast.JoinedStr`` with only Constant Str parts → the joined text

    Returns None for anything more complex. We deliberately do not run
    real evaluation; the goal is to catch the obvious bypasses (string
    concat / f-string of literals) without growing a full sandboxed
    evaluator. Anything sneakier than that is still rejected by the
    other checks (blocked-builtin getattr is itself a generic warning
    surface in a future patch).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_str_const(node.left)
        right = _fold_str_const(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                return None
        return "".join(parts)
    return None


def _is_dynamic_blocked_import(call: ast.Call) -> str | None:
    """Pure: ``importlib.import_module('blocked')`` target string, or None.

    Uses ``_fold_str_const`` so an attacker can't bypass the check by
    splitting the argument across a concat: ``importlib.import_module("sub"
    + "process")`` is folded and recognised.
    """
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "importlib"
        and func.attr == "import_module"
        and call.args
    ):
        return None
    target = _fold_str_const(call.args[0])
    if target is None:
        return None
    return target if target.split(".")[0] in _BLOCKED_IMPORTS else None


def _getattr_reflection_target(call: ast.Call) -> str | None:
    """Pure: ``getattr(o, 'ex'+'ec')`` → ``'exec'`` when it folds to a blocked name.

    Returns the name iff:
      - ``call`` is a ``getattr(...)`` Call,
      - the second arg folds to a string literal,
      - that literal is in ``_BLOCKED_BUILTINS`` or ``_BLOCKED_OS_ATTRS``.

    This catches the reflection-bypass class: ``getattr(__builtins__,
    "ex" + "ec")`` or ``getattr(os, "syst" + "em")``.
    """
    func = call.func
    if not (
        isinstance(func, ast.Name)
        and func.id == "getattr"
        and len(call.args) >= 2
    ):
        return None
    target = _fold_str_const(call.args[1])
    if target is None:
        return None
    if target in _BLOCKED_BUILTINS or target in _BLOCKED_OS_ATTRS:
        return target
    return None


def _is_subclass_walk(call: ast.Call) -> bool:
    """Pure: detect ``().__class__.__bases__[0].__subclasses__()`` reach.

    The attacker pattern is to climb the type hierarchy via ``__class__``
    + ``__bases__`` + ``__subclasses__`` to land on ``Popen`` or another
    privileged class. Any expression containing a ``__subclasses__`` call
    on something whose chain references ``__bases__`` is suspicious by
    construction — no legitimate handler needs that walk.
    """
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "__subclasses__"):
        return False
    # Walk up the attribute chain; bail when we see __bases__ or __class__.
    node: Any = func.value
    depth = 0
    while isinstance(node, (ast.Attribute, ast.Subscript)) and depth < 6:
        if isinstance(node, ast.Attribute) and node.attr in {"__bases__", "__mro__", "__class__"}:
            return True
        if isinstance(node, ast.Subscript):
            node = node.value
        else:
            node = node.value
        depth += 1
    return False


def _is_blocked_os_call(func: Any) -> bool:
    """Pure: True for ``os.<blocked>`` attribute calls (system / popen / exec*)."""
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
        and func.attr in _BLOCKED_OS_ATTRS
    )


def _check_call_node(call: ast.Call) -> list[VerificationFinding]:
    """Pure: BLOCK findings for risky calls — exec/eval, importlib, os.*, reflection."""
    func = call.func
    if isinstance(func, ast.Name) and func.id in _BLOCKED_BUILTINS:
        return [VerificationFinding(
            code="python.blocked_builtin",
            level=LEVEL_BLOCK,
            message=(
                f"Python handler calls '{func.id}(...)', which is "
                "not allowed for in-process listings."
            ),
            detail={"name": func.id, "line": call.lineno},
        )]
    target = _is_dynamic_blocked_import(call)
    if target:
        return [VerificationFinding(
            code="python.blocked_import",
            level=LEVEL_BLOCK,
            message=(
                f"Python handler dynamically imports '{target}' "
                "via importlib.import_module, which is not allowed "
                "for in-process listings."
            ),
            detail={"module": target, "line": call.lineno},
        )]
    if _is_blocked_os_call(func):
        return [VerificationFinding(
            code="python.blocked_os_call",
            level=LEVEL_BLOCK,
            message=f"Python handler calls 'os.{func.attr}(...)'.",
            detail={"attr": func.attr, "line": call.lineno},
        )]
    reflected = _getattr_reflection_target(call)
    if reflected is not None:
        return [VerificationFinding(
            code="python.reflection_bypass",
            level=LEVEL_BLOCK,
            message=(
                f"Python handler reaches a blocked name ('{reflected}') via "
                "getattr() with a folded-literal attribute name. Reflection "
                "is not a defence."
            ),
            detail={"name": reflected, "line": call.lineno},
        )]
    if _is_subclass_walk(call):
        return [VerificationFinding(
            code="python.subclass_walk",
            level=LEVEL_BLOCK,
            message=(
                "Python handler walks the type hierarchy via "
                "__bases__/__mro__/__subclasses__ — a classic sandbox-"
                "escape pattern."
            ),
            detail={"line": call.lineno},
        )]
    return []


def _has_handler_definition(tree: ast.Module) -> bool:
    """Pure: True when the module defines a top-level ``handler`` (def or assignment)."""
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "handler":
            return True
        if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "handler" for t in n.targets
        ):
            return True
    return False


def scan_python_handler(source: str) -> list[VerificationFinding]:
    """Pure: AST-walk a Python module for risky imports / calls.

    Why: handlers don't need shells, raw sockets, or eval; an author with a
    legitimate need can host their own HTTP endpoint and skip the in-line
    publish flow that auto-runs ``handler()`` under our worker.
    """
    if not isinstance(source, str):
        raise TypeError("source must be a str")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [VerificationFinding(
            code="python.syntax_error",
            level=LEVEL_BLOCK,
            message=f"Python file did not parse: {exc.msg} (line {exc.lineno}).",
            detail={"line": exc.lineno, "offset": exc.offset},
        )]
    findings: list[VerificationFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            findings.extend(_check_import_node(node))
        elif isinstance(node, ast.Call):
            findings.extend(_check_call_node(node))
    if not _has_handler_definition(tree):
        findings.append(VerificationFinding(
            code="python.no_handler",
            level=LEVEL_WARN,
            message=(
                "Python file does not define a top-level `handler(payload)` "
                "function. The CLI cannot auto-run it; you'll need to wire "
                "up `aztea.AgentServer` manually."
            ),
        ))
    return findings


# ---------------------------------------------------------------------------
# Stage 2c — agent.md / endpoint URL hygiene
# ---------------------------------------------------------------------------

# Hosts an agent.md endpoint should not point at: aztea's own production
# surfaces would re-list a built-in under a third-party owner. We
# deliberately scope this to aztea.ai only — staging hosts (aztea.dev) and
# loopback aliases are legitimate test-fixture targets and are filtered out
# by the SSRF check in core/url_security.py instead.
_AZTEA_OWN_HOST_SUFFIXES: tuple[str, ...] = ("aztea.ai",)

# Common Cyrillic look-alikes that visually impersonate Latin letters used in
# our own host name. NFKC does NOT fold these; we apply this map explicitly so
# `aztеa.ai` (Cyrillic 'е') resolves to `aztea.ai` for comparison purposes.
_HOMOGLYPH_FOLD = str.maketrans({
    "а": "a", "А": "A",
    "е": "e", "Е": "E",
    "о": "o", "О": "O",
    "р": "p", "Р": "P",
    "с": "c", "С": "C",
    "у": "y", "У": "Y",
    "х": "x", "Х": "X",
    "ѕ": "s", "Ѕ": "S",
    "і": "i", "І": "I",
    "ј": "j", "Ј": "J",
    "ԛ": "q", "Ԛ": "Q",
})


def _decode_idn_host_in_url(url: str) -> str:
    """Best-effort: replace any xn--… host label with its IDN-decoded form.

    Why: a publisher can register ``https://xn--zte-3oa.ai/run`` which
    decodes to ``https://azteа.ai/run`` (Cyrillic 'а'). The
    _HOMOGLYPH_FOLD only fires after the host is decoded; if we leave the
    xn-- form alone the fold has nothing to fold.

    Returns the input unchanged when the URL has no host or the labels
    don't decode.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:  # noqa: BLE001
        return url
    host = parsed.hostname
    if not host or "xn--" not in host:
        return url
    decoded_labels: list[str] = []
    for label in host.split("."):
        if label.startswith("xn--"):
            try:
                decoded_labels.append(label.encode("ascii").decode("idna"))
            except (UnicodeError, UnicodeDecodeError):
                decoded_labels.append(label)
        else:
            decoded_labels.append(label)
    decoded_host = ".".join(decoded_labels)
    if decoded_host == host:
        return url
    # Rebuild the URL with the decoded host. We must preserve userinfo
    # and port if present.
    netloc = parsed.netloc
    # Replace the host portion (case-insensitive) while keeping
    # userinfo / port intact.
    new_netloc = netloc.replace(host, decoded_host, 1)
    return urllib.parse.urlunsplit(parsed._replace(netloc=new_netloc))


def _candidate_endpoint_forms(raw: str) -> set[str]:
    """Return the set of canonical forms the suffix check should run against.

    Defence-in-depth across four canonicalisations:
      1. NFKC-folded + lowered original
      2. percent-decoded version of (1)
      3. (2) with Cyrillic look-alikes folded to ASCII
      4. IDN-decoded form (xn--zte-3oa.ai → azteа.ai → aztea.ai after fold)
    """
    forms: set[str] = set()
    base = unicodedata.normalize("NFKC", raw.strip()).lower()
    forms.add(base)
    try:
        decoded = urllib.parse.unquote(base)
    except Exception:  # noqa: BLE001 — malformed input shouldn't blow up scan
        decoded = base
    forms.add(decoded)
    forms.add(decoded.translate(_HOMOGLYPH_FOLD))
    # IDN decode runs on the raw input (urlsplit needs the original scheme)
    # then percent-decodes and homoglyph-folds the result.
    idn_decoded = _decode_idn_host_in_url(raw.strip()).lower()
    forms.add(idn_decoded)
    try:
        idn_pct = urllib.parse.unquote(idn_decoded)
    except Exception:  # noqa: BLE001
        idn_pct = idn_decoded
    forms.add(idn_pct.translate(_HOMOGLYPH_FOLD))
    return forms


def scan_agent_md_endpoint(endpoint_url: str) -> list[VerificationFinding]:
    """Endpoint-URL sanity above and beyond the SSRF check.

    SSRF / private-IP enforcement is in core.url_security; here we only catch
    the "you registered against aztea.ai itself" footgun, including
    percent-encoded and Cyrillic-homoglyph bypass attempts.
    """
    if not isinstance(endpoint_url, str):
        raise TypeError("endpoint_url must be a str")
    findings: list[VerificationFinding] = []
    if not endpoint_url.strip():
        return findings
    candidates = _candidate_endpoint_forms(endpoint_url)
    for suffix in _AZTEA_OWN_HOST_SUFFIXES:
        for candidate in candidates:
            if (
                f"://{suffix}" in candidate
                or f".{suffix}/" in candidate
                or candidate.endswith(suffix)
            ):
                findings.append(
                    VerificationFinding(
                        code="manifest.endpoint_is_aztea",
                        level=LEVEL_BLOCK,
                        message=(
                            "endpoint_url points at an Aztea-owned host. Third-"
                            "party agents must host their own endpoint."
                        ),
                        detail={"host_suffix": suffix},
                    )
                )
                return findings
    return findings


# ---------------------------------------------------------------------------
# Stage 2d — descriptive clone detection
# ---------------------------------------------------------------------------

# We deliberately keep this dependency-light so it can run on the CLI without
# loading the embedding model. Embedding-cosine clone detection is layered on
# top by the caller when they have an embedding backend available; this
# function provides a fast string-similarity fallback.

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalize_for_clone_scan(text: str) -> str:
    """Lowercase + homoglyph-fold + zero-width-strip before tokenising.

    Without this, ``scan_clone_against`` tokenises ``Cоde Review``
    (Cyrillic 'о') as ['c', 'de', 'review'] which has Jaccard 0% with
    ['code', 'review']. After folding, the Cyrillic 'о' becomes Latin
    'o' and the tokeniser produces ['code', 'review'] — clone caught.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    no_invisible = _ZERO_WIDTH_RE.sub("", normalized)
    folded = no_invisible.translate(_PHRASE_HOMOGLYPH_FOLD)
    return folded.lower()


def _shingles(text: str, n: int = 2) -> set[tuple[str, ...]]:
    """Default to bigrams: agent names are 2-4 words, so trigrams are sparse."""
    words = _WORD_RE.findall(_normalize_for_clone_scan(text))
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def jaccard_similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def _format_clone_finding(
    e_name: str, name_sim: float, desc_sim: float,
) -> VerificationFinding:
    """Pure: shape one near-duplicate match into a WARN finding."""
    return VerificationFinding(
        code="listing.near_duplicate",
        level=LEVEL_WARN,
        message=(
            f"Listing closely resembles existing agent "
            f"'{e_name}' (name {name_sim:.0%}, "
            f"description {desc_sim:.0%})."
        ),
        detail={
            "matched_name": e_name,
            "name_similarity": round(name_sim, 3),
            "description_similarity": round(desc_sim, 3),
        },
    )


def scan_clone_against(
    candidate_name: str,
    candidate_description: str,
    existing: Iterable[dict[str, Any]],
    *,
    name_threshold: float = 0.5,
    description_threshold: float = 0.5,
) -> list[VerificationFinding]:
    """Pure: compare candidate listing text against existing curated entries.

    Why: clone detection is signal, not gospel — only WARN findings, and we
    stop at the first match so the CLI receipt isn't overwhelmed by 50
    near-builtins.
    """
    cand_name = (candidate_name or "").strip()
    cand_desc = (candidate_description or "").strip()
    for entry in existing or ():
        e_name = str(entry.get("name") or "").strip()
        e_desc = str(entry.get("description") or "").strip()
        if not e_name and not e_desc:
            continue
        name_sim = jaccard_similarity(cand_name, e_name)
        desc_sim = jaccard_similarity(cand_desc, e_desc)
        if name_sim >= name_threshold or desc_sim >= description_threshold:
            return [_format_clone_finding(e_name, name_sim, desc_sim)]
    return []


# ---------------------------------------------------------------------------
# Stage 3 — synthetic + adversarial endpoint probe (re-exported from
# ``core.listing_safety_probe`` to keep this file under the 1000-line CI cap)
# ---------------------------------------------------------------------------
from core.listing_safety_probe import (  # noqa: E402  (re-export)
    adversarial_probes,
    evaluate_probe_response,
    synthesize_input_from_schema,
)


__all__ = [
    "LEVEL_BLOCK",
    "LEVEL_INFO",
    "LEVEL_WARN",
    "VerificationFinding",
    "adversarial_probes",
    "evaluate_probe_response",
    "has_block",
    "has_warn",
    "jaccard_similarity",
    "scan_agent_md_endpoint",
    "scan_clone_against",
    "scan_python_handler",
    "scan_skill_md",
    "synthesize_input_from_schema",
]
