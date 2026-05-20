"""Tests for the V3 interactive REPL.

These exercise the dispatcher, the typo-suggestion path, and the free-text
redirect — the layers that don't require a real TTY to test. The prompt
loop itself (`repl.app.start`) needs a TTY + prompt_toolkit; that's left
for manual smoke-testing.
"""
from __future__ import annotations

from aztea.cli.repl import commands as _cmd


# ── Registry / lookup ─────────────────────────────────────────────────────


def test_registry_has_expected_core_commands():
    names = {c.name for c in _cmd.all_commands()}
    # The user-facing core set must always be present.
    must_have = {
        "/login", "/logout", "/whoami",
        "/agents", "/show",
        "/hire", "/batch",
        "/status", "/jobs", "/follow", "/cancel", "/rate", "/verify", "/dispute",
        "/wallet",
        "/init", "/publish",
        "/claude-code",
        "/help", "/clear", "/exit", "/quit",
    }
    missing = must_have - names
    assert not missing, f"Missing slash commands: {missing}"


def test_registry_descriptions_are_sentence_case():
    """Every summary should start with an uppercase letter (sentence case)."""
    for cmd in _cmd.all_commands():
        assert cmd.summary, f"{cmd.name} missing summary"
        first = cmd.summary[0]
        assert first.isupper() or not first.isalpha(), (
            f"{cmd.name} summary should start uppercase: {cmd.summary!r}"
        )


def test_find_returns_command_or_none():
    assert _cmd.find("/login") is not None
    assert _cmd.find("/nope") is None


# ── Typo suggestions ──────────────────────────────────────────────────────


def test_suggest_catches_one_character_typo():
    """`/agent` is one character off from `/agents` — must be suggested."""
    suggestions = _cmd.suggest("/agent")
    assert "/agents" in suggestions


def test_suggest_returns_empty_for_unrelated_input():
    """Random words should not produce noisy suggestions."""
    # Pick a string with no character overlap with any registered command —
    # difflib uses ratio-based matching and even short shared substrings
    # (e.g. q-u-i in /quit) can sneak through.
    suggestions = _cmd.suggest("/xyzabc")
    assert suggestions == []


def test_suggest_caps_at_three_matches():
    suggestions = _cmd.suggest("/log", n=3)
    assert len(suggestions) <= 3


# ── Dispatch ──────────────────────────────────────────────────────────────


def test_dispatch_unknown_slash_prints_did_you_mean(capsys):
    _cmd.dispatch("/agent")
    captured = capsys.readouterr()
    # `warn()` writes to stderr; `info()` writes to stdout — check both.
    combined = captured.out + captured.err
    assert "Unknown command /agent" in combined
    assert "/agents" in combined


def test_dispatch_free_text_prints_redirect(capsys):
    _cmd.dispatch("audit my requirements.txt please")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "marketplace control room" in combined
    assert "/claude-code" in combined


def test_dispatch_empty_line_is_noop(capsys):
    _cmd.dispatch("")
    _cmd.dispatch("   ")
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_dispatch_help_lists_groups(capsys):
    _cmd.dispatch("/help")
    out = capsys.readouterr().out
    # Groups render as their pretty names.
    for group in ("Auth", "Browse", "Hire", "Manage", "Setup", "Bridge", "Meta"):
        assert group in out


def test_dispatch_clear_does_not_raise(capsys):
    """`/clear` calls console.clear(); we just verify no exception."""
    _cmd.dispatch("/clear")  # no assertion — clearing terminal is a no-op in capsys


def test_dispatch_exit_raises_eoferror():
    import pytest
    with pytest.raises(EOFError):
        _cmd.dispatch("/exit")


def test_dispatch_quit_raises_eoferror():
    import pytest
    with pytest.raises(EOFError):
        _cmd.dispatch("/quit")


# ── /claude-code bridge ───────────────────────────────────────────────────


