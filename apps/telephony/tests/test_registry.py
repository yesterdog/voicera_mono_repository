"""Tests for telephony registry creator maps."""

from __future__ import annotations

import pytest

from apps.telephony.registry import (
    ANSWER_XML_BUILDERS,
    CLIENT_CREATORS,
    FRAME_SERIALIZER_FACTORIES,
    INBOUND_ONLY_PROVIDERS,
    TELEPHONY_CONFIGS,
    build_config,
    create_client,
    get_answer_xml_builder,
    get_client_creator,
    get_frame_serializer_factory,
    is_inbound_only,
    load_frame_serializers,
    load_providers,
    registered_providers,
)
from apps.telephony.providers.plivo.config import PlivoConfig
from apps.telephony.providers.vobiz.config import VobizConfig
from apps.telephony.providers.vobiz import VobizClient
from apps.telephony.providers.plivo import PlivoClient


@pytest.fixture(autouse=True)
def _ensure_providers_loaded() -> None:
    load_providers()


def test_registered_providers_include_vobiz_and_plivo() -> None:
    assert {"vobiz", "plivo"} <= registered_providers()


def test_registered_providers_include_inbound_only() -> None:
    assert registered_providers() == frozenset({"vobiz", "plivo", "neuracx", "asterisk"})


@pytest.mark.parametrize("provider", ["vobiz", "plivo"])
def test_each_provider_has_config_client_and_xml(provider: str) -> None:
    assert provider in TELEPHONY_CONFIGS
    assert provider in CLIENT_CREATORS
    assert provider in ANSWER_XML_BUILDERS


@pytest.mark.parametrize("provider", ["vobiz", "plivo"])
def test_each_provider_has_frame_serializer_after_lazy_load(provider: str) -> None:
    load_frame_serializers()
    assert provider in FRAME_SERIALIZER_FACTORIES


def test_get_client_creator_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported telephony provider"):
        get_client_creator("twilio")


def test_get_answer_xml_builder_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported telephony provider for XML"):
        get_answer_xml_builder("twilio")


def test_get_frame_serializer_factory_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported telephony provider for serializer"):
        get_frame_serializer_factory("twilio")


def test_create_client_from_registered_creators() -> None:
    vobiz = create_client(VobizConfig(auth_id="id", auth_token="tok"))
    assert isinstance(vobiz, VobizClient)

    plivo = create_client(
        PlivoConfig(
            auth_id="id",
            auth_token="tok",
            base_url="https://api.plivo.com/v1/",
        )
    )
    assert isinstance(plivo, PlivoClient)


def test_build_config_rejects_empty_provider() -> None:
    with pytest.raises(ValueError, match="provider id is required"):
        build_config("")


@pytest.mark.parametrize("provider", ["neuracx", "asterisk"])
def test_inbound_only_providers_have_frame_serializer_but_no_rest_registration(
    provider: str,
) -> None:
    """NeuraCX/Asterisk are WS-only — no config/client/answer-XML, deliberately.

    Each streams straight into our WS route (NeuraCX's own dashboard/OBD API,
    or the local asterisk_bridge process) with nothing to provision. They
    should never silently gain a config/client/XML registration without
    someone consciously adding a real config.py/service.py (see each
    package's __init__.py). If this starts failing because those were
    added, update this test alongside them.
    """
    load_frame_serializers()
    assert provider in FRAME_SERIALIZER_FACTORIES
    assert provider not in TELEPHONY_CONFIGS
    assert provider not in CLIENT_CREATORS
    assert provider not in ANSWER_XML_BUILDERS
    assert provider in INBOUND_ONLY_PROVIDERS
    assert is_inbound_only(provider) is True
    assert provider in registered_providers()


@pytest.mark.parametrize("provider", ["vobiz", "plivo"])
def test_rest_providers_are_not_inbound_only(provider: str) -> None:
    assert is_inbound_only(provider) is False


def test_inbound_only_provider_errors_are_specific() -> None:
    with pytest.raises(ValueError, match="inbound-only provider: no config class"):
        build_config("neuracx")
    with pytest.raises(ValueError, match="inbound-only provider: no REST client"):
        get_client_creator("asterisk")
    with pytest.raises(ValueError, match="inbound-only provider: no answer-XML webhook"):
        get_answer_xml_builder("neuracx")
