"""
Audio processing utilities — vendored from AVA-AI-Voice-Agent-for-Asterisk
(MIT License, see ../NOTICE). Only used by rtp_server.py's lazy resample
path; not exercised by the bridge's current 8kHz-mu-law-only configuration.
"""

from .resampler import (
    mulaw_to_pcm16le,
    pcm16le_to_mulaw,
    resample_audio,
    convert_pcm16le_to_target_format,
    resolve_output_resampler_policy,
)

__all__ = [
    "mulaw_to_pcm16le",
    "pcm16le_to_mulaw",
    "resample_audio",
    "convert_pcm16le_to_target_format",
    "resolve_output_resampler_policy",
]