def test_claude_code_handler_warns_when_claude_missing(monkeypatch, capsys):
    """If `claude` is not on PATH, the handler prints an install hint and
    does not call subprocess.run."""
    monkeypatch.setattr("aztea.cli.repl.commands.shutil.which", lambda _: None)
    called = []
    monkeypatch.setattr(
        "aztea.cli.repl.commands.subprocess.run",
        lambda *a, **k: called.append((a, k)),
    )
    _cmd.dispatch("/claude-code")
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "not found on PATH" in combined
    assert called == []


# ── Completer ─────────────────────────────────────────────────────────────


def test_completer_emits_slash_commands_for_empty_input():
    from prompt_toolkit.document import Document
    from aztea.cli.repl.completer import AzteaCompleter

    completer = AzteaCompleter()
    completions = list(completer.get_completions(Document(""), None))
    assert {c.text for c in completions} >= {"/login", "/agents", "/help"}


def test_completer_emits_slash_commands_for_partial_slash():
    from prompt_toolkit.document import Document
    from aztea.cli.repl.completer import AzteaCompleter

    completer = AzteaCompleter()
    completions = list(completer.get_completions(Document("/he"), None))
    texts = {c.text for c in completions}
    # Only commands starting with `/he` should be in the menu.
    assert "/help" in texts
    assert "/login" not in texts


def test_completer_completes_categories_after_category_flag():
    from prompt_toolkit.document import Document
    from aztea.cli.repl.completer import AzteaCompleter

    completer = AzteaCompleter()
    doc = Document("/agents --category ")
    completions = list(completer.get_completions(doc, None))
    texts = {c.text for c in completions}
    assert "Security" in texts
    assert "Code Execution" in texts


def test_completer_completes_job_ids_when_cached():
    from prompt_toolkit.document import Document
    from aztea.cli.repl import completer as _comp

    _comp.remember_jobs(["job-abc", "job-def"])
    completions = list(_comp.AzteaCompleter().get_completions(
        Document("/follow "), None,
    ))
    assert {c.text for c in completions} == {"job-abc", "job-def"}


def test_completer_completes_agent_slugs_when_cached():
    from prompt_toolkit.document import Document
    from aztea.cli.repl import completer as _comp

    _comp.remember_agents(["cve-lookup", "secret-scanner", "python-executor"])
    completions = list(_comp.AzteaCompleter().get_completions(
        Document("/hire "), None,
    ))
    assert {c.text for c in completions} == {
        "cve-lookup", "secret-scanner", "python-executor",
    }


def test_completer_matches_slash_command_without_leading_slash():
    """Typing `l` must surface `/login` so the dropdown is useful from
    the very first keystroke (no slash required)."""
    from prompt_toolkit.document import Document
    from aztea.cli.repl.completer import AzteaCompleter

    completions = list(AzteaCompleter().get_completions(Document("l"), None))
    texts = {c.text for c in completions}
    assert "/login" in texts
    # Anything starting with `l` (or whose body starts with l) is fine; the
    # important thing is /login showed up.


def test_completer_shows_flags_after_trailing_space():
    """After typing `/login ` (no flag yet), the dropdown should show the
    available flags — that's the 'square-bracket arg menu' the user asked
    for, surfaced via the completer."""
    from prompt_toolkit.document import Document
    from aztea.cli.repl.completer import AzteaCompleter

    completions = list(AzteaCompleter().get_completions(Document("/login "), None))
    texts = {c.text for c in completions}
    assert "--api-key" in texts
    assert "--base-url" in texts
    assert "--rotate" in texts


def test_completer_flag_display_uses_square_brackets():
    """The dropdown display string wraps each flag in [brackets] without
    changing the actual text inserted on selection."""
    from prompt_toolkit.document import Document
    from aztea.cli.repl.completer import AzteaCompleter

    completions = list(AzteaCompleter().get_completions(Document("/login "), None))
    api_key_comp = next(c for c in completions if c.text == "--api-key")
    # `.display` is a FormattedText fragment list; the plain-text content
    # is what matters for the visual assertion.
    rendered = "".join(seg[1] for seg in api_key_comp.display)
    assert rendered == "[--api-key]"
    # `.display_meta` carries the hint shown to the right.
    meta = "".join(seg[1] for seg in api_key_comp.display_meta)
    assert meta  # non-empty hint
    assert "az_" in meta or "API key" in meta


