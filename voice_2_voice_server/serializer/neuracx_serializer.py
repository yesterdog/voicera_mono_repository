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
import time
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

    # NeuraCX's server rejects outbound `media` events whose base64 payload
    # is "excessively small" and terminates the call with reason:
    #   "exiting bidirectional streaming due to excessively small
    #    Base64 payload in received media event: payload size <N>"
    # observed on 2026-09-09 with a first-chunk of 640 raw bytes (base64
    # = 856 chars). Their own inbound chunks are 1599 raw bytes (base64
    # ~2132 chars), which sets a rough target. We buffer outbound audio
    # until we have at least MIN_OUT_BYTES raw bytes before emitting a
    # media event. Small first-utterance latency cost (~80-120 ms while
    # the buffer fills) but no more rejections.
    MIN_OUT_BYTES = 1600

    # If more than IDLE_FLUSH_SECONDS pass between outbound AudioRawFrames,
    # the accumulator is treated as belonging to a previous utterance and
    # discarded before appending the new frame. Prevents the tail of one
    # TTS response from getting spliced into the start of the next one,
    # which produces audible static/glitch at NeuraCX's playback side —
    # observed on a two-turn call (greeting → user Q → answer) 2026-09-09
    # 21:52 UTC where the answer's opening bytes came out as noise.
    IDLE_FLUSH_SECONDS = 0.15

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

        # Outbound-audio buffer: TTS frames arrive at ~40 ms cadence
        # (Sarvam @ 24kHz → 8kHz becomes ~640 raw bytes/chunk), well
        # below NeuraCX's rejection threshold. Accumulate here until we
        # have MIN_OUT_BYTES worth, then emit one larger media event.
        self._out_buffer = bytearray()
        self._last_audio_monotonic = 0.0

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

        # If the buffer has stale content from a prior utterance (last
        # frame was seen more than IDLE_FLUSH_SECONDS ago), drop it —
        # otherwise the new utterance's first emitted media event would
        # be a splice of stale-tail + new-start bytes, which NeuraCX
        # plays as audible static/glitch.
        now = time.monotonic()
        if self._out_buffer and (now - self._last_audio_monotonic) > self.IDLE_FLUSH_SECONDS:
            logger.debug(
                f"NeuraCX serializer: dropping {len(self._out_buffer)} stale "
                f"buffer bytes (idle {now - self._last_audio_monotonic:.2f}s)"
            )
            self._out_buffer.clear()
        self._last_audio_monotonic = now

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

        # Accumulate until we have enough raw bytes to satisfy NeuraCX's
        # minimum-payload gate (see MIN_OUT_BYTES comment above). Returning
        # None here tells Pipecat's transport there's nothing to emit yet.
        self._out_buffer.extend(data)
        if len(self._out_buffer) < self.MIN_OUT_BYTES:
            return None

        # Drain the accumulated buffer as one media event.
        out_bytes = bytes(self._out_buffer)
        self._out_buffer.clear()

        payload = base64.b64encode(out_bytes).decode("ascii")

        # Outbound `media` envelope is minimal, per the NeuraCX reference
        # SDK: JUST {event: "media", media: {payload: b64}}. Do NOT include
        # sequence_number, room_id, chunk, or timestamp on outbound media —
        # their parser is strict about it and the reference kit omits them.
        # (room_id is sent on OTHER outbound events like `clear` for barge-in,
        # but not on `media`.) An earlier version of this serializer echoed
        # the full inbound envelope shape on outbound too; that caused
        # NeuraCX to hang up ~400ms after our first outbound frame.
        out_json = json.dumps({
            "event": "media",
            "media": {"payload": payload},
        })

        # TEMPORARY: log first outbound frame's actual size for
        # verification post-fix. Counters bump on every emitted (not
        # buffered-only) event; retained since they're serializer-internal
        # state useful for future outbound event types like `clear`.
        self._sequence_number += 1
        self._media_chunk += 1
        if self._media_chunk == 1:
            logger.info(
                f"🔎 NeuraCX first outbound media: "
                f"input_rate={frame.sample_rate}, wire_rate={self._neuracx_sample_rate}, "
                f"input_bytes={len(frame.audio)}, out_bytes={len(out_bytes)}, "
                f"json_len={len(out_json)}, "
                f"first_60_chars={out_json[:60]!r}"
            )
        return out_json

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
            # TEMPORARY: log EVERY non-media event at INFO with the full
            # payload — investigating whether NeuraCX ever sends any
            # error/reject/warning event we've been silently dropping
            # before their `stop` fires. Trim to DEBUG once diagnosis
            # is complete.
            if event in ("connected", "start", "stop"):
                logger.info(
                    f"NeuraCX inbound event={event}: {json.dumps(msg)[:600]}"
                )
            else:
                logger.warning(
                    f"NeuraCX UNRECOGNIZED inbound event={event}: "
                    f"{json.dumps(msg)[:600]}"
                )
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
