"""G.711 mu-law codec (ITU-T G.711), vectorized via lookup tables.

Standalone (no `audioop`, which is removed in Python 3.13+).
"""

import numpy as np

_BIAS = 0x84
_CLIP = 32635


def _linear_to_ulaw_scalar(sample: int) -> int:
    sign = 0
    if sample < 0:
        sample = -sample
        sign = 0x80
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS

    exponent = 7
    exp_mask = 0x4000
    while exponent > 0 and not (sample & exp_mask):
        exponent -= 1
        exp_mask >>= 1

    mantissa = (sample >> (exponent + 3)) & 0x0F
    ulaw = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return ulaw


def _ulaw_to_linear_scalar(ulaw: int) -> int:
    ulaw = ~ulaw & 0xFF
    sign = ulaw & 0x80
    exponent = (ulaw >> 4) & 0x07
    mantissa = ulaw & 0x0F
    sample = ((mantissa << 3) + _BIAS) << exponent
    sample -= _BIAS
    if sign:
        sample = -sample
    return sample


_ENCODE_TABLE = np.array(
    [_linear_to_ulaw_scalar(s) for s in range(-32768, 32768)], dtype=np.uint8
)
_DECODE_TABLE = np.array(
    [_ulaw_to_linear_scalar(u) for u in range(256)], dtype=np.int16
)


def encode(pcm: np.ndarray) -> bytes:
    """int16 mono PCM samples -> mu-law bytes."""
    idx = pcm.astype(np.int32) + 32768
    return _ENCODE_TABLE[idx].tobytes()


def decode(data: bytes) -> np.ndarray:
    """mu-law bytes -> int16 mono PCM samples."""
    idx = np.frombuffer(data, dtype=np.uint8)
    return _DECODE_TABLE[idx]