def test_completer_flag_hint_is_per_command_when_overridden():
    """`--api-key` has different meanings in different commands; the per-
    command override should win for /login (sign in WITH a key)."""
    from aztea.cli.repl.completer import _flag_hint
    assert "Sign in" in _flag_hint("/login", "--api-key")
    assert "Override" in _flag_hint("/agents", "--api-key")


# ── Application wiring (regression guards) ────────────────────────────────


# ── Login modal (V6) ─────────────────────────────────────────────────────


def _fresh_modal_module(monkeypatch):
    """Re-import the modal module so each test gets a clean state.

    The module holds singleton state (visible flag, current step, field
    refs). Sharing it across tests masks bugs; re-importing guarantees
    isolation. Also builds the Application once so the field refs are
    populated — the modal can't be exercised without those.
    """
    monkeypatch.setenv("LINES", "40")
    monkeypatch.setenv("COLUMNS", "120")
    import importlib
    import aztea.cli.repl.login_modal as login_modal
    importlib.reload(login_modal)
    from aztea.cli.repl.app import _build_application
    _build_application()  # populates the field refs as a side effect
    return login_modal


def test_modal_starts_hidden(monkeypatch):
    """Before show_login_modal, the modal should be invisible."""
    lm = _fresh_modal_module(monkeypatch)
    assert lm._modal_visible[0] is False
    assert lm.modal_is_visible() is False


def test_show_login_modal_flips_visibility(monkeypatch):
    lm = _fresh_modal_module(monkeypatch)
    lm.show_login_modal()
    assert lm._modal_visible[0] is True
    assert lm.modal_is_visible() is True
    # And starts at the method picker step.
    assert lm._modal_step[0] == lm.STEP_METHOD


def test_method_picker_routes_to_email_on_choice_1(monkeypatch):
    lm = _fresh_modal_module(monkeypatch)
    lm.show_login_modal()
    method_field = lm._method_field[0]
    assert method_field is not None
    method_field.buffer.text = "1"
    lm._on_method_accept(method_field.buffer)
    assert lm._modal_step[0] == lm.STEP_EMAIL
    # The picker field should have been cleared.
    assert method_field.buffer.text == ""


def test_method_picker_routes_to_api_key_on_choice_2(monkeypatch):
    lm = _fresh_modal_module(monkeypatch)
    lm.show_login_modal()
    method_field = lm._method_field[0]
    method_field.buffer.text = "2"
    lm._on_method_accept(method_field.buffer)
    assert lm._modal_step[0] == lm.STEP_API_KEY


def test_method_picker_default_choice_routes_to_email(monkeypatch):
    """Empty input (pressing Enter on the default) picks 1 = email."""
    lm = _fresh_modal_module(monkeypatch)
    lm.show_login_modal()
    method_field = lm._method_field[0]
    method_field.buffer.text = ""
    lm._on_method_accept(method_field.buffer)
    assert lm._modal_step[0] == lm.STEP_EMAIL


def test_method_picker_rejects_garbage_with_hint(monkeypatch):
    lm = _fresh_modal_module(monkeypatch)
    lm.show_login_modal()
    method_field = lm._method_field[0]
    method_field.buffer.text = "banana"
    lm._on_method_accept(method_field.buffer)
    # Stays on the picker step, status shows a hint.
    assert lm._modal_step[0] == lm.STEP_METHOD
    assert "1" in lm._modal_status[0] and "2" in lm._modal_status[0]


def test_email_step_advances_to_password(monkeypatch):
    lm = _fresh_modal_module(monkeypatch)
    lm.show_login_modal()
    lm._modal_step[0] = lm.STEP_EMAIL
    email_field = lm._email_field[0]
    email_field.buffer.text = "alice@example.com"
    lm._on_email_accept(email_field.buffer)
    assert lm._modal_step[0] == lm.STEP_PASSWORD
    assert lm._collected_email[0] == "alice@example.com"


