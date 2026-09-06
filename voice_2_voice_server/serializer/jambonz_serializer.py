"""Jambonz bidirectional audio streaming (listen verb) serializer.

Unlike Vobiz/Plivo/Twilio, jambonz's `listen` verb with
`bidirectionalAudio.streaming=true` does not wrap audio in a JSON+base64
envelope. It exchanges raw binary linear-PCM WebSocket frames directly in
both directions. The one-time JSON text frame jambonz sends immediately
after connecting (call metadata + sampleRate/mixType) is consumed by the
websocket route handler before the Pipecat transport takes over, so this
serializer only ever sees binary audio frames.
"""

from typing import Optional

from pydantic import BaseModel

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    InputAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer, FrameSerializerType


class JambonzFrameSerializer(FrameSerializer):
    """Serializer for jambonz's `listen` verb bidirectional binary audio stream."""

    class InputParams(BaseModel):
        """Configuration parameters for JambonzFrameSerializer.

        Parameters:
            jambonz_sample_rate: Sample rate negotiated with jambonz for this
                call (must match the `bidirectionalAudio.sampleRate` set in
                the `listen` verb). Jambonz supports 8000/16000/24000/48000/64000.
            sample_rate: Optional override for the pipeline's input sample rate.
        """

        jambonz_sample_rate: int = 8000
        sample_rate: Optional[int] = None

    def __init__(self, call_sid: Optional[str] = None, params: Optional["InputParams"] = None):
        self._call_sid = call_sid
        self._params = params or JambonzFrameSerializer.InputParams()
        self._jambonz_sample_rate = self._params.jambonz_sample_rate
        self._sample_rate = 0  # Pipeline input rate, set in setup()

        self._input_resampler = create_stream_resampler()
        self._output_resampler = create_stream_resampler()

    @property
    def type(self) -> FrameSerializerType:
        return FrameSerializerType.BINARY

    async def setup(self, frame):
        self._sample_rate = self._params.sample_rate or frame.audio_in_sample_rate

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, AudioRawFrame):
            data = frame.audio
            if frame.sample_rate != self._jambonz_sample_rate:
                data = await self._output_resampler.resample(
                    data, frame.sample_rate, self._jambonz_sample_rate
                )
            return data

        # EndFrame/CancelFrame/InterruptionFrame: jambonz call teardown for
        # this integration is driven by the Call Status webhook, not an
        # in-band hangup message, and streaming-mode barge-in control frames
        # aren't documented, so there's nothing to send for those here.
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, str):
            # Stray text frame (e.g. a second metadata message) - ignore.
            return None

        if self._jambonz_sample_rate == self._sample_rate:
            deserialized_data = data
        else:
            deserialized_data = await self._input_resampler.resample(
                data, self._jambonz_sample_rate, self._sample_rate
            )
        if not deserialized_data:
            return None

        return InputAudioRawFrame(
            audio=deserialized_data, num_channels=1, sample_rate=self._sample_rate
        )
