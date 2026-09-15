"""Registers NeuraCX as an inbound-only telephony provider.

No REST config/client here — see the package docstring for why. This
module only marks "neuracx" valid in ``registered_providers()`` (imported
by ``load_providers()``, which apps/api uses without pulling in pipecat).
The WS frame serializer is registered separately in ``serializer_service.py``.
"""

from __future__ import annotations

from apps.telephony.registry import register_inbound_provider

register_inbound_provider(
    "neuracx",
    name="NeuraCX",
    description=(
        "Inbound-only. NeuraCX's platform owns the number and streams the call "
        "to this deployment's WebSocket URL; no credentials are stored here."
    ),
)