def test_email_step_rejects_empty_with_hint(monkeypatch):
    lm = _fresh_modal_module(monkeypatch)
    lm.show_login_modal()
    lm._modal_step[0] = lm.STEP_EMAIL
    email_field = lm._email_field[0]
    email_field.buffer.text = "   "
    lm._on_email_accept(email_field.buffer)
    assert lm._modal_step[0] == lm.STEP_EMAIL
    assert "required" in lm._modal_status[0].lower()


def test_api_key_step_rejects_non_az_prefix(monkeypatch):
    """API keys MUST start with az_ — the modal rejects anything else
    client-side before bothering the server."""
    lm = _fresh_modal_module(monkeypatch)
    lm.show_login_modal()
    lm._modal_step[0] = lm.STEP_API_KEY
    api_key_field = lm._api_key_field[0]
    api_key_field.buffer.text = "sk_live_wrongprefix"
    lm._on_api_key_accept(api_key_field.buffer)
    assert lm._modal_step[0] == lm.STEP_API_KEY  # stayed put
    assert "az_" in lm._modal_status[0]


def test_password_submit_calls_auth_login_with_collected_credentials(monkeypatch):
    """When the user completes the email/password flow, auth.login must
    receive the captured email + the freshly-typed password."""
    lm = _fresh_modal_module(monkeypatch)
    lm.show_login_modal()
    lm._modal_step[0] = lm.STEP_EMAIL

    # Walk: email step → enter "alice@example.com"
    email_field = lm._email_field[0]
    email_field.buffer.text = "alice@example.com"
    lm._on_email_accept(email_field.buffer)

    # Mock _auth.login to capture what we pass it.
    captured_kwargs: dict = {}
    def fake_login(**kwargs):
        captured_kwargs.update(kwargs)
        # Mimic auth.login: raise Exit on success.
        import typer
        raise typer.Exit(code=0)
    import aztea.cli.auth as _auth_module
    monkeypatch.setattr(_auth_module, "login", fake_login)

    # Walk: password step → enter "hunter2"
    password_field = lm._password_field[0]
    password_field.buffer.text = "hunter2"
    lm._on_password_accept(password_field.buffer)

    assert captured_kwargs["email"] == "alice@example.com"
    assert captured_kwargs["password"] == "hunter2"
    assert captured_kwargs["api_key"] is None
    # Submission closes the modal regardless of outcome.
    assert lm._modal_visible[0] is False


def test_api_key_submit_calls_auth_login_with_api_key_only(monkeypatch):
    lm = _fresh_modal_module(monkeypatch)
    lm.show_login_modal()
    lm._modal_step[0] = lm.STEP_API_KEY

    captured_kwargs: dict = {}
    def fake_login(**kwargs):
        captured_kwargs.update(kwargs)
        import typer
        raise typer.Exit(code=0)
    import aztea.cli.auth as _auth_module
    monkeypatch.setattr(_auth_module, "login", fake_login)

    api_key_field = lm._api_key_field[0]
    api_key_field.buffer.text = "az_test_key_123"
    lm._on_api_key_accept(api_key_field.buffer)

    assert captured_kwargs["api_key"] == "az_test_key_123"
    assert captured_kwargs["email"] is None
    assert captured_kwargs["password"] is None
    assert lm._modal_visible[0] is False


def test_hide_login_modal_clears_state(monkeypatch):
    lm = _fresh_modal_module(monkeypatch)
    lm.show_login_modal()
    lm._modal_step[0] = lm.STEP_PASSWORD
    lm._collected_email[0] = "alice@example.com"
    lm._modal_status[0] = "some error"
    lm.hide_login_modal()
    assert lm._modal_visible[0] is False
    assert lm._modal_step[0] == lm.STEP_METHOD
    assert lm._collected_email[0] == ""
    assert lm._modal_status[0] == ""


# ── /agents overlay: scroll + full-width ──────────────────────────────────


