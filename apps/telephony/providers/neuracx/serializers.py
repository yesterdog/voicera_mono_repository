"""NeuraCX WebSocket frame serializer (requires pipecat).

Import from this module explicitly so Application/Recording clients do not
pull in pipecat at package import time::

    from apps.telephony.providers.neuracx.serializers import NeuraCXFrameSerializer

Schema captured live from real NeuraCX outbound test calls (see
``docs/`` and the proven ``ast_ve`` branch this was ported from). Envelope
shape on the wire:

    {
      "event": <"connected" | "start" | "media" | "stop">,
      "sequence_number": <int>,        # outer WS-frame counter (NeuraCX starts ~10)
      "room_id": <str>,                # from "start" event, echoed on every subsequent frame
      "<event>": { ... event-specific sub-object ... }
    }

Media chunks carry base64-encoded signed 16-bit linear PCM at 8 kHz — NOT
mu-law like Plivo/Vobiz — inside a per-media sub-object:

    {
      "event": "media",
      "sequence_number": <int>,
      "room_id": <str>,
      "media": {
        "chunk": <int>,
        "timestamp": <str>,
        "payload": "<base64 signed 16-bit LE PCM @ 8kHz>"
      }
    }

The runtime's WS route (``apps/runtime/routes/agent.py``) consumes the
``connected`` preamble and the ``start`` event itself before handing the
socket to Pipecat (NeuraCX's ``preamble_policy``, registered in
``agent_routing.py``, tells the route to skip ``connected`` and treat
``start`` as the real handshake). This serializer therefore only ever sees
``media`` frames on the deserialize path and only ever emits ``media``
frames on the serialize path.
"""

from __future__ import annotations

import base64
import json
import time

from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import AudioRawFrame, Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.serializers.base_serializer import FrameSerializer


