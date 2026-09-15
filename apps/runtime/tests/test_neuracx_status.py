"""NeuraCX status-pingback route tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def test_neuracx_status_missing_ids_is_inert(client: TestClient) -> None:
    response = client.post("/neuracx/status", json={"status": "completed"})
    assert response.status_code == 200


@patch("apps.runtime.routes.telephony.backend_client.notify_campaign_call_status", new_callable=AsyncMock)
@patch("apps.runtime.routes.telephony.backend_client.update_call_by_provider_sid", new_callable=AsyncMock)
def test_neuracx_status_non_terminal_does_not_touch_call_log(
    update_by_sid_mock: AsyncMock,
    notify_mock: AsyncMock,
    client: TestClient,
) -> None:
    response = client.post(
        "/neuracx/status",
        params={"agent_id": "agent-1", "org_id": "org-1"},
        json={"status": "ringing", "call_id": "955"},
    )

    assert response.status_code == 200
    update_by_sid_mock.assert_not_awaited()
    notify_mock.assert_not_awaited()


@patch("apps.runtime.routes.telephony.backend_client.notify_campaign_call_status", new_callable=AsyncMock)
@patch("apps.runtime.routes.telephony.backend_client.update_call_by_provider_sid", new_callable=AsyncMock)
def test_neuracx_status_terminal_closes_call_log_by_provider_sid(
    update_by_sid_mock: AsyncMock,
    notify_mock: AsyncMock,
    client: TestClient,
) -> None:
    response = client.post(
        "/neuracx/status",
        params={"agent_id": "agent-1", "org_id": "org-1"},
        json={"status": "no-answer", "call_id": "955", "cli": "+919812345678"},
    )

    assert response.status_code == 200
    update_by_sid_mock.assert_awaited_once()
    args, _ = update_by_sid_mock.await_args
    assert args[0] == "org-1"
    assert args[1] == "955"
    assert args[2]["status"] == "completed"
    assert args[2]["call_response"] == "no_answer"
    # No internal call_id on a NeuraCX pingback — campaign notify never fires here.
    notify_mock.assert_not_awaited()


@patch("apps.runtime.routes.telephony.backend_client.update_call_by_provider_sid", new_callable=AsyncMock)
def test_neuracx_status_plain_completed_has_no_call_response(
    update_by_sid_mock: AsyncMock,
    client: TestClient,
) -> None:
    response = client.post(
        "/neuracx/status",
        params={"agent_id": "agent-1", "org_id": "org-1"},
        json={"event": "completed", "call_id": "42"},
    )

    assert response.status_code == 200
    update_by_sid_mock.assert_awaited_once()
    args, _ = update_by_sid_mock.await_args
    assert "call_response" not in args[2]