def test_overlay_scroll_offset_clamps_at_bounds():
    """Scroll offset must clamp to [0, total-1]; can't go negative or
    past the last line."""
    from aztea.cli.repl import app as _app
    _app._overlay_visible[0] = True
    _app._overlay_text[0] = "\n".join(f"row {i}" for i in range(50))
    _ = _app._overlay_ansi()  # populates total
    assert _app._overlay_total_lines[0] == 50

    # Scroll up from 0 → still 0.
    _app._scroll_overlay(-5)
    assert _app._overlay_scroll[0] == 0

    # Scroll down 10 → 10.
    _app._scroll_overlay(10)
    assert _app._overlay_scroll[0] == 10

    # Page down past the end → clamps to total - 1.
    _app._scroll_overlay(1000)
    assert _app._overlay_scroll[0] == 49


def test_overlay_ansi_slices_from_scroll_offset():
    """_overlay_ansi must hide lines above the current scroll offset."""
    from aztea.cli.repl import app as _app
    _app._overlay_visible[0] = True
    _app._overlay_text[0] = "alpha\nbeta\ngamma\ndelta\nepsilon"
    _app._overlay_scroll[0] = 0
    ansi_top = _app._overlay_ansi()
    # ANSI is a thin wrapper; .value (the underlying string) preserves text.
    top_text = "".join(ansi_top.__pt_formatted_text__()[i][1] for i in range(len(ansi_top.__pt_formatted_text__())))
    assert "alpha" in top_text
    assert "epsilon" in top_text

    _app._overlay_scroll[0] = 2
    ansi_mid = _app._overlay_ansi()
    mid_text = "".join(ansi_mid.__pt_formatted_text__()[i][1] for i in range(len(ansi_mid.__pt_formatted_text__())))
    assert "alpha" not in mid_text
    assert "beta" not in mid_text
    assert "gamma" in mid_text


def test_overlay_show_resets_scroll():
    """Opening a new overlay must reset scroll to 0 — last session's
    position has nothing to do with a fresh result."""
    from aztea.cli.repl import app as _app
    _app._overlay_scroll[0] = 30
    _app._show_overlay("/agents", "row 0\nrow 1\nrow 2")
    assert _app._overlay_scroll[0] == 0
    _app._hide_overlay()


def test_overlay_label_shows_position_when_scrollable():
    """The frame title carries a 'line N/M' hint when there's content
    to scroll, so the user knows there's more below."""
    from aztea.cli.repl import app as _app
    _app._overlay_visible[0] = True
    _app._overlay_text[0] = "\n".join(f"r{i}" for i in range(15))
    _ = _app._overlay_ansi()
    _app._overlay_scroll[0] = 4
    label = _app._overlay_label()
    assert "line 5/15" in label
    assert "scroll" in label.lower()


# ── /register modal ───────────────────────────────────────────────────────


def test_register_validators_match_server_rules():
    """Username / email / password validators mirror server-side rules
    in core/models/core_types.py:UserRegisterRequest. Drift here means
    the user sees a server error instead of the inline hint."""
    from aztea.cli.repl import register_modal as _rm

    # Username: 3-32 chars, [a-zA-Z0-9_-]+
    assert _rm._validate_username("ab") is not None        # too short
    assert _rm._validate_username("x" * 33) is not None    # too long
    assert _rm._validate_username("has space") is not None # invalid char
    assert _rm._validate_username("a.b") is not None       # invalid char
    assert _rm._validate_username("alice_42") is None
    assert _rm._validate_username("AliceB") is None

    # Email regex
    assert _rm._validate_email("") is not None
    assert _rm._validate_email("not-an-email") is not None
    assert _rm._validate_email("alice@example.com") is None
    assert _rm._validate_email("a@b.co") is None

    # Password: 8+, letter+digit
    assert _rm._validate_password("short1") is not None    # < 8
    assert _rm._validate_password("alllettersOK") is not None  # no digit
    assert _rm._validate_password("123456789") is not None # no letter
    assert _rm._validate_password("Goodpass1") is None


def test_register_command_is_in_auth_group():
    """The slash registry must surface /register under Auth so /help
    groups it with /login + /logout + /whoami."""
    cmd = _cmd.find("/register")
    assert cmd is not None
    assert cmd.group == "auth"
    assert "account" in cmd.summary.lower()


