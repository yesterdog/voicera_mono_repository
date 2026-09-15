"""NeuraCX telephony provider.

Inbound-only, WS-streaming provider today: NeuraCX's own dashboard / OBD
API initiates the call and streams directly to our WS route — there is no
answer-XML webhook and no outbound Application/Recording REST surface yet,
so ``config.py`` only calls ``register_inbound_provider("neuracx")`` (see
``apps.telephony.registry``) and this package has no ``client.py``,
``application.py``, ``recording.py``, ``xml.py``, or ``service.py`` (see
``docs/developer/guides/adding-a-telephony-provider.md``). Add those, and
register a real ``@register_telephony`` config, once outbound-call
triggering via NeuraCX's OBD API is built.

Call-status pingbacks are normalised in ``webhooks.py`` (pure, no pipecat)
and consumed by ``apps/runtime/routes/telephony.py``'s ``/neuracx/status``.

Serializer requires pipecat and must be imported from
``apps.telephony.providers.neuracx.serializers``.
"""
