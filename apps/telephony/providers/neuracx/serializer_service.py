"""NeuraCX frame serializer registration (requires pipecat).

Imported only by ``load_frame_serializers()`` — not loaded by the API.
"""

from __future__ import annotations

from typing import Any

from apps.telephony.providers.neuracx.serializers import NeuraCXFrameSerializer
from apps.telephony.registry import register_frame_serializer


@register_frame_serializer("neuracx")
def create_frame_serializer(
    *,
    stream_sid: str,
    call_sid: str,
    sample_rate: int = 16000,
    **kwargs: Any,
):
    """Build a NeuraCX serializer.

    NeuraCX has no separate stream/call id pair — its `start` event carries
    a single ``room_id`` (used as the call's identity end to end) and a
    ``call_id``. The generic runtime route maps its ``stream_sid`` slot to
    ``room_id`` and its ``call_sid`` slot to ``call_id`` (see
    ``apps/runtime/routes/agent.py``'s stream_sid resolution).

    ``sample_rate`` here is the *pipeline's* internal rate (from the
    runtime's SAMPLE_RATE env), not NeuraCX's wire rate — NeuraCX always
    speaks 8kHz signed 16-bit PCM on the wire regardless of that setting.
    """
    return NeuraCXFrameSerializer(
        room_id=stream_sid,
        call_id=call_sid,
        params=NeuraCXFrameSerializer.InputParams(
            sample_rate=sample_rate,
            **kwargs,
        ),
    )