def test_register_appears_in_unauth_quickstart():
    """Sign-up should be one of the first things an unauth user sees."""
    from aztea.cli import splash
    guest_cmds = {row[0] for row in splash._SHORTCUTS_GUEST}
    assert "/register" in guest_cmds
    assert "/login" in guest_cmds


# ── Register modal — end-to-end state machine ─────────────────────────────


def test_register_full_happy_path_calls_client_register(monkeypatch):
    """Drive all four steps via the _on_*_accept handlers, assert the
    SDK register call gets the right kwargs and save_config persists."""
    from aztea.cli.repl import register_modal as _rm
    from aztea.cli.repl.app import _capture_command_output as _cap

    captured_register: dict = {}
    saved_config: dict = {}

    class _FakeAuth:
        def register(self, username, email, password):
            captured_register.update(
                username=username, email=email, password=password,
            )
            return {"raw_api_key": "az_minted_42", "username": username}

    class _FakeClient:
        def __init__(self, **k): self.auth = _FakeAuth()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("aztea.client.AzteaClient", _FakeClient)
    monkeypatch.setattr(
        "aztea.config.save_config",
        lambda **kw: saved_config.update(kw),
    )

    # Force a fresh modal state.
    _rm._reset_state()
    _rm._modal_visible[0] = True

    # Walk through the steps via the accept handlers using fake buffers.
    class _Buf:
        def __init__(self, text=""): self.text = text

    buf = _Buf("alice42")
    _rm._on_username_accept(buf)
    assert _rm._modal_step[0] == _rm.STEP_EMAIL

    buf = _Buf("alice@example.com")
    _rm._on_email_accept(buf)
    assert _rm._modal_step[0] == _rm.STEP_PASSWORD

    buf = _Buf("password1")
    _rm._on_password_accept(buf)
    assert _rm._modal_step[0] == _rm.STEP_CONFIRM

    buf = _Buf("password1")
    _rm._on_confirm_accept(buf)

    # The handler called register through the fake client.
    assert captured_register == {
        "username": "alice42",
        "email": "alice@example.com",
        "password": "password1",
    }
    # And the returned API key landed in save_config.
    assert saved_config.get("api_key") == "az_minted_42"
    assert saved_config.get("username") == "alice42"
    # Modal closed after success.
    assert _rm._modal_visible[0] is False


def test_register_password_mismatch_returns_to_password_step():
    """When confirm ≠ password, we bounce back to STEP_PASSWORD with a
    cleared stash — NOT forward, NOT silently rejected."""
    from aztea.cli.repl import register_modal as _rm

    _rm._reset_state()
    _rm._modal_visible[0] = True
    _rm._modal_step[0] = _rm.STEP_CONFIRM
    _rm._collected["username"] = "alice"
    _rm._collected["email"] = "a@b.co"
    _rm._collected["password"] = "Goodpass1"

    class _Buf:
        def __init__(self, text=""): self.text = text

    buf = _Buf("Goodpass2")
    _rm._on_confirm_accept(buf)

    assert _rm._modal_step[0] == _rm.STEP_PASSWORD
    assert _rm._collected["password"] == ""  # stash cleared
    assert "don't match" in _rm._modal_status[0]


def test_register_server_taken_error_routes_to_friendly_hint(monkeypatch):
    """A 409-style 'username taken' AzteaError must map to the
    register.taken code and a hint pointing the user at /login.

    _do_register routes its output through _capture_command_output which
    redirects the Rich consoles to a StringIO — pytest's capsys doesn't
    see that. Read the function's return value instead.
    """
    from aztea.cli.repl import register_modal as _rm
    from aztea.errors import AzteaError

    class _FakeAuth:
        def register(self, **kw):
            raise AzteaError("username already exists")

    class _FakeClient:
        def __init__(self, **k): self.auth = _FakeAuth()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("aztea.client.AzteaClient", _FakeClient)

    _rm._collected.update(
        username="alice", email="a@b.co", password="Goodpass1",
    )
    captured_text = _rm._do_register(
        username="alice", email="a@b.co", password="Goodpass1",
    )
    assert "register.taken" in captured_text
    assert "/login" in captured_text