class NeuraCXFrameSerializer(FrameSerializer):
    """Serializer for NeuraCX's bidirectional WS audio-streaming protocol."""

    # NeuraCX's server rejects outbound `media` events whose base64 payload
    # is "excessively small" and terminates the call with reason:
    #   "exiting bidirectional streaming due to excessively small
    #    Base64 payload in received media event: payload size <N>"
    # observed with a first-chunk of 640 raw bytes (base64 = 856 chars).
    # Their own inbound chunks are 1599 raw bytes, which sets a rough
    # target. Buffer outbound audio until at least MIN_OUT_BYTES raw bytes
    # are accumulated before emitting a media event.
    MIN_OUT_BYTES = 1600

    # If more than IDLE_FLUSH_SECONDS pass between outbound AudioRawFrames,
    # the accumulator is treated as belonging to a previous utterance and
    # discarded before appending the new frame — otherwise the tail of one
    # TTS response can splice into the start of the next one, which
    # produces audible static at NeuraCX's playback side.
    IDLE_FLUSH_SECONDS = 0.15

    class InputParams(FrameSerializer.InputParams):
        """Configuration parameters for NeuraCXFrameSerializer.

        Parameters:
            neuracx_sample_rate: Wire sample rate NeuraCX negotiates in the
                `start` event's media_format.sample_rate (always 8000 in
                every observed capture — bit_rate 128000 confirms 16-bit
                linear PCM, not mu-law). Not driven by the runtime's global
                SAMPLE_RATE env — NeuraCX's wire format is fixed.
            sample_rate: Optional override for the pipeline's input rate.
        """

        neuracx_sample_rate: int = 8000
        sample_rate: int | None = None

    def __init__(
        self,
        room_id: str,
        call_id: str | None = None,
        params: InputParams | None = None,
    ):
        params = params or NeuraCXFrameSerializer.InputParams()
        super().__init__(params)
        self._params: NeuraCXFrameSerializer.InputParams = params

        self._room_id = room_id
        self._call_id = call_id
        self._neuracx_sample_rate = self._params.neuracx_sample_rate
        self._sample_rate = 0  # Pipeline input rate, set in setup()

        # NeuraCX carries two independent monotonic counters on every media
        # frame: the outer sequence_number (WS-frame level, they reserve
        # 1-9 for internal use and start real traffic ~10) and media.chunk
        # (audio-chunk level, starts at 1). Tracked for parity with the
        # observed wire shape even though outbound media omits them today.
        self._sequence_number = 100
        self._media_chunk = 0

        # Outbound-audio buffer: TTS frames arrive well below NeuraCX's
        # rejection threshold. Accumulate here until we have MIN_OUT_BYTES
        # worth, then emit one larger media event.
        self._out_buffer = bytearray()
        self._last_audio_monotonic = 0.0

        self._input_resampler = create_stream_resampler(
            clear_after_secs=self._params.resampler_clear_after_secs
        )
        self._output_resampler = create_stream_resampler(
            clear_after_secs=self._params.resampler_clear_after_secs
        )

    async def setup(self, setup: FrameProcessorSetup):
        self._sample_rate = self._params.sample_rate or setup.audio_in_sample_rate

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if not isinstance(frame, AudioRawFrame):
            # Pipecat sends various control frames (Start/End/Cancel/etc);
            # NeuraCX has no in-band equivalent for those (call teardown is
            # driven by their own status pingback), so nothing to emit.
            return None

        # Drop stale buffered content from a prior utterance before
        # appending — otherwise the next emitted media event would splice
        # stale-tail + new-start bytes, audible as static/glitch.
        now = time.monotonic()
        if self._out_buffer and (now - self._last_audio_monotonic) > self.IDLE_FLUSH_SECONDS:
            logger.debug(
                f"NeuraCX serializer: dropping {len(self._out_buffer)} stale "
                f"buffer bytes (idle {now - self._last_audio_monotonic:.2f}s)"
            )
            self._out_buffer.clear()
        self._last_audio_monotonic = now

        data = frame.audio
        # Pad trailing half-sample with 0x00 — matches NeuraCX's own
        # reference SDK (github.com/devops-prudent/NeuraCX voice-streaming
        # main.py does exactly `raw_pcm += b"\x00"` on odd-byte buffers).
        if len(data) % 2 != 0:
            data = data + b"\x00"
        if frame.sample_rate != self._neuracx_sample_rate:
            data = await self._output_resampler.resample(
                data, frame.sample_rate, self._neuracx_sample_rate
            )
        if not data:
            return None

        # Accumulate until we have enough raw bytes to satisfy NeuraCX's
        # minimum-payload gate. Returning None tells Pipecat's transport
        # there's nothing to emit yet.
        self._out_buffer.extend(data)
        if len(self._out_buffer) < self.MIN_OUT_BYTES:
            return None

        out_bytes = bytes(self._out_buffer)
        self._out_buffer.clear()

        payload = base64.b64encode(out_bytes).decode("ascii")

        # Outbound `media` envelope is minimal, per the NeuraCX reference
        # SDK: JUST {event: "media", media: {payload: b64}}. Do NOT include
        # sequence_number, room_id, chunk, or timestamp on outbound media —
        # their parser is strict about it and the reference kit omits them.
        # Echoing the full inbound envelope shape on outbound caused
        # NeuraCX to hang up ~400ms after the first outbound frame.
        out_json = json.dumps({
            "event": "media",
            "media": {"payload": payload},
        })

        self._sequence_number += 1
        self._media_chunk += 1
        if self._media_chunk == 1:
            logger.info(
                f"NeuraCX first outbound media: "
                f"input_rate={frame.sample_rate}, wire_rate={self._neuracx_sample_rate}, "
                f"input_bytes={len(frame.audio)}, out_bytes={len(out_bytes)}, "
                f"json_len={len(out_json)}"
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
            # WS close that follows ends the pipeline naturally.
            if event in ("connected", "start", "stop"):
                logger.debug(f"NeuraCX inbound event={event}: {json.dumps(msg)[:600]}")
            else:
                logger.warning(
                    f"NeuraCX unrecognized inbound event={event}: {json.dumps(msg)[:600]}"
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
