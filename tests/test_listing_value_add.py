"""Thin-wrapper AST heuristic: flags pass-throughs, spares real logic + boilerplate."""
from __future__ import annotations

from core.listing_value_add import CODE_THIN_WRAPPER, assess_thin_wrapper

_THIN = """\
import requests
def handler(payload):
    return requests.get(payload['url']).json()
"""

_SUBSTANTIVE = """\
import requests
def handler(payload):
    r = requests.get(payload['url'])
    data = r.json()
    cleaned = {k: str(v).strip() for k, v in data.items()}
    total = sum(1 for v in cleaned.values() if v)
    return {'cleaned': cleaned, 'total': total}
"""

_BOILERPLATE_REAL_LOGIC = """\
from aztea import AgentServer, handler

server = AgentServer()

@handler
def handler(payload):
    text = payload.get('text', '')
    words = text.split()
    return {'word_count': len(words), 'first': words[0] if words else None}
"""

_STDLIB_ONLY = """\
import json
def handler(payload):
    return json.dumps(payload)
"""


def test_thin_wrapper_is_flagged():
    findings = assess_thin_wrapper(_THIN)
    assert [f.code for f in findings] == [CODE_THIN_WRAPPER]
    assert findings[0].level == "warn"
    assert "requests" in findings[0].detail["libraries"]


def test_substantive_handler_not_flagged():
    assert assess_thin_wrapper(_SUBSTANTIVE) == []


def test_agentserver_boilerplate_with_real_logic_not_flagged():
    assert assess_thin_wrapper(_BOILERPLATE_REAL_LOGIC) == []


def test_stdlib_only_pass_through_not_flagged():
    # A wrapper around a stdlib module is not the "could pip-install it" concern.
    assert assess_thin_wrapper(_STDLIB_ONLY) == []


def test_unparseable_source_is_silent():
    assert assess_thin_wrapper("def handler(payload):\n    return (") == []


def test_empty_source_is_silent():
    assert assess_thin_wrapper("") == []
