"""
Asterisk <-> Voicera bridge (PoC).

Bridges a live Asterisk call to Voicera's `voice_2_voice_server` over its
existing `/asterisk/agent/{agent_id}` WebSocket endpoint, reusing this
repo's own ARIClient/RTPServer for the Asterisk-facing half (ARI
externalMedia + bidirectional RTP) instead of duplicating that logic.

Wire format is deliberately 8kHz mu-law on both sides:
  - It's the well-exercised path in this repo's RTPServer (codec="ulaw"
    decodes/encodes via audioop with no resampling surprises).
  - It's also Voicera's default (non-16kHz) Vobiz/Plivo wire format
    (see pipecat's PlivoFrameSerializer: {"event":"media","media":{"payload":
    base64(mu-law)}} in, {"event":"playAudio","media":{"payload":
    base64(mu-law)}} out) and matches voice_2_voice_server's default
    SAMPLE_RATE=8000 (utils/bot_utils.get_sample_rate).

Explicitly out of scope for this PoC: outbound calls, multiple concurrent
calls beyond what ARIClient/RTPServer already support, WS reconnect/retry,
DTMF, auth/TLS hardening. See ../asterisk_poc/README.md.

ari_client.py, rtp_server.py, and audio/resampler.py in this directory are
vendored from AVA-AI-Voice-Agent-for-Asterisk (MIT License, see NOTICE);
this file and logging_config.py are original to this integration.
"""

import asyncio
import audioop
import base64
import contextlib
import json
import os
import signal
import socket
from typing import Optional

import websockets

from .ari_client import ARIClient
from .rtp_server import RTPServer
from .logging_config import get_logger, configure_logging

logger = get_logger(__name__)

# Asterisk ARI connection
ASTERISK_HOST = os.environ.get("BRIDGE_ASTERISK_HOST", "127.0.0.1")
ASTERISK_PORT = int(os.environ.get("BRIDGE_ASTERISK_PORT", "8088"))
ASTERISK_SCHEME = os.environ.get("BRIDGE_ASTERISK_SCHEME", "http")
ASTERISK_USERNAME = os.environ.get("BRIDGE_ARI_USERNAME", "asterisk")
ASTERISK_PASSWORD = os.environ.get("BRIDGE_ARI_PASSWORD", "asterisk")
ASTERISK_APP_NAME = os.environ.get("BRIDGE_STASIS_APP", "voicera-poc")

# RTP (Asterisk externalMedia) side.
# RTP_HOST is the local bind address; RTP_ADVERTISE_HOST is what we tell
# Asterisk to send RTP *to* (e.g. this container's own IP when Asterisk and
# the bridge are separate Docker containers on the same network and
# hostnames aren't reliably resolved by Asterisk's externalMedia socket).
RTP_HOST = os.environ.get("BRIDGE_RTP_HOST", "0.0.0.0")
RTP_ADVERTISE_HOST = os.environ.get("BRIDGE_RTP_ADVERTISE_HOST") or socket.gethostbyname(
    socket.gethostname()
)
RTP_BASE_PORT = int(os.environ.get("BRIDGE_RTP_PORT", "20000"))
RTP_PORT_RANGE_END = int(
    os.environ.get("BRIDGE_RTP_PORT_RANGE_END", str(RTP_BASE_PORT + 100))
)

# Voicera voice_2_voice_server side
VOICERA_WS_URL = os.environ.get("VOICERA_WS_URL", "ws://127.0.0.1:7860")
VOICERA_AGENT_ID = os.environ.get("VOICERA_AGENT_ID")

# Must match RTPServer.SAMPLES_PER_PACKET (rtp_server.py) — that's the
# frame size its RTP timestamp bookkeeping implicitly assumes per send.
RTP_FRAME_BYTES = 160  # 20ms @ 8kHz mu-law (1 byte/sample)
RTP_FRAME_SECONDS = 0.02


