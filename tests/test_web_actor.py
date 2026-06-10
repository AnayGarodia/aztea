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
    # The "disabled by default" tests must not inherit an operator's .env: any other
    # collected test that imports the server app runs load_dotenv(), and a dev box
    # with AZTEA_ACTION_WEB_ENABLED=1 would otherwise fail them spuriously.
    monkeypatch.delenv("AZTEA_ACTION_WEB_ENABLED", raising=False)
    monkeypatch.delenv("AZTEA_ACTION_WEB_COMMIT_ENABLED", raising=False)
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

    # Resolver surface (mirrors the Playwright Locator API _resolve_locator uses).
    @property
    def first(self):
        return self

    def filter(self, **_):
        return self

    def count(self):
        return 1

    def is_visible(self):
        return True

    def scroll_into_view_if_needed(self, **_):
        pass

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

    # _resolve_locator tries accessible-name + attribute strategies; the fake resolves
    # on the first (get_by_role) and records the action against the passed target.
    def get_by_role(self, _role, name="", **_):
        return _FakeLocator(self._rec, name)

    def get_by_placeholder(self, target, **_):
        return _FakeLocator(self._rec, target)

    def get_by_title(self, target, **_):
        return _FakeLocator(self._rec, target)

    def get_by_alt_text(self, target, **_):
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


# --------------------------------------------------------------------------- dry_run (rehearsal)
def test_dry_run_returns_planned_and_revealed_without_consuming(wa):
    wa.setenv("AZTEA_ACTION_WEB_ENABLED", "1")
    rec: list = []
    wa.setattr(web_actor, "_import_playwright", lambda: (lambda: _FakeSyncPlaywright(rec)))
    m = _mandate()
    am.authorize_mandate(m["mandate_id"], m["confirmation_nonce"])
    out = web_actor.run({"action": "dry_run", "url": "https://shop.example.com/cart",
                         "mandate_id": m["mandate_id"],
                         "steps": [{"action": "click", "target": "Add to cart"}]})
    assert out["phase"] == "dry_run"
    assert out["planned"]["action_kind"] == "purchase"
    assert out["revealed"]["phase"] == "interacted"
    # A rehearsal must NOT consume the mandate.
    assert am.get_mandate(m["mandate_id"])["status"] == "authorized"


def test_dry_run_enforces_domain_binding(wa):
    wa.setenv("AZTEA_ACTION_WEB_ENABLED", "1")
    m = _mandate()  # allowed_domains = ["shop.example.com"]
    out = web_actor.run({"action": "dry_run", "url": "https://evil.example.net/x",
                         "mandate_id": m["mandate_id"]})
    assert out["error"]["code"] == "web_actor.domain_not_allowed"


def test_dry_run_credential_gated_off_by_default(wa):
    wa.setenv("AZTEA_ACTION_WEB_ENABLED", "1")  # injection flag intentionally NOT set
    rec: list = []
    wa.setattr(web_actor, "_import_playwright", lambda: (lambda: _FakeSyncPlaywright(rec)))
    m = _mandate()
    out = web_actor.run({"action": "dry_run", "url": "https://shop.example.com/cart",
                         "mandate_id": m["mandate_id"], "use_credential": "password"})
    assert out["error"]["code"] == "web_actor.credential_unavailable"


def test_dry_run_with_login_injects_vault_credential_when_enabled(wa, tmp_path):
    import base64
    import os

    from core import credential_vault as cv
    from core.migrate import apply_migrations

    vdb = str(tmp_path / "vault.db")
    apply_migrations(vdb)
    wa.setattr(cv, "DB_PATH", vdb)
    wa.setenv("AZTEA_ACTION_WEB_ENABLED", "1")
    wa.setenv("AZTEA_CREDENTIAL_VAULT_ENABLED", "1")
    wa.setenv("AZTEA_CREDENTIAL_INJECTION_ENABLED", "1")
    wa.setenv("AZTEA_VAULT_ALLOW_LOCAL_KEK", "1")
    wa.setenv("AZTEA_VAULT_LOCAL_KEK", base64.b64encode(os.urandom(32)).decode())
    # _mandate() owner is "c"; the credential is scoped to the mandate owner + domain.
    cv.store_credential(owner_id="c", domain="shop.example.com", cred_kind="password",
                        secret={"username": "alice", "password": "hunter2"})
    rec: list = []
    wa.setattr(web_actor, "_import_playwright", lambda: (lambda: _FakeSyncPlaywright(rec)))
    m = _mandate()
    out = web_actor.run({"action": "dry_run", "url": "https://shop.example.com/cart",
                         "mandate_id": m["mandate_id"], "use_credential": "password"})
    assert out["phase"] == "dry_run"
    # The vault secret (not a caller-supplied value) was filled into the login form.
    fills = {r[2] for r in rec if r[0] == "fill"}
    assert "alice" in fills and "hunter2" in fills
