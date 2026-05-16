from __future__ import annotations

import pytest
import typer

from aztea.cli import auth as cli_auth


class _FakeAuthApi:
    def __init__(self, calls: list[bool]) -> None:
        self.calls = calls

    def login(self, _email: str, _password: str, *, rotate: bool) -> dict:
        self.calls.append(rotate)
        return {
            "username": "founders",
            "raw_api_key": None,
            "key_id": "key_123",
        }


class _FakeClient:
    def __init__(self, calls: list[bool]) -> None:
        self.auth = _FakeAuthApi(calls)

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def test_login_does_not_silently_rotate_when_raw_session_key_is_absent(
    monkeypatch,
) -> None:
    calls: list[bool] = []
    saved: dict[str, str] = {}

    monkeypatch.setattr(cli_auth, "load_config", lambda: {})
    monkeypatch.setattr(cli_auth, "_new_client", lambda **_kwargs: _FakeClient(calls))
    monkeypatch.setattr(cli_auth, "save_config", lambda **kwargs: saved.update(kwargs))
    monkeypatch.setattr(cli_auth, "_run_setup", lambda *_args, **_kwargs: None)

    with pytest.raises(typer.Exit) as exc_info:
        cli_auth.login(
            email="founders@aztea.ai",
            password="password123",
            api_key=None,
            base_url="https://aztea.ai",
            rotate=False,
            force=True,
            json_mode=True,
        )

    assert exc_info.value.exit_code == 1
    assert calls == [False]
    assert saved == {}
