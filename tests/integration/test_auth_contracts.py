from tests.integration.helpers import TEST_MASTER_KEY


def test_missing_api_key_returns_401_with_signup_guidance(client):
    response = client.get("/auth/me")
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["error"] == "AUTHENTICATION_REQUIRED"
    assert body["details"]["signup_url"]
    assert body["details"]["docs_url"]


def test_invalid_api_key_returns_401_with_structured_payload(client):
    response = client.get(
        "/auth/me",
        headers={"Authorization": "Bearer az_invalid_key_for_test"},
    )
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["error"] == "INVALID_API_KEY"
    assert body["message"] == "API key is invalid or expired."
    assert body["details"]["signup_url"]
    assert body["details"]["docs_url"]


def test_valid_api_key_still_authenticates(client):
    response = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {TEST_MASTER_KEY}"},
    )
    assert response.status_code == 200, response.text
