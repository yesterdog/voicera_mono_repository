"""NeuraCX bidirectional audio streaming serializer.

Schema captured live from a real NeuraCX outbound test call on 2026-09-09
(see git history + neuracx-captures/ during development). Envelope shape:

    {
      "event": <"connected" | "start" | "media" | "stop">,
      "sequence_number": <int>,        # outer WS-frame counter (NeuraCX starts at ~10)
      "room_id": <str>,                # from "start" event, echoed on every subsequent frame
      "<event>": { ... event-specific sub-object ... }
    }

Media chunks carry base64-encoded signed 16-bit linear PCM at 8 kHz — NOT
µ-law like Jambonz/Vobiz — inside a per-media sub-object:

    {
      "event": "media",
      "sequence_number": <int>,
      "room_id": <str>,
      "media": {
        "chunk": <int>,                # monotonic audio-chunk counter (separate from outer seq)
        "timestamp": <str>,            # ms since start (NeuraCX sends it as a string)
        "payload": "<base64 signed 16-bit LE PCM @ 8kHz>"
      }
    }

The WebSocket route handler (api/server.py: neuracx_websocket_endpoint)
consumes the initial "connected" + "start" frames itself and extracts the
room_id before handing the socket to Pipecat. This serializer therefore
only sees "media" frames on the wire and only ever emits "media" frames.
"""

import base64
import json
from typing import Optional

from loguru import logger
from pydantic import BaseModel

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    InputAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer, FrameSerializerType


class NeuraCXFrameSerializer(FrameSerializer):
    """Serializer for NeuraCX's WS streaming protocol."""

    class InputParams(BaseModel):
        """Configuration parameters for NeuraCXFrameSerializer.

        Parameters:
            neuracx_sample_rate: Sample rate NeuraCX negotiates in the
                `start` event's media_format.sample_rate (observed as
                8000 in the live capture — bit_rate 128000 confirms
                16-bit linear PCM, not µ-law).
            sample_rate: Optional override for the pipeline's input rate.
        """

        neuracx_sample_rate: int = 8000
        sample_rate: Optional[int] = None

    def __init__(
        self,
        room_id: str,
        call_id: Optional[str] = None,
        params: Optional["InputParams"] = None,
    ):
        self._room_id = room_id
        self._call_id = call_id
        self._params = params or NeuraCXFrameSerializer.InputParams()
        self._neuracx_sample_rate = self._params.neuracx_sample_rate
        self._sample_rate = 0  # Pipeline input rate, set in setup()

        # NeuraCX carries two independent monotonic counters on every
        # media frame: the outer sequence_number (WS-frame level, they
        # reserve 1-9 for internal use and start real traffic ~10) and
        # media.chunk (audio-chunk level, starts at 1). We track both
        # independently so outbound frames match the observed shape.
        self._sequence_number = 100
        self._media_chunk = 0

        self._input_resampler = create_stream_resampler()
        self._output_resampler = create_stream_resampler()

    @property
    def type(self) -> FrameSerializerType:
        return FrameSerializerType.TEXT

    async def setup(self, frame):
        self._sample_rate = self._params.sample_rate or frame.audio_in_sample_rate

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if not isinstance(frame, AudioRawFrame):
            # Pipecat sends various control frames (Start/End/Cancel/etc);
            # NeuraCX has no in-band equivalent for those (call teardown is
            # driven by the status pingback), so nothing to emit.
            return None

        data = frame.audio
        # Pad trailing half-sample with 0x00 (matches NeuraCX's own reference
        # SDK: github.com/devops-prudent/NeuraCX voice-streaming main.py does
        # exactly `raw_pcm += b"\x00"` on odd-byte buffers).
        if len(data) % 2 != 0:
            data = data + b"\x00"
        if frame.sample_rate != self._neuracx_sample_rate:
            data = await self._output_resampler.resample(
                data, frame.sample_rate, self._neuracx_sample_rate
            )
        if not data:
            return None

        payload = base64.b64encode(data).decode("ascii")

        # Outbound `media` envelope is minimal, per the NeuraCX reference
        # SDK: JUST {event: "media", media: {payload: b64}}. Do NOT include
        # sequence_number, room_id, chunk, or timestamp on outbound media —
        # their parser is strict about it and the reference kit omits them.
        # (room_id is sent on OTHER outbound events like `clear` for barge-in,
        # but not on `media`.) An earlier version of this serializer echoed
        # the full inbound envelope shape on outbound too; that caused
        # NeuraCX to hang up ~400ms after our first outbound frame.
        return json.dumps({
            "event": "media",
            "media": {"payload": payload},
        })

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, bytes):
            try:
                data = data.decode("utf-8")
            except UnicodeDecodeError:
                return None

        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            return None

        event = msg.get("event")
        if event != "media":
            # `connected` + `start` are consumed by the route handler
            # before Pipecat takes over. A `stop` event mid-stream (or a
            # late-arriving control frame) is rare enough to just log; the
            # WS close that follows will end the pipeline naturally.
            if event in ("connected", "start", "stop"):
                logger.debug(f"NeuraCX serializer: ignoring in-stream event={event}")
            return None

        media = msg.get("media") or {}
        payload_b64 = media.get("payload")
        if not payload_b64:
            return None

        try:
            payload = base64.b64decode(payload_b64)
        except (ValueError, TypeError):
            logger.warning("NeuraCX serializer: base64 decode failed on media.payload")
            return None

        # Signed 16-bit PCM requires an even byte count. NeuraCX in fact
        # sends 1599-byte chunks (verified live) — an odd count every
        # frame. Their own reference SDK pads with 0x00 rather than
        # truncating, so we do the same to keep the sample stream
        # length-preserved.
        if len(payload) % 2 != 0:
            payload = payload + b"\x00"

        # NeuraCX confirmed sends signed 16-bit LE PCM at neuracx_sample_rate.
        if self._neuracx_sample_rate != self._sample_rate:
            payload = await self._input_resampler.resample(
                payload, self._neuracx_sample_rate, self._sample_rate
            )
        if not payload:
            return None

        return InputAudioRawFrame(
            audio=payload,
            num_channels=1,
            sample_rate=self._sample_rate,
        )
