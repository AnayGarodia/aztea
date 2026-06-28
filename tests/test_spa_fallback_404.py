"""Regression: spa_fallback must 404 missing static assets, not serve index.html.

Before the fix, GET /releases/appcast.xml (and any missing file) returned 200 text/html (the
SPA shell), masking missing assets and breaking Sparkle (which expects XML/binary). The fix
404s ONLY known static-asset extensions when absent; extension-less paths and dotted SPA deep
links still serve index.html. Env via monkeypatch only (no module-level setdefault leak).
"""

import httpx
import pytest


@pytest.mark.asyncio
async def test_spa_fallback_404_for_missing_assets(monkeypatch, tmp_path):
    for _k, _v in {
        "API_KEY": "test-master-key",
        "SECRET_KEY": "dummy",
        "JWT_SECRET": "dummy",
        "DATABASE_URL": "sqlite:////tmp/otto-spa-test.db",
        "ENVIRONMENT": "test",
        "TESTING": "1",
    }.items():
        monkeypatch.setenv(_k, _v)

    import server.application as s

    # Hermetic frontend dist: a real index.html + a real asset, nothing else.
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><html>app</html>")
    (dist / "real.js").write_text("console.log(1)")
    monkeypatch.setattr(s, "_FRONTEND_DIST_DIR", dist)

    transport = httpx.ASGITransport(app=s.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        # Missing known static assets → real 404 (the fix).
        assert (await c.get("/releases/appcast.xml")).status_code == 404
        assert (await c.get("/downloads/Otto-9.9.9.dmg")).status_code == 404
        assert (await c.get("/assets/missing.js")).status_code == 404

        # A real asset that exists → served with the right content-type.
        r = await c.get("/real.js")
        assert r.status_code == 200
        assert "javascript" in r.headers.get("content-type", "")

        # Extension-less SPA route → index.html (client-side routing).
        r = await c.get("/spaonly/dashboard")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

        # Dotted SPA deep link (non-asset extension) → index.html, NOT a 404.
        r = await c.get("/spaonly/jane.doe")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

        # Static EXTENSION but OUTSIDE an asset dir (e.g. a future SPA slug like /builders/jane.js)
        # → index.html, NOT 404 (prefix-scoping protects SPA routes anywhere outside asset dirs).
        r = await c.get("/builders/jane.js")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

        # Under an asset dir (otto/) a missing asset still 404s (the Sparkle appcast/DMG path)...
        assert (await c.get("/otto/appcast.xml")).status_code == 404
        # ...but the extension-less /otto landing route still serves the SPA shell.
        r = await c.get("/otto")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

        # HEAD is handled identically (Sparkle HEADs the appcast/DMG before GET): missing
        # asset → 404, real asset → 200. This is the exact Sparkle precondition path.
        assert (await c.head("/otto/appcast.xml")).status_code == 404
        assert (await c.head("/releases/appcast.xml")).status_code == 404
        assert (await c.head("/real.js")).status_code == 200
