"""Asterisk frame serializer registration (requires pipecat).

Imported only by ``load_frame_serializers()`` — not loaded by the API.
"""

from __future__ import annotations

from typing import Any

from apps.telephony.registry import register_frame_serializer


@register_frame_serializer("asterisk")
def create_frame_serializer(
    *,
    stream_sid: str,
    call_sid: str,
    sample_rate: int = 8000,
    **kwargs: Any,
):
    """Reuse pipecat's PlivoFrameSerializer — asterisk_bridge speaks its wire
    protocol byte-for-byte (see the asterisk provider package docstring).

    ``auto_hang_up`` is forced off: that feature calls Plivo's REST API to
    end the call, but there is no Plivo account here — ``asterisk_bridge``
    tears down the call itself via Asterisk's ARI when the pipeline ends.
    """
    from pipecat.serializers.plivo import PlivoFrameSerializer

    kwargs.pop("auto_hang_up", None)
    return PlivoFrameSerializer(
        stream_id=stream_sid,
        call_id=call_sid,
        params=PlivoFrameSerializer.InputParams(
            plivo_sample_rate=sample_rate,
            sample_rate=sample_rate,
            auto_hang_up=False,
            **kwargs,
        ),
    )
