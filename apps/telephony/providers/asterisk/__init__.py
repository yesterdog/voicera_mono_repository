"""Asterisk telephony provider — inbound-only, via a local bridge process.

``asterisk_bridge`` (repo root) is a standalone ARI/RTP process, not a
CPaaS vendor: it originates the WS connection itself using the wire
protocol pipecat's ``PlivoFrameSerializer`` already speaks — see
``asterisk_bridge/voicera_bridge.py``'s module docstring, which documents
this deliberately (µ-law @ 8kHz, ``{"event":"media",...}`` in /
``{"event":"playAudio",...}`` out). There is no Asterisk REST API to
provision an Application against, so ``config.py`` only calls
``register_inbound_provider("asterisk")`` — no ``client.py``,
``application.py``, ``recording.py``, or ``xml.py``.

Frame serializer requires pipecat and must be imported from
``apps.telephony.providers.asterisk.serializer_service`` (via
``apps.telephony.serializers.create_frame_serializer("asterisk", ...)``).
"""
