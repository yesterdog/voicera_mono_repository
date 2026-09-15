"""NeuraCX call-status pingback normalisation.

NeuraCX POSTs call-status events (initiated/ringing/answered/completed/
failed/expired/busy/no-answer) to a status callback URL — see the old
``voice_2_voice_server/api/server.py`` handler this replaces. There is no
answer-XML round trip for NeuraCX (see ``apps/telephony/providers/neuracx``
package docstring), so this only normalises the pingback into the same
vendor-agnostic shape ``apps.telephony.webhooks.parse_webhook_form``
already understands for Vobiz/Plivo, so the shared hangup-handling path in
``apps/runtime/routes/telephony.py`` can process it unchanged.

CAUTION: field names (``status``/``event``, ``cli``/``dni``, where
``call_id`` nests) are inferred from the old normalizer's source, not from
a captured HTTP pingback — the WS ``start`` capture confirms ``cli``/``dni``/
``call_id`` for the streaming path only. Confirm against a real pingback
(or NeuraCX's docs) before relying on this in production.
"""

from __future__ import annotations

from typing import Any, Mapping

_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "expired", "no-answer", "no_answer", "busy"}
)


def _get_nested(payload: Mapping[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def normalize_status_webhook(payload: Mapping[str, Any]) -> dict[str, str]:
    """Map a NeuraCX status pingback onto the shared webhook field names."""
    status = str(payload.get("status") or payload.get("event") or "").strip().lower()
    call_id = (
        payload.get("call_id")
        or payload.get("callId")
        or _get_nested(payload, "start", "call_id")
        or _get_nested(payload, "stop", "call_id")
    )

    out: dict[str, str] = {}
    if call_id:
        out["call_id"] = str(call_id)
    cli = payload.get("cli") or payload.get("from")
    if cli:
        out["From"] = str(cli)
    dni = payload.get("dni") or payload.get("to")
    if dni:
        out["To"] = str(dni)
    direction = payload.get("direction")
    if direction:
        out["Direction"] = str(direction)

    if status in _TERMINAL_STATUSES:
        out["Event"] = "hangup"
        out["CallStatus"] = status
    elif status:
        out["Event"] = status
    return out
