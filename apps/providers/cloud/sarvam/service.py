"""Build Pipecat (or adapter) services from this vendor's configs."""

from __future__ import annotations

from ...capabilities import languages_map
from ...registry import register_llm, register_stt, register_tts, llm_settings
from .catalog import STT_CAPABILITIES, TTS_CAPABILITIES
from .config import SarvamLLMConfig, SarvamSTTConfig, SarvamTTSConfig


def _vendor_language(capabilities: dict, model: str, language: str) -> str:
    """Translate a canonical language id ("en") to Sarvam's wire code ("en-IN").

    Configs store canonical ids; the catalog declares ``{vendor_code:
    canonical}`` per model. Pipecat's Sarvam STT map only knows the ``*-IN``
    codes (its TTS map happens to alias ``en`` too, which is why TTS worked
    while STT was rejected upstream with "Input should be 'unknown', 'hi-IN',
    …"). Unknown ids pass through unchanged so a vendor code given directly
    still works.
    """
    for vendor_code, canonical in languages_map(capabilities).get(model, {}).items():
        if language in (canonical, vendor_code):
            return vendor_code
    return language


@register_stt
def create_stt(cfg: SarvamSTTConfig):
    from pipecat.services.sarvam.stt import SarvamSTTService, SarvamSTTSettings

    return SarvamSTTService(
        api_key=cfg.api_key,
        settings=SarvamSTTSettings(
            model=cfg.model,
            language=_vendor_language(STT_CAPABILITIES, cfg.model, cfg.language),
        ),
    )


@register_tts
def create_tts(cfg: SarvamTTSConfig):
    from pipecat.services.sarvam.tts import SarvamTTSService, SarvamTTSSettings

    return SarvamTTSService(
        api_key=cfg.api_key,
        settings=SarvamTTSSettings(
            model=cfg.model,
            voice=cfg.voice,
            language=_vendor_language(TTS_CAPABILITIES, cfg.model, cfg.language),
            pace=cfg.speed,
        ),
    )


@register_llm
def create_llm(cfg: SarvamLLMConfig):
    from pipecat.services.sarvam.llm import SarvamLLMService, SarvamLLMSettings

    return SarvamLLMService(
        api_key=cfg.api_key,
        settings=SarvamLLMSettings(**llm_settings(cfg)),
    )

