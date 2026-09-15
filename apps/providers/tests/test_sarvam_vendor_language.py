"""Sarvam service must hand pipecat vendor language codes, not canonical ids.

pipecat's Sarvam STT map only knows the ``*-IN`` codes (its TTS map happens to
alias ``en``), so passing canonical ``en`` made Sarvam's realtime STT reject the
session with "Input should be 'unknown', 'hi-IN', …".
"""

from __future__ import annotations

import pytest

from apps.providers.cloud.sarvam.catalog import STT_CAPABILITIES, TTS_CAPABILITIES
from apps.providers.cloud.sarvam.service import _vendor_language


@pytest.mark.parametrize(
    ("capabilities", "model", "language", "expected"),
    [
        (STT_CAPABILITIES, "saaras:v3", "en", "en-IN"),
        (STT_CAPABILITIES, "saaras:v3", "hi", "hi-IN"),
        (STT_CAPABILITIES, "saaras:v3", "multi", "unknown"),
        (STT_CAPABILITIES, "saaras:v3", "en-IN", "en-IN"),  # vendor code passes through
        (TTS_CAPABILITIES, "bulbul:v3", "en", "en-IN"),
        (STT_CAPABILITIES, "saaras:v3", "xx", "xx"),  # unknown id left untouched
    ],
)
def test_vendor_language(capabilities, model, language, expected) -> None:
    assert _vendor_language(capabilities, model, language) == expected


def test_unknown_model_passes_language_through() -> None:
    assert _vendor_language(STT_CAPABILITIES, "not-a-model", "en") == "en"