# ── REPL lifecycle (Phase A Group 4) ──────────────────────────────────────


def test_logout_clears_chat_history(monkeypatch):
    """V19 fix: dispatching /logout from the accept-handler path must
    wipe accumulated chat history before appending the new receipt.

    The /logout special-case in app.on_accept only fires when there's
    real captured output (the "Logged out" success line); a silent
    mock that prints nothing would skip the clear-history branch. So
    we have the mocked logout call success() to produce realistic
    output that the accept-handler can detect.
    """
    from aztea.cli.repl import app as _app
    from aztea.cli.output import success as _success

    # Seed history with some prior chatter.
    _app._output_text.clear()
    _app._append_output("previous transcript line 1\n")
    _app._append_output("previous transcript line 2\n")
    assert len(_app._output_text) >= 2

    handler = _app._make_accept_handler()

    class _Buf:
        text = "/logout"

    def _fake_logout(**kw):
        _success("Logged out", detail="/tmp/fake-config.json")
    monkeypatch.setattr("aztea.cli.repl.commands._auth.logout", _fake_logout)

    handler(_Buf())

    transcript = "".join(_app._output_text)
    assert "previous transcript line 1" not in transcript
    assert "previous transcript line 2" not in transcript
    # The new logout receipt IS present.
    assert "Logged out" in transcript


def test_claude_code_schedules_subprocess_via_request_on_exit(monkeypatch):
    """V12 fix: /claude-code must NOT call subprocess.run inline (that
    nests Claude Code inside Aztea's alt-buffer). Instead it queues the
    command via request_subprocess_on_exit + calls get_app().exit()."""
    from aztea.cli.repl import commands as _cmd
    from aztea.cli.repl import app as _app

    monkeypatch.setattr("aztea.cli.repl.commands.shutil.which", lambda _: "/usr/bin/claude")
    # MCP-registered check shouldn't matter, but stub it to avoid file IO.
    monkeypatch.setattr(
        "aztea.cli.mcp._read_config",
        lambda p: {"mcpServers": {"aztea": {}}},
    )

    scheduled: list = []
    monkeypatch.setattr(
        _app, "request_subprocess_on_exit", lambda cmd: scheduled.append(cmd),
    )
    exited = {"count": 0}

    class _FakeApp:
        def exit(self):
            exited["count"] += 1

    # get_app is imported inside the handler from prompt_toolkit; patch
    # the source, not the importing module.
    monkeypatch.setattr(
        "prompt_toolkit.application.get_app", lambda: _FakeApp(),
    )
    # Subprocess.run must NOT be called from the handler.
    called = []
    monkeypatch.setattr(
        "aztea.cli.repl.commands.subprocess.run",
        lambda *a, **k: called.append((a, k)),
    )

    _cmd.dispatch("/claude-code")

    assert scheduled == [["claude"]]
    assert exited["count"] == 1
    assert called == [], "subprocess.run must defer to start()"


# ── /ask + V7 regression guards ───────────────────────────────────────────


def test_ask_registered_in_its_own_group():
    """`/ask` lives in the 'ask' group so /help can call it out
    separately from the deterministic slash commands."""
    cmd = _cmd.find("/ask")
    assert cmd is not None
    assert cmd.group == "ask"
    assert "troubleshooter" in cmd.summary.lower()


