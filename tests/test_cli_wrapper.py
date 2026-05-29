"""Plan B Phase 4 (2026-05-27) — wrapper-template CLI.

`aztea wrapper init` generates a deploy-ready FastAPI server + Dockerfile
+ deploy config for sellers who have a framework agent (LangGraph, CrewAI,
MCP, custom Python) and need a minimal HTTP wrapper.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# The SDK lives under sdks/python-sdk/aztea — add that to sys.path so the
# tests import the same module the CLI does.
_SDK_ROOT = Path(__file__).resolve().parent.parent / "sdks" / "python-sdk"
sys.path.insert(0, str(_SDK_ROOT))

try:
    from aztea.cli.wrapper import app as wrapper_app
    import typer.testing
except ImportError:
    pytest.skip("aztea SDK not importable in this test env", allow_module_level=True)


def _run(args: list[str]) -> typer.testing.Result:
    runner = typer.testing.CliRunner()
    return runner.invoke(wrapper_app, args)


def test_wrapper_init_emits_full_set_for_fly():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(["--out-dir", tmp, "--target", "fly"])
        assert result.exit_code == 0, result.output
        files = {p.name for p in Path(tmp).iterdir()}
        assert files == {
            "server.py", "requirements.txt", "Dockerfile",
            "fly.toml", "README.md",
        }


def test_wrapper_init_emits_render_yaml_for_render_target():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(["--out-dir", tmp, "--target", "render"])
        assert result.exit_code == 0, result.output
        files = {p.name for p in Path(tmp).iterdir()}
        assert "render.yaml" in files
        assert "fly.toml" not in files


def test_wrapper_init_omits_platform_config_for_docker_target():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(["--out-dir", tmp, "--target", "docker"])
        assert result.exit_code == 0, result.output
        files = {p.name for p in Path(tmp).iterdir()}
        assert "Dockerfile" in files
        assert "fly.toml" not in files
        assert "render.yaml" not in files


def test_server_py_includes_signature_verification():
    """The whole point of the wrapper: HMAC verification must be wired."""
    with tempfile.TemporaryDirectory() as tmp:
        _run(["--out-dir", tmp, "--target", "fly"])
        server = (Path(tmp) / "server.py").read_text()
        assert "verify_request" in server
        assert "InvalidSignature" in server
        assert "AZTEA_ENDPOINT_SIGNING_SECRET" in server
        # Refuses to start if secret missing.
        assert "raise RuntimeError" in server


def test_server_py_includes_healthz_endpoint():
    """Phase 3b health sweeper expects /healthz on the seller endpoint."""
    with tempfile.TemporaryDirectory() as tmp:
        _run(["--out-dir", tmp, "--target", "fly"])
        server = (Path(tmp) / "server.py").read_text()
        assert "/healthz" in server


def test_server_py_short_circuits_aztea_health_probe():
    """Audit fix 2026-05-27: /run must NOT invoke handler() for Aztea's hourly
    health probe (body == {"_aztea_health": true}). Otherwise sellers' real
    handler runs every hour billing LLM/compute cost while Aztea pays nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        _run(["--out-dir", tmp, "--target", "fly"])
        server = (Path(tmp) / "server.py").read_text()
        assert "_aztea_health" in server
        assert "aztea_health_ack" in server


def test_wrapper_init_rejects_unknown_lang():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(["--out-dir", tmp, "--lang", "rust"])
        assert result.exit_code != 0
        assert "python" in result.output.lower()


def test_wrapper_init_rejects_unknown_target():
    with tempfile.TemporaryDirectory() as tmp:
        result = _run(["--out-dir", tmp, "--target", "kubernetes"])
        assert result.exit_code != 0


def test_wrapper_init_does_not_overwrite_without_force():
    with tempfile.TemporaryDirectory() as tmp:
        _run(["--out-dir", tmp, "--target", "fly"])
        # Modify server.py
        server_path = Path(tmp) / "server.py"
        server_path.write_text("# my custom code, don't blow away")
        # Re-run without --force
        result = _run(["--out-dir", tmp, "--target", "fly"])
        assert result.exit_code == 0
        # Custom content preserved.
        assert server_path.read_text() == "# my custom code, don't blow away"
        # Skip message in output.
        assert "already exists" in result.output


def test_wrapper_init_overwrites_with_force():
    with tempfile.TemporaryDirectory() as tmp:
        _run(["--out-dir", tmp, "--target", "fly"])
        server_path = Path(tmp) / "server.py"
        server_path.write_text("# my custom code")
        result = _run(["--out-dir", tmp, "--target", "fly", "--force"])
        assert result.exit_code == 0
        # Server overwritten with template.
        assert "verify_request" in server_path.read_text()