class VoiceraBridge:
    """Owns one ARIClient + one RTPServer and fans calls out to Voicera over WS."""

    def __init__(self):
        self.ari = ARIClient(
            username=ASTERISK_USERNAME,
            password=ASTERISK_PASSWORD,
            base_url=f"{ASTERISK_SCHEME}://{ASTERISK_HOST}:{ASTERISK_PORT}/ari",
            app_name=ASTERISK_APP_NAME,
        )
        self.rtp = RTPServer(
            host=RTP_HOST,
            port=RTP_BASE_PORT,
            engine_callback=self._on_rtp_audio,
            codec="ulaw",
            format="ulaw",
            sample_rate=8000,  # matches RTPServer.SAMPLE_RATE -> no resampling
            port_range=(RTP_BASE_PORT, RTP_PORT_RANGE_END),
        )
        self.calls: dict[str, dict] = {}
        # Populated the instant we learn an id (caller or externalMedia leg),
        # with no `await` in between — closes the race where the externalMedia
        # channel's own StasisStart arrives (as a separately scheduled task)
        # before self.calls[channel_id] would otherwise have been set,
        # which would otherwise be mistaken for a brand-new caller and
        # bridged again, cascading indefinitely.
        self._known_channel_ids: set[str] = set()
        self._listener_task: asyncio.Task | None = None

    async def start(self):
        if not VOICERA_AGENT_ID:
            raise RuntimeError("VOICERA_AGENT_ID must be set")
        await self.rtp.start()
        self.ari.add_event_handler("StasisStart", self._on_stasis_start)
        self.ari.add_event_handler("ChannelDestroyed", self._on_channel_destroyed)
        self._listener_task = asyncio.create_task(self.ari.start_listening())
        logger.info(
            "Voicera bridge started",
            app=ASTERISK_APP_NAME,
            voicera_ws_url=VOICERA_WS_URL,
            agent_id=VOICERA_AGENT_ID,
        )

    async def stop(self):
        for channel_id in list(self.calls.keys()):
            await self._cleanup_call(channel_id)
        await self.rtp.stop()
        await self.ari.disconnect()
        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
        logger.info("Voicera bridge stopped")

    # ------------------------------------------------------------------ #
    # ARI events
    # ------------------------------------------------------------------ #

    async def _on_stasis_start(self, event: dict):
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        if not channel_id:
            return
        if channel_id in self._known_channel_ids:
            return
        self._known_channel_ids.add(channel_id)

        logger.info("Call entering Stasis", channel_id=channel_id)
        try:
            await self.ari.answer_channel(channel_id)

            rtp_port = await self.rtp.allocate_session(channel_id)
            external_channel = await self.ari.create_external_media_channel(
                app=ASTERISK_APP_NAME,
                external_host=f"{RTP_ADVERTISE_HOST}:{rtp_port}",
                format="ulaw",
                direction="both",
                # Without this, Asterisk 22's externalMedia appears to default
                # to waiting for us to send first (never observed sending any
                # RTP unprompted across many test calls) instead of proactively
                # sending to external_host.
                connection_type="client",
            )
            if not external_channel:
                logger.error("Failed to create external media channel", channel_id=channel_id)
                await self._cleanup_call(channel_id)
                return
            external_channel_id = external_channel["id"]
            # Record before any further await so its own StasisStart (handled
            # as a separately scheduled task) is recognized and skipped.
            self._known_channel_ids.add(external_channel_id)

            # "proxy_media" forces Asterisk to relay actual RTP through its own
            # core for this bridge instead of optimizing 2-party bridges into a
            # native/direct-media passthrough, which would try to point the
            # caller and the externalMedia leg at each other's raw addresses
            # directly (unreachable across the container/host boundary here).
            bridge_id = await self.ari.create_bridge("mixing,proxy_media")
            if not bridge_id:
                logger.error("Failed to create bridge", channel_id=channel_id)
                await self._cleanup_call(channel_id)
                return

            self.calls[channel_id] = {
                "external_channel_id": external_channel_id,
                "bridge_id": bridge_id,
                "ws": None,
                "recv_task": None,
            }

            if not await self.ari.add_channel_to_bridge(bridge_id, channel_id):
                await self._cleanup_call(channel_id)
                return
            if not await self.ari.add_channel_to_bridge(bridge_id, external_channel_id):
                await self._cleanup_call(channel_id)
                return

            ws_url = f"{VOICERA_WS_URL}/asterisk/agent/{VOICERA_AGENT_ID}"
            ws = await websockets.connect(ws_url)
            await ws.send(
                json.dumps(
                    {
                        "event": "start",
                        "start": {"callId": channel_id, "streamId": channel_id},
                    }
                )
            )
            self.calls[channel_id]["ws"] = ws
            self.calls[channel_id]["recv_task"] = asyncio.create_task(
                self._pump_ws_to_rtp(channel_id, ws)
            )
            logger.info(
                "Call bridged to Voicera",
                channel_id=channel_id,
                external_channel_id=external_channel_id,
                bridge_id=bridge_id,
            )
        except Exception:
            logger.error("Error bridging call", channel_id=channel_id, exc_info=True)
            await self._cleanup_call(channel_id)

    async def _on_channel_destroyed(self, event: dict):
        channel_id = event.get("channel", {}).get("id")
        if channel_id and channel_id in self.calls:
            logger.info("Channel destroyed", channel_id=channel_id)
            await self._cleanup_call(channel_id)

    # ------------------------------------------------------------------ #
    # Audio plumbing
    # ------------------------------------------------------------------ #

    async def _on_rtp_audio(self, call_id: str, ssrc: int, pcm16_8k: bytes):
        """RTPServer hands us decoded 16-bit PCM @ 8kHz; re-encode to mu-law for Voicera."""
        call = self.calls.get(call_id)
        if not call or call.get("ws") is None:
            return
        try:
            mulaw = audioop.lin2ulaw(pcm16_8k, 2)
            await call["ws"].send(
                json.dumps({"event": "media", "media": {"payload": base64.b64encode(mulaw).decode("ascii")}})
            )
        except Exception:
            logger.debug("WS send failed (call likely ending)", call_id=call_id)

    async def _pump_ws_to_rtp(self, call_id: str, ws):
        """Voicera's playAudio payload is already mu-law @ 8kHz; repacketize to
        RTP.send_audio()'s implicit 160-byte (20ms) frame assumption (it always
        advances the RTP timestamp by exactly 160 samples per call, regardless
        of the chunk size given) and pace sends in real time. Voicera streams
        larger chunks (audio_out_10ms_chunks=4 -> ~40ms each); forwarding those
        as single oversized packets desyncs the RTP timestamp from actual
        audio duration and was producing jittery/choppy playback.
        """
        next_send_time: Optional[float] = None
        loop = asyncio.get_event_loop()
        try:
            async for message in ws:
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if data.get("event") == "playAudio":
                    payload_b64 = (data.get("media") or {}).get("payload")
                    if not payload_b64:
                        continue
                    mulaw = base64.b64decode(payload_b64)
                    for offset in range(0, len(mulaw), RTP_FRAME_BYTES):
                        chunk = mulaw[offset : offset + RTP_FRAME_BYTES]
                        if not chunk:
                            continue
                        now = loop.time()
                        if next_send_time is None or next_send_time < now:
                            next_send_time = now
                        else:
                            await asyncio.sleep(next_send_time - now)
                        await self.rtp.send_audio(call_id, chunk)
                        next_send_time += RTP_FRAME_SECONDS
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            logger.error("Error pumping Voicera audio to RTP", call_id=call_id, exc_info=True)
        finally:
            logger.info("Voicera WS closed", call_id=call_id)
            await self._cleanup_call(call_id)

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    async def _cleanup_call(self, channel_id: str):
        call = self.calls.pop(channel_id, None)
        if not call:
            return

        ws = call.get("ws")
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

        recv_task = call.get("recv_task")
        if recv_task and not recv_task.done():
            recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recv_task

        await self.rtp.cleanup_session(channel_id)

        bridge_id = call.get("bridge_id")
        if bridge_id:
            await self.ari.destroy_bridge(bridge_id)

        external_channel_id = call.get("external_channel_id")
        if external_channel_id:
            with contextlib.suppress(Exception):
                await self.ari.hangup_channel(external_channel_id)

        with contextlib.suppress(Exception):
            await self.ari.hangup_channel(channel_id)

        logger.info("Call cleaned up", channel_id=channel_id)


async def main():
    configure_logging()
    bridge = VoiceraBridge()
    await bridge.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await bridge.stop()


if __name__ == "__main__":
    asyncio.run(main())
