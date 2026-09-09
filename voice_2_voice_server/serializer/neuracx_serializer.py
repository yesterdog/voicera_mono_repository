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
        # Trim any trailing half-sample before resampling (see deserialize
        # for the same rationale).
        if len(data) % 2 != 0:
            data = data[:-1]
        if frame.sample_rate != self._neuracx_sample_rate:
            data = await self._output_resampler.resample(
                data, frame.sample_rate, self._neuracx_sample_rate
            )
        if not data:
            return None

        self._sequence_number += 1
        self._media_chunk += 1
        payload = base64.b64encode(data).decode("ascii")

        envelope = {
            "event": "media",
            "sequence_number": self._sequence_number,
            "room_id": self._room_id,
            "media": {
                "chunk": self._media_chunk,
                # ms since start, chunk_size approximation. NeuraCX's own
                # timestamp semantics weren't clarified in the schema doc;
                # a monotonic ~100ms-per-chunk value matches what they emit.
                "timestamp": str(self._media_chunk * 100),
                "payload": payload,
            },
        }
        return json.dumps(envelope)

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

        # Signed 16-bit PCM requires an even byte count. If NeuraCX ever
        # emits an odd-byte chunk (or base64 padding produces one), the
        # soxr resampler downstream errors with "buffer size must be a
        # multiple of element size" and drops the chunk. Trim the trailing
        # half-sample defensively.
        if len(payload) % 2 != 0:
            logger.debug(
                f"NeuraCX serializer: odd-byte payload len={len(payload)}, "
                f"trimming last byte for 16-bit alignment"
            )
            payload = payload[:-1]

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
