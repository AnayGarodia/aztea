"""web_actor fail-closed gating tests (Phase 4).

Default OFF; preview needs the master flag; commit needs both flags AND an
authorized mandate; commit consumes the mandate (replay fails).
"""

from __future__ import annotations

import pytest

from agents import web_actor
from core import action_mandates as am


@pytest.fixture()
def wa(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "DB_PATH", str(tmp_path / "mandates.db"))
    am.init_action_mandates_db()
    return monkeypatch


def _mandate():
    return am.create_mandate(
        caller_owner_id="c", agent_id="a", action_kind="purchase",
        reversibility="reversible", max_spend_cents=500,
        allowed_domains=["shop.example.com"], action_descriptor={"sku": "X"},
    )


def test_disabled_by_default(wa):
    out = web_actor.run({"action": "preview", "mandate_id": "anything"})
    assert out["error"]["code"] == "web_actor.disabled"


def test_preview_when_master_enabled(wa):
    wa.setenv("AZTEA_ACTION_WEB_ENABLED", "1")
    m = _mandate()
    out = web_actor.run({"action": "preview", "mandate_id": m["mandate_id"]})
    assert out["phase"] == "previewed"
    assert out["confirmation"]["action_kind"] == "purchase"
    assert out["confirmation"]["max_spend_cents"] == 500
    assert out["confirmation"]["allowed_domains"] == ["shop.example.com"]


def test_commit_gated_off_even_when_master_on(wa):
    wa.setenv("AZTEA_ACTION_WEB_ENABLED", "1")  # commit flag intentionally NOT set
    m = _mandate()
    am.authorize_mandate(m["mandate_id"], m["confirmation_nonce"])
    out = web_actor.run({"action": "commit", "url": "https://shop.example.com/checkout",
                         "mandate_id": m["mandate_id"],
                         "confirmation_nonce": m["confirmation_nonce"]})
    assert out["error"]["code"] == "web_actor.commit_disabled"


def test_commit_consumes_authorized_mandate_and_replay_fails(wa):
    wa.setenv("AZTEA_ACTION_WEB_ENABLED", "1")
    wa.setenv("AZTEA_ACTION_WEB_COMMIT_ENABLED", "1")
    m = _mandate()
    am.authorize_mandate(m["mandate_id"], m["confirmation_nonce"])
    out = web_actor.run({"action": "commit", "url": "https://shop.example.com/checkout",
                         "mandate_id": m["mandate_id"],
                         "confirmation_nonce": m["confirmation_nonce"]})
    assert out["phase"] == "committed_validated"
    replay = web_actor.run({"action": "commit", "url": "https://shop.example.com/checkout",
                            "mandate_id": m["mandate_id"],
                            "confirmation_nonce": m["confirmation_nonce"]})
    assert replay["error"]["code"] == "web_actor.not_authorized"  # consumed -> idempotent


def test_commit_rejects_unauthorized_mandate(wa):
    wa.setenv("AZTEA_ACTION_WEB_ENABLED", "1")
    wa.setenv("AZTEA_ACTION_WEB_COMMIT_ENABLED", "1")
    m = _mandate()  # never authorized
    out = web_actor.run({"action": "commit", "url": "https://shop.example.com/checkout",
                         "mandate_id": m["mandate_id"],
                         "confirmation_nonce": m["confirmation_nonce"]})
    assert out["error"]["code"] == "web_actor.not_authorized"


def test_commit_refuses_url_outside_allowed_domains(wa):
    wa.setenv("AZTEA_ACTION_WEB_ENABLED", "1")
    wa.setenv("AZTEA_ACTION_WEB_COMMIT_ENABLED", "1")
    m = _mandate()  # allowed_domains = ["shop.example.com"]
    am.authorize_mandate(m["mandate_id"], m["confirmation_nonce"])
    out = web_actor.run({"action": "commit", "url": "https://evil.example.net/x",
                         "mandate_id": m["mandate_id"], "confirmation_nonce": m["confirmation_nonce"]})
    assert out["error"]["code"] == "web_actor.domain_not_allowed"
    # The domain gate must refuse BEFORE consuming the mandate.
    assert am.get_mandate(m["mandate_id"])["status"] == "authorized"


