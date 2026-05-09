"""L11 — fuzz / invariant tests for listing safety scanners.

Uses a seeded `random.Random` rather than `hypothesis` so we don't add a
dependency. Same invariants:

  - crash-free: no scanner ever raises on any str input.
  - deterministic: same input → same finding list.
  - code-set bounded: scanners only emit known codes.
  - whitespace-stable: leading/trailing whitespace doesn't change findings.
"""
from __future__ import annotations

import random
import string

from core.listing_safety import (
    LEVEL_BLOCK,
    LEVEL_INFO,
    LEVEL_WARN,
    scan_agent_md_endpoint,
    scan_python_handler,
    scan_skill_md,
)

_FUZZ_ROUNDS = 500
_RNG_SEED = 0xA27EA  # stable seed → reproducible CI

_SKILL_CODES = {
    "skill.prompt_injection",
    "skill.embedded_api_key",
    "skill.base64_blob",
    "skill.references_internal_path",
}
_PY_CODES = {
    "python.syntax_error",
    "python.blocked_import",
    "python.blocked_builtin",
    "python.blocked_os_call",
    "python.no_handler",
}
_ENDPOINT_CODES = {"manifest.endpoint_is_aztea"}

_LEVELS = {LEVEL_INFO, LEVEL_WARN, LEVEL_BLOCK}

_PRINTABLE_POOL = string.ascii_letters + string.digits + string.punctuation + " \n\t"
_TOKEN_POOL = [
    "ignore",
    "previous",
    "instructions",
    "system",
    "prompt",
    "sk-",
    "azk_",
    "AKIA",
    "subprocess",
    "os.system",
    "eval",
    "import",
    "def handler(p):",
    "https://aztea.ai/x",
    "https://my.host/x",
    "wallet",
    "/wallet/",
    "task",
    "hello",
    " " * 10,
    "\n",
    "\r\n",
    "AAAAAAAAAAAAAAAAAAAAAAAA",
    "<!--",
    "-->",
    "{",
    "}",
    "---",
    "name:",
    "description:",
]


def _random_blob(rng: random.Random, max_len: int = 2048) -> str:
    """Mix random chars with token sprinkles to maximize finding-trigger surface."""
    n = rng.randint(0, max_len)
    parts: list[str] = []
    while sum(len(p) for p in parts) < n:
        if rng.random() < 0.25:
            parts.append(rng.choice(_TOKEN_POOL))
        else:
            chunk = "".join(rng.choices(_PRINTABLE_POOL, k=rng.randint(1, 32)))
            parts.append(chunk)
    return "".join(parts)[:max_len]


def _random_endpoint(rng: random.Random) -> str:
    schemes = ["http://", "https://", "ftp://", ""]
    hosts = [
        "aztea.ai",
        "api.aztea.ai",
        "evil.example",
        "my.host",
        "AzTeA.AI",
        "aztea.dev",
        "AZTEA.AI/foo",
        "aztea.ai.evil.com",
        "127.0.0.1",
        "localhost",
        "example.org",
    ]
    paths = ["", "/x", "/agents/y", "/registry"]
    return rng.choice(schemes) + rng.choice(hosts) + rng.choice(paths)


# ---------------------------------------------------------------------------
# Crash-free invariant
# ---------------------------------------------------------------------------


def test_scan_skill_md_never_crashes():
    rng = random.Random(_RNG_SEED)
    for _ in range(_FUZZ_ROUNDS):
        body = _random_blob(rng)
        findings = scan_skill_md(body)
        assert isinstance(findings, list)


def test_scan_python_handler_never_crashes():
    rng = random.Random(_RNG_SEED + 1)
    for _ in range(_FUZZ_ROUNDS):
        src = _random_blob(rng, max_len=512)
        findings = scan_python_handler(src)
        assert isinstance(findings, list)


def test_scan_agent_md_endpoint_never_crashes():
    rng = random.Random(_RNG_SEED + 2)
    for _ in range(_FUZZ_ROUNDS):
        url = _random_endpoint(rng)
        findings = scan_agent_md_endpoint(url)
        assert isinstance(findings, list)


# ---------------------------------------------------------------------------
# Determinism invariant
# ---------------------------------------------------------------------------


def test_scan_skill_md_is_deterministic():
    rng = random.Random(_RNG_SEED + 10)
    for _ in range(100):
        body = _random_blob(rng)
        a = scan_skill_md(body)
        b = scan_skill_md(body)
        assert [(f.code, f.level, f.message) for f in a] == [
            (f.code, f.level, f.message) for f in b
        ]


def test_scan_python_handler_is_deterministic():
    rng = random.Random(_RNG_SEED + 11)
    for _ in range(100):
        src = _random_blob(rng, max_len=512)
        a = scan_python_handler(src)
        b = scan_python_handler(src)
        assert [(f.code, f.level) for f in a] == [(f.code, f.level) for f in b]


# ---------------------------------------------------------------------------
# Code-set invariant — only known codes leak through
# ---------------------------------------------------------------------------


def test_scan_skill_md_emits_only_known_codes():
    rng = random.Random(_RNG_SEED + 20)
    for _ in range(_FUZZ_ROUNDS):
        body = _random_blob(rng)
        findings = scan_skill_md(body)
        for f in findings:
            assert f.level in _LEVELS
            assert f.code in _SKILL_CODES, f"unexpected SKILL.md code: {f.code}"


def test_scan_python_handler_emits_only_known_codes():
    rng = random.Random(_RNG_SEED + 21)
    for _ in range(_FUZZ_ROUNDS):
        src = _random_blob(rng, max_len=512)
        findings = scan_python_handler(src)
        for f in findings:
            assert f.level in _LEVELS
            assert f.code in _PY_CODES, f"unexpected python code: {f.code}"


def test_scan_agent_md_endpoint_emits_only_known_codes():
    rng = random.Random(_RNG_SEED + 22)
    for _ in range(_FUZZ_ROUNDS):
        url = _random_endpoint(rng)
        findings = scan_agent_md_endpoint(url)
        for f in findings:
            assert f.level in _LEVELS
            assert f.code in _ENDPOINT_CODES


# ---------------------------------------------------------------------------
# Whitespace-stability invariant on SKILL.md scanner
# ---------------------------------------------------------------------------


def test_scan_skill_md_unchanged_under_outer_whitespace():
    rng = random.Random(_RNG_SEED + 30)
    for _ in range(200):
        body = _random_blob(rng)
        padded = "   \n\n" + body + "\n\n   "
        a = {(f.code, f.level) for f in scan_skill_md(body)}
        b = {(f.code, f.level) for f in scan_skill_md(padded)}
        assert a == b
