"""Tests for telephony frame serializer factory."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from apps.telephony.serializers import create_frame_serializer


@pytest.mark.parametrize("provider", ["vobiz", "plivo", "Vobiz", "Plivo", "neuracx"])
def test_create_frame_serializer_known_providers(provider: str) -> None:
    serializer = create_frame_serializer(
        provider,
        stream_sid="stream-1",
        call_sid="call-1",
        sample_rate=8000,
    )
    assert serializer is not None
    assert type(serializer).__name__ in (
        "VobizFrameSerializer",
        "PlivoFrameSerializer",
        "NeuraCXFrameSerializer",
    )


def test_vobiz_16k_serializer() -> None:
    serializer = create_frame_serializer(
        "vobiz",
        stream_sid="stream-1",
        call_sid="call-1",
        sample_rate=16000,
    )
    assert type(serializer).__name__ == "VobizFrameSerializer"


def test_unsupported_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        create_frame_serializer(
            "twilio",
            stream_sid="stream-1",
            call_sid="call-1",
        )


def test_empty_provider_rejected() -> None:
    with pytest.raises(ValueError, match="provider id is required"):
        create_frame_serializer(
            "",
            stream_sid="stream-1",
            call_sid="call-1",
        )


# ---------------------------------------------------------------------------
# NeuraCX-specific behavior. All cases below run at neuracx_sample_rate ==
# pipeline sample_rate (8000/8000) so serialize()/deserialize() skip
# resampling entirely and byte counts are exact and assertable.
# ---------------------------------------------------------------------------


def _neuracx_serializer():
    from pipecat.frames.frames import AudioRawFrame

    serializer = create_frame_serializer(
        "neuracx",
        stream_sid="room-1",
        call_sid="call-1",
        sample_rate=8000,
    )
    # setup()'s `self._params.sample_rate or setup.audio_in_sample_rate` short
    # circuits on our explicit sample_rate=8000 above, so a real
    # FrameProcessorSetup (clock/task_manager/pipeline_worker) is never
    # touched — None is safe here.
    asyncio.run(serializer.setup(None))
    return serializer, AudioRawFrame


def test_neuracx_serializer_buffers_below_min_out_bytes() -> None:
    serializer, AudioRawFrame = _neuracx_serializer()
    small_chunk = b"\x01\x00" * 100  # 200 bytes, well under MIN_OUT_BYTES=1600

    result = asyncio.run(
        serializer.serialize(AudioRawFrame(audio=small_chunk, sample_rate=8000, num_channels=1))
    )

    assert result is None


def test_neuracx_serializer_emits_media_once_threshold_crossed() -> None:
    serializer, AudioRawFrame = _neuracx_serializer()
    chunk = b"\x01\x00" * 100  # 200 bytes/frame

    result = None
    fed = b""
    for _ in range(9):  # 9 * 200 = 1800 bytes, crosses MIN_OUT_BYTES=1600
        fed += chunk
        result = asyncio.run(
            serializer.serialize(AudioRawFrame(audio=chunk, sample_rate=8000, num_channels=1))
        )
        if result is not None:
            break

    assert result is not None
    message = json.loads(result)
    # Outbound envelope is bare — no sequence_number/room_id/chunk/timestamp.
    assert set(message.keys()) == {"event", "media"}
    assert message["event"] == "media"
    assert set(message["media"].keys()) == {"payload"}
    payload = base64.b64decode(message["media"]["payload"])
    assert payload == fed
    assert len(payload) % 2 == 0


def test_neuracx_serializer_pads_odd_byte_output() -> None:
    serializer, AudioRawFrame = _neuracx_serializer()
    odd_chunk = bytes(1601)  # odd length, already above MIN_OUT_BYTES alone

    result = asyncio.run(
        serializer.serialize(AudioRawFrame(audio=odd_chunk, sample_rate=8000, num_channels=1))
    )

    assert result is not None
    message = json.loads(result)
    payload = base64.b64decode(message["media"]["payload"])
    assert len(payload) == 1602  # padded to even
    assert payload[-1] == 0


def test_neuracx_serializer_ignores_non_audio_frames() -> None:
    from pipecat.frames.frames import EndFrame

    serializer, _ = _neuracx_serializer()

    result = asyncio.run(serializer.serialize(EndFrame()))

    assert result is None


def test_neuracx_serializer_deserialize_media_roundtrip() -> None:
    serializer, _ = _neuracx_serializer()
    raw_pcm = b"\x02\x00" * 400  # even length, no padding needed
    inbound = json.dumps(
        {
            "event": "media",
            "sequence_number": 12,
            "room_id": "room-1",
            "media": {
                "chunk": 3,
                "timestamp": "120",
                "payload": base64.b64encode(raw_pcm).decode("ascii"),
            },
        }
    )

    frame = asyncio.run(serializer.deserialize(inbound))

    assert frame is not None
    assert frame.audio == raw_pcm
    assert frame.sample_rate == 8000
    assert frame.num_channels == 1


def test_neuracx_serializer_deserialize_pads_odd_byte_input() -> None:
    serializer, _ = _neuracx_serializer()
    raw_pcm = bytes(1599)  # odd length, as NeuraCX actually sends
    inbound = json.dumps({"event": "media", "media": {"payload": base64.b64encode(raw_pcm).decode("ascii")}})

    frame = asyncio.run(serializer.deserialize(inbound))

    assert frame is not None
    assert len(frame.audio) == 1600  # padded to even
    assert frame.audio[-1] == 0


@pytest.mark.parametrize("event", ["connected", "start", "stop", "something_unrecognized"])
def test_neuracx_serializer_deserialize_ignores_non_media_events(event: str) -> None:
    serializer, _ = _neuracx_serializer()
    inbound = json.dumps({"event": event, "start": {"room_id": "room-1"}})

    frame = asyncio.run(serializer.deserialize(inbound))

    assert frame is None
