"""NeuraCX status-pingback normalisation tests."""

from __future__ import annotations

from apps.telephony.providers.neuracx.webhooks import normalize_status_webhook
from apps.telephony.webhooks import (
    is_hangup_event,
    map_hangup_call_response,
    parse_webhook_form,
)


def test_normalize_terminal_status_maps_to_hangup() -> None:
    normalized = normalize_status_webhook(
        {
            "status": "completed",
            "call_id": "955",
            "cli": "+919812345678",
            "dni": "+918012345678",
        }
    )
    assert normalized["Event"] == "hangup"
    assert normalized["CallStatus"] == "completed"
    assert normalized["call_id"] == "955"
    assert normalized["From"] == "+919812345678"
    assert normalized["To"] == "+918012345678"


def test_normalize_non_terminal_status_is_not_hangup() -> None:
    normalized = normalize_status_webhook({"status": "ringing", "call_id": "955"})
    assert normalized["Event"] == "ringing"
    parsed = parse_webhook_form(normalized)
    assert is_hangup_event(parsed.event) is False


def test_normalize_reads_event_field_and_nested_call_id() -> None:
    normalized = normalize_status_webhook(
        {"event": "expired", "stop": {"call_id": "77"}}
    )
    assert normalized["Event"] == "hangup"
    assert normalized["CallStatus"] == "expired"
    assert normalized["call_id"] == "77"


def test_normalize_feeds_shared_hangup_pipeline() -> None:
    normalized = normalize_status_webhook(
        {"status": "no-answer", "call_id": "42", "cli": "+911111111111"}
    )
    parsed = parse_webhook_form(normalized)
    assert is_hangup_event(parsed.event) is True
    assert parsed.provider_call_sid == "42"
    assert map_hangup_call_response(parsed.call_status) == "no_answer"


def test_normalize_empty_payload_is_inert() -> None:
    assert normalize_status_webhook({}) == {}