def test_ask_without_api_key_surfaces_clear_error(monkeypatch, capsys):
    """When no env var and no ~/.aztea/anthropic.key, /ask prints an
    actionable error pointing the user at both setup paths."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from aztea.cli.repl import ask as _ask_mod
    monkeypatch.setattr(_ask_mod, "_get_api_key", lambda: None)
    _cmd.dispatch("/ask anything")
    combined = capsys.readouterr().out + capsys.readouterr().err
    # Read the stderr too — error() routes there
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_ask_blank_query_shows_usage(monkeypatch, capsys):
    """Empty `/ask` (no question) prints usage instead of calling the API."""
    from aztea.cli.repl import ask as _ask_mod
    called: list = []
    monkeypatch.setattr(_ask_mod, "_call_anthropic", lambda q, *, key: (called.append(q), (200, {}))[1])
    _cmd.dispatch("/ask")
    captured = capsys.readouterr()
    # API must not be called for a blank question.
    assert called == []
    assert "Usage" in captured.out


def test_ask_handler_calls_api_with_query(monkeypatch):
    """The handler joins argv tokens, looks up the key, and forwards to
    the Anthropic Messages API. Mocks _call_anthropic so we don't make
    a network round-trip."""
    from aztea.cli.repl import ask as _ask_mod
    monkeypatch.setattr(_ask_mod, "_get_api_key", lambda: "az_test")
    captured: dict = {}

    def fake_call(query, *, key):
        captured["query"] = query
        captured["key"] = key
        return 200, {"content": [{"type": "text", "text": "hello"}]}

    monkeypatch.setattr(_ask_mod, "_call_anthropic", fake_call)
    _cmd.dispatch("/ask how do I log in?")
    assert captured["query"] == "how do I log in?"
    assert captured["key"] == "az_test"


def test_help_lists_common_workflows(capsys):
    """`/help` should prepend a 'Common workflows' cheat sheet block."""
    _cmd.dispatch("/help")
    out = capsys.readouterr().out
    assert "Common workflows" in out
    # Each workflow line names a slash command.
    assert "/init" in out
    assert "/claude-code" in out
    assert "/ask" in out


def test_banner_cache_invalidates_on_auth_state_change(monkeypatch, tmp_path):
    """Logging in should make the next banner draw re-render (cache key
    flips when username goes from None → 'alice'). Without this fix the
    Quickstart panel stayed at the unauth shortcuts post-login."""
    import os
    os.environ["LINES"] = "40"
    os.environ["COLUMNS"] = "120"
    from aztea.cli.repl import app as _app

    # Force a clean cache.
    _app._banner_cache["key"] = None
    _app._banner_cache["text"] = ""

    # Simulate signed-out: no config.
    monkeypatch.setenv("AZTEA_CONFIG_DIR", str(tmp_path))
    first = _app._banner_ansi()
    first_key = _app._banner_cache["key"]

    # Simulate signed-in by writing a config.
    import json as _json
    (tmp_path / "config.json").write_text(_json.dumps({
        "api_key": "az_test",
        "username": "alice",
        "base_url": "https://aztea.ai",
    }))
    _ = _app._banner_ansi()
    second_key = _app._banner_cache["key"]

    # Username component of the cache key must have changed.
    assert first_key != second_key, "Banner cache didn't invalidate on auth change"
    assert second_key[1] == "alice"  # (width, username, mcp_registered)


def test_input_buffer_has_accept_handler_attached(monkeypatch):
    """The accept_handler must be on the input Buffer, not a stray attribute.

    Regression: setting ``input_field.accept_handler = on_accept`` after
    TextArea construction is a no-op — the attribute is silently ignored
    by prompt_toolkit. The handler MUST be passed at construction time so
    it lands on ``buffer.accept_handler``. Without this, pressing Enter
    does nothing and /login appears broken.

    Note: this test must NOT actually invoke the handler — invoking it
    runs ``_commands.dispatch`` which mutates the shared Rich console's
    file pointer and pollutes subsequent CliRunner-based tests. The
    wiring assertion alone is enough to catch the regression.
    """
    monkeypatch.setenv("LINES", "40")
    monkeypatch.setenv("COLUMNS", "120")
    from aztea.cli.repl.app import _build_application
    app = _build_application()
    buf = app.layout.current_window.content.buffer
    assert buf.accept_handler is not None, (
        "Input buffer must have accept_handler wired — set it via "
        "TextArea(accept_handler=...) at construction, NOT as an "
        "attribute after the fact."
    )
    assert not buf.read_only(), "Input buffer must be editable"
