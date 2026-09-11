"""NeuraCX telephony provider.

Inbound-only, WS-streaming provider today: NeuraCX's own dashboard / OBD
API initiates the call and streams directly to our WS route — there is no
answer-XML webhook and no outbound Application/Recording REST surface yet,
so this package intentionally has no ``config.py``, ``client.py``,
``application.py``, ``recording.py``, ``xml.py``, or ``service.py`` (see
``docs/developer/guides/adding-a-telephony-provider.md``). Add those, and
register a ``@register_telephony`` config, once outbound-call triggering
via NeuraCX's OBD API is built.

Serializer requires pipecat and must be imported from
``apps.telephony.providers.neuracx.serializers``.
"""