# --------------------------------------------------------------------------- E1 interact (safe tier)
def test_parse_steps_validates_and_caps():
    from agents import _web_interact as wi
    steps = wi.parse_steps([{"action": "fill", "target": "Search", "value": "x"},
                            {"action": "click", "target": "Go"}, {"action": "scroll"}])
    assert [s["action"] for s in steps] == ["fill", "click", "scroll"]
    with pytest.raises(ValueError):
        wi.parse_steps([])  # empty
    with pytest.raises(ValueError):
        wi.parse_steps([{"action": "navigate", "target": "x"}])  # unknown action
    with pytest.raises(ValueError):
        wi.parse_steps([{"action": "click"}])  # click requires a target
    with pytest.raises(ValueError):
        wi.parse_steps([{"action": "scroll"}] * (wi._MAX_STEPS + 1))  # over the cap


def test_interact_disabled_by_default(wa):
    out = web_actor.run({"action": "interact", "url": "https://example.com/",
                         "steps": [{"action": "scroll"}]})
    assert out["error"]["code"] == "web_actor.disabled"


def test_interact_requires_url_and_valid_steps(wa):
    wa.setenv("AZTEA_ACTION_WEB_ENABLED", "1")
    assert web_actor.run({"action": "interact", "steps": [{"action": "scroll"}]})["error"]["code"] == "web_actor.missing_url"
    bad = web_actor.run({"action": "interact", "url": "https://example.com/", "steps": []})
    assert bad["error"]["code"] == "web_actor.invalid_steps"


# A fake Playwright so the orchestration (step order + result shape) is tested without a browser.
class _FakeLocator:
    def __init__(self, rec, target):
        self._rec, self._target = rec, target

    @property
    def first(self):
        return self

    def click(self, **_):
        self._rec.append(("click", self._target))

    def fill(self, value, **_):
        self._rec.append(("fill", self._target, value))

    def select_option(self, value, **_):
        self._rec.append(("select", self._target, value))

    def inner_text(self):
        return "revealed body text"


class _FakeMouse:
    def __init__(self, rec):
        self._rec = rec

    def wheel(self, _dx, dy):
        self._rec.append(("scroll", dy))


class _FakePage:
    def __init__(self, rec):
        self._rec = rec
        self.url = "https://example.com/after"
        self.mouse = _FakeMouse(rec)

    def goto(self, url, **_):
        self._rec.append(("goto", url))

    def get_by_text(self, target, **_):
        return _FakeLocator(self._rec, target)

    def get_by_label(self, target, **_):
        return _FakeLocator(self._rec, target)

    def locator(self, _sel):
        return _FakeLocator(self._rec, "body")

    def wait_for_timeout(self, _ms):
        pass

    def title(self):
        return "After"


class _FakeContext:
    def __init__(self, rec):
        self._rec = rec
        self.routed = False

    def route(self, _pattern, _guard):
        self.routed = True

    def new_page(self):
        return _FakePage(self._rec)

    def close(self):
        pass


class _FakeBrowser:
    def __init__(self, rec):
        self._rec = rec

    def new_context(self, **_):
        return _FakeContext(self._rec)

    def close(self):
        pass


class _FakePW:
    def __init__(self, rec):
        self.chromium = type("C", (), {"launch": lambda _self, **_k: _FakeBrowser(rec)})()


class _FakeSyncPlaywright:
    def __init__(self, rec):
        self._rec = rec

    def __enter__(self):
        return _FakePW(self._rec)

    def __exit__(self, *_a):
        return False


def test_interact_applies_steps_in_order(wa):
    wa.setenv("AZTEA_ACTION_WEB_ENABLED", "1")
    rec: list = []
    wa.setattr(web_actor, "_import_playwright", lambda: (lambda: _FakeSyncPlaywright(rec)))
    out = web_actor.run({
        "action": "interact", "url": "https://example.com/",
        "steps": [
            {"action": "fill", "target": "Search", "value": "shoes"},
            {"action": "click", "target": "Go"},
            {"action": "scroll"},
        ],
    })
    assert out["phase"] == "interacted" and out["steps_completed"] == 3
    assert out["title"] == "After" and out["text"] == "revealed body text"
    assert [r[0] for r in rec] == ["goto", "fill", "click", "scroll"]
