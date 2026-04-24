from unittest.mock import MagicMock, patch

import pytest


def _mock_client():
    client = MagicMock()
    client.registry.list.return_value = {
        "agents": [
            {
                "agent_id": "abc-123",
                "name": "Test Agent",
                "description": "A test agent",
                "price_per_call_usd": 0.05,
                "tags": ["test"],
                "trust_score": 87.5,
                "success_rate": 0.92,
                "total_calls": 100,
                "status": "active",
                "endpoint_health_status": "ok",
            }
        ],
        "count": 1,
    }
    client.wallets.me.return_value = {
        "wallet_id": "w-1",
        "balance_cents": 2450,
        "caller_trust": 0.95,
    }
    client.auth.login.return_value = {
        "user_id": "u-1",
        "username": "alice",
        "raw_api_key": "az_abc123",
    }
    return client


@pytest.mark.asyncio
async def test_list_agents_returns_agent_rows():
    from aztea_tui.api import AzteaAPI
    with patch("aztea_tui.api._make_client", return_value=_mock_client()):
        api = AzteaAPI("az_test", "http://localhost:8000")
        agents = await api.list_agents()
    assert len(agents) == 1
    assert agents[0].name == "Test Agent"
    assert agents[0].price_display == "$0.05"
    assert agents[0].trust_score == 87.5


@pytest.mark.asyncio
async def test_wallet_formats_balance():
    from aztea_tui.api import AzteaAPI
    with patch("aztea_tui.api._make_client", return_value=_mock_client()):
        api = AzteaAPI("az_test", "http://localhost:8000")
        wallet = await api.get_wallet()
    assert wallet.balance_display == "$24.50"
    assert wallet.balance_cents == 2450


@pytest.mark.asyncio
async def test_login_returns_credentials():
    from aztea_tui.api import AzteaAPI, LoginResult
    with patch("aztea_tui.api._make_client", return_value=_mock_client()):
        api = AzteaAPI(None, "http://localhost:8000")
        result = await api.login("alice@example.com", "password123")
    assert isinstance(result, LoginResult)
    assert result.api_key == "az_abc123"
    assert result.username == "alice"


@pytest.mark.asyncio
async def test_api_error_on_exception():
    from aztea_tui.api import AzteaAPI, AzteaAPIError
    from aztea.errors import AzteaError
    mock = _mock_client()
    mock.registry.list.side_effect = AzteaError("server down")
    with patch("aztea_tui.api._make_client", return_value=mock):
        api = AzteaAPI("az_test", "http://localhost:8000")
        with pytest.raises(AzteaAPIError, match="server down"):
            await api.list_agents()
