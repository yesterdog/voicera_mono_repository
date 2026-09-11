"""Agent category and telephony helpers for the voice runtime."""

from __future__ import annotations

from typing import Any, Literal, NamedTuple

AgentCategory = Literal["telephony", "websocket"]


class AgentRoutingError(ValueError):
    """Raised when an agent cannot be routed to a runtime handler."""


class PreamblePolicy(NamedTuple):
    """How a provider's WS handshake precedes the pipeline-driving ``start`` event.

    Vobiz and Plivo send ``start`` as the very first frame. NeuraCX sends a
    bare ``{"event": "connected"}`` ack first — a socket-is-up signal with
    no call metadata — and only then ``start``. ``skip_events`` names
    events the route should silently consume before it starts looking for
    ``start_event``.
    """

    skip_events: frozenset[str] = frozenset()
    start_event: str = "start"


_DEFAULT_PREAMBLE_POLICY = PreamblePolicy()

# Per-provider overrides. Most providers use the default (no preamble,
# "start" is the first frame) and never need an entry here.
_PREAMBLE_POLICIES: dict[str, PreamblePolicy] = {
    "neuracx": PreamblePolicy(skip_events=frozenset({"connected"}), start_event="start"),
}


def preamble_policy(provider: str) -> PreamblePolicy:
    """Return the WS handshake policy for ``provider`` (default if unregistered)."""
    return _PREAMBLE_POLICIES.get(provider, _DEFAULT_PREAMBLE_POLICY)


def agent_category(agent: dict[str, Any]) -> AgentCategory:
    """Return the agent category, defaulting to websocket."""
    category = str(agent.get("agent_category") or "websocket").strip().lower()
    if category not in ("telephony", "websocket"):
        raise AgentRoutingError(f"Unsupported agent_category: {category!r}")
    return category  # type: ignore[return-value]


def telephony_provider(agent: dict[str, Any]) -> str:
    """Return the configured telephony provider for a telephony agent."""
    if agent_category(agent) != "telephony":
        raise AgentRoutingError("Agent is not a telephony agent")
    telephony = agent.get("telephony") or {}
    provider = str(telephony.get("provider") or "").strip().lower()
    if not provider:
        raise AgentRoutingError("Agent has no telephony provider configured")
    return provider
