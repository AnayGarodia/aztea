"""Server integration tests (auto-split fragment 6/6)."""

def test_public_docs_index_and_content_are_available_without_auth(client):
    index_response = client.get("/public/docs")
    assert index_response.status_code == 200, index_response.text
    body = index_response.json()
    assert body["count"] >= 1
    assert body["docs"]

    first = body["docs"][0]
    assert first["path"] == f"/public/docs/{first['slug']}"

    doc_response = client.get(first["path"])
    assert doc_response.status_code == 200, doc_response.text
    doc_body = doc_response.json()
    assert doc_body["slug"] == first["slug"]
    assert isinstance(doc_body["content"], str)
    assert doc_body["content"].strip()


def test_public_docs_unknown_slug_returns_404(client):
    response = client.get("/public/docs/not-a-real-doc")
    assert response.status_code == 404
