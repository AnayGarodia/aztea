"""HTTP-layer integration tests for workspace endpoints (PR 2)."""

from __future__ import annotations

import hashlib

import pytest

from tests.integration.helpers import TEST_MASTER_KEY, _auth_headers


def _h() -> dict[str, str]:
    return _auth_headers(TEST_MASTER_KEY)


# ---------------------------------------------------------------------------
# Workspace lifecycle
# ---------------------------------------------------------------------------


def test_post_workspaces_creates_active_workspace(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    r = client.post("/workspaces", json={"ttl_seconds": 3600}, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace_id"].startswith("ws_")
    assert "expires_at" in body


def test_get_workspaces_returns_metadata(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    create = client.post("/workspaces", json={}, headers=_h()).json()
    r = client.get(f"/workspaces/{create['workspace_id']}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["backing_type"] == "bytea"
    assert body["artifact_count"] == 0
    assert body["quota_bytes"] == 64 * 1024 * 1024


def test_get_unknown_workspace_returns_404(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    r = client.get("/workspaces/ws_does_not_exist_12345", headers=_h())
    assert r.status_code == 404
    assert r.json()["error"] == "workspace.not_found"


def test_create_workspace_rejects_invalid_backing(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    r = client.post(
        "/workspaces",
        json={"backing_type": "magnetic_tape"},
        headers=_h(),
    )
    assert r.status_code == 422


def test_delete_workspace_removes_it(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    r = client.delete(f"/workspaces/{ws_id}", headers=_h())
    assert r.status_code == 204
    r2 = client.get(f"/workspaces/{ws_id}", headers=_h())
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Artifact CRUD
# ---------------------------------------------------------------------------


def test_put_artifact_stores_bytes_and_returns_sha256(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    r = client.put(
        f"/workspaces/{ws_id}/artifacts/hello.txt",
        content=b"hello world",
        headers={**_h(), "Content-Type": "text/plain"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert body["size_bytes"] == 11


def test_get_artifact_returns_bytes_with_content_type(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    client.put(
        f"/workspaces/{ws_id}/artifacts/data.json",
        content=b'{"x":1}',
        headers={**_h(), "Content-Type": "application/json"},
    )
    r = client.get(f"/workspaces/{ws_id}/artifacts/data.json", headers=_h())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.content == b'{"x":1}'


def test_list_artifacts_returns_metadata(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    for name, body in (("a", b"AA"), ("b", b"BBB")):
        client.put(
            f"/workspaces/{ws_id}/artifacts/{name}",
            content=body,
            headers={**_h(), "Content-Type": "application/octet-stream"},
        )
    r = client.get(f"/workspaces/{ws_id}/artifacts", headers=_h())
    assert r.status_code == 200
    listing = r.json()["artifacts"]
    assert {a["name"] for a in listing} == {"a", "b"}


def test_delete_artifact_removes_it(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    client.put(f"/workspaces/{ws_id}/artifacts/drop",
               content=b"x", headers={**_h(), "Content-Type": "text/plain"})
    r = client.delete(f"/workspaces/{ws_id}/artifacts/drop", headers=_h())
    assert r.status_code == 204
    r2 = client.get(f"/workspaces/{ws_id}/artifacts/drop", headers=_h())
    assert r2.status_code == 404


def test_put_artifact_rejects_oversized(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    too_big = b"\x00" * (8 * 1024 * 1024 + 1)
    r = client.put(
        f"/workspaces/{ws_id}/artifacts/big.bin",
        content=too_big,
        headers={**_h(), "Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 413
    # The size guard fires either at the middleware (`request.invalid_input`)
    # or in the route handler (`workspace.artifact.too_large`) depending on
    # whether Content-Length is sent. Both surface as 413 to the caller.
    assert r.json()["error"] in (
        "request.invalid_input",
        "workspace.artifact.too_large",
    )


def test_put_artifact_if_match_conflict_then_success(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    first = client.put(
        f"/workspaces/{ws_id}/artifacts/cas",
        content=b"v1",
        headers={**_h(), "Content-Type": "text/plain"},
    ).json()
    r = client.put(
        f"/workspaces/{ws_id}/artifacts/cas",
        content=b"v2",
        headers={**_h(), "Content-Type": "text/plain", "If-Match": "wrong_sha"},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "workspace.artifact.conflict"
    r2 = client.put(
        f"/workspaces/{ws_id}/artifacts/cas",
        content=b"v3",
        headers={**_h(), "Content-Type": "text/plain",
                 "If-Match": first["sha256"]},
    )
    assert r2.status_code == 200


def test_put_artifact_rejects_invalid_name(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    r = client.put(
        f"/workspaces/{ws_id}/artifacts/..%2Fescape",
        content=b"x",
        headers={**_h(), "Content-Type": "text/plain"},
    )
    # Either 400 (name validation) or 404 from FastAPI routing the dots
    # to a different path — both are acceptable rejections; the artifact
    # must not exist after the attempt.
    assert r.status_code in (400, 404)


# ---------------------------------------------------------------------------
# Seal + manifest + verify
# ---------------------------------------------------------------------------


def test_post_seal_returns_signed_manifest(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    client.put(f"/workspaces/{ws_id}/artifacts/a",
               content=b"AA", headers={**_h(), "Content-Type": "text/plain"})
    r = client.post(f"/workspaces/{ws_id}/seal", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["manifest"]["schema"] == "aztea/workspace-seal/1"
    assert "signature" in body
    assert body["public_key_did"].startswith("did:web:")
    assert ":workspaces:sealer" in body["public_key_did"]


def test_get_manifest_after_seal_is_public(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    client.put(f"/workspaces/{ws_id}/artifacts/a",
               content=b"x", headers={**_h(), "Content-Type": "text/plain"})
    client.post(f"/workspaces/{ws_id}/seal", headers=_h())
    # No auth header — public endpoint.
    r = client.get(f"/workspaces/{ws_id}/manifest")
    assert r.status_code == 200
    assert "manifest" in r.json()


def test_get_manifest_before_seal_returns_404(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    r = client.get(f"/workspaces/{ws_id}/manifest")
    assert r.status_code == 404


def test_post_verify_returns_true_for_intact(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    client.put(f"/workspaces/{ws_id}/artifacts/a",
               content=b"x", headers={**_h(), "Content-Type": "text/plain"})
    client.post(f"/workspaces/{ws_id}/seal", headers=_h())
    r = client.post(f"/workspaces/{ws_id}/verify")
    assert r.status_code == 200
    assert r.json()["valid"] is True


def test_sealed_workspace_rejects_writes(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    ws_id = client.post("/workspaces", json={}, headers=_h()).json()["workspace_id"]
    client.put(f"/workspaces/{ws_id}/artifacts/a",
               content=b"x", headers={**_h(), "Content-Type": "text/plain"})
    client.post(f"/workspaces/{ws_id}/seal", headers=_h())
    r = client.put(
        f"/workspaces/{ws_id}/artifacts/b",
        content=b"y",
        headers={**_h(), "Content-Type": "text/plain"},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "workspace.sealed"


def test_did_document_route_returns_jwk(client, isolated_db):
    from core.migrate import apply_migrations
    apply_migrations(str(isolated_db))
    r = client.get("/workspaces/sealer/did.json")
    assert r.status_code == 200
    body = r.json()
    assert body["id"].startswith("did:web:")
    assert body["verificationMethod"][0]["publicKeyJwk"]["crv"] == "Ed25519"
