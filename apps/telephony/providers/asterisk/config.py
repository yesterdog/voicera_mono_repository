"""Registers Asterisk as an inbound-only telephony provider.

No REST config/client — see the package docstring. This module only marks
"asterisk" valid in ``registered_providers()`` (imported by
``load_providers()``, which apps/api uses without pulling in pipecat).
The WS frame serializer is registered separately in ``serializer_service.py``.
"""

from __future__ import annotations

from apps.telephony.registry import register_inbound_provider

register_inbound_provider(
    "asterisk",
    name="Asterisk",
    description=(
        "Inbound-only. A local asterisk_bridge process streams calls from an "
        "Asterisk PBX to the runtime; no credentials are stored here."
    ),
)
