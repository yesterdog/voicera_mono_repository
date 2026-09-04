# Voicera-on-Asterisk PoC

Proves Voicera's voice pipeline (`voice_2_voice_server`) can run over a real
Asterisk call, with Vobiz (telephony) and Bhashini (STT/TTS) fully out of
the picture — using Sarvam for STT/TTS and OpenRouter (Qwen) for the LLM
instead.

The Asterisk-facing bridge (`../asterisk_bridge/`) reuses ARI/RTP handling
vendored from the AVA-AI-Voice-Agent-for-Asterisk project (MIT License —
see `../asterisk_bridge/NOTICE`) rather than reimplementing it; everything
else here is specific to this integration.

## Status: live-verified, not just mechanically tested

This has carried a real SIP call end to end, multiple times, with:
- A real softphone (Zoiper) → Asterisk → the bridge → Voicera's pipeline
  → a real, coherent transcribed conversation (not just the greeting).
- Sarvam STT and TTS actually connecting and transcribing/synthesizing.
- Qwen (via OpenRouter) generating contextual replies.
- Clean call teardown — no leaked channels or bridges across repeated calls.

Four real issues were found and fixed getting here (worth knowing if you
hit something similar after moving/rebuilding this):

1. **Runaway channel cascade** — the `externalMedia` channel's own
   `StasisStart` event arrived (as a separately scheduled asyncio task)
   before it was recorded as "known," so it got mistaken for a new caller
   and re-bridged recursively. Fixed by recording channel ids the instant
   they're known, with no `await` in between (`voicera_bridge.py`).
2. **No RTP ever sent** — Asterisk 22's `externalMedia` defaults to waiting
   for the far end to send first. Fixed by passing `connection_type="client"`
   explicitly (`ari_client.py`'s `create_external_media_channel`).
3. **No audio in either direction on a real call** — Asterisk's SDP was
   advertising its own internal Docker container IP, unreachable from the
   host. Fixed with `external_media_address`/`external_signaling_address`
   in `conf/pjsip.conf`'s transport.
4. **Jittery/choppy audio** — the bridge forwarded Voicera's larger
   WebSocket audio chunks as single oversized RTP packets, desyncing
   `RTPServer`'s fixed 160-sample (20ms) timestamp increment from actual
   audio duration. Fixed by repacketizing to 20ms frames and pacing sends
   in real time (`voicera_bridge.py`'s `_pump_ws_to_rtp`).

## Prerequisites

- Docker Desktop.
- A SIP softphone (Zoiper, Linphone, MicroSIP, etc.) on the same machine.
- Voicera's own stack running (`docker compose up -d` from the repo root —
  or at minimum `postgres ferretdb backend minio voice_server`).
- `voice_2_voice_server/.env` has `SARVAM_API_KEY` and `VLLM_API_KEY` /
  `VLLM_BASE_URL=https://openrouter.ai/api/v1` set.
- `voicera_backend/.env` and `voice_2_voice_server/.env` both have the
  **same** `INTERNAL_API_KEY` value (used for the bot-facing agent-config
  endpoint's auth). Restart both `backend` and `voice_server` after
  changing either `.env` — a `docker restart` reuses old env; use
  `docker compose up -d --force-recreate backend voice_server` to be sure
  it actually re-reads the file.

## Demo agent

A demo agent (`agent_id=asterisk-poc-agent`) should exist in Voicera's
`AgentConfig` collection, configured with Sarvam STT/TTS + OpenRouter/Qwen
LLM. To (re)create it — this bypasses the JWT-authenticated `/agents`
create endpoint (which needs a real user/org) and inserts the document
directly, matching the schema `agent_service.create_agent()` produces:

```bash
docker exec voicera_backend python -c "
from pymongo import MongoClient
from datetime import datetime
client = MongoClient('mongodb://admin:admin123@ferretdb:27017/voicera')
col = client['voicera']['AgentConfig']
col.delete_many({'agent_id': 'asterisk-poc-agent'})
now = datetime.now().isoformat()
col.insert_one({
    'agent_type': 'asterisk-poc', 'agent_id': 'asterisk-poc-agent', 'org_id': '',
    'agent_config': {
        'interaction_mode': 'conversational', 'language': 'English',
        'system_prompt': 'You are a helpful voice assistant demoing a phone call proof-of-concept. Keep answers short and conversational, one or two sentences.',
        'greeting_message': 'Hello! This is a live demo of Voicera running over Asterisk. How can I help you today?',
        'stt_model': {'name': 'Sarvam', 'args': {}},
        'tts_model': {'name': 'Sarvam', 'args': {}},
        'llm_model': {'name': 'qwen', 'args': {'model': 'qwen/qwen-2.5-72b-instruct'}},
    },
    'created_at': now, 'updated_at': now,
})
"
```

`org_id` is deliberately `''` (falsy) so `services.py` uses the
`SARVAM_API_KEY`/`VLLM_API_KEY` env vars instead of an org-scoped
Integration record.

## Bring-up

```bash
# 1. Voicera's own stack first (from the repo root):
docker compose up -d

# 2. Then the Asterisk + bridge stack:
docker compose -f asterisk_poc/docker-compose.yml up -d --build
```

This starts:
- `voicera_poc_asterisk` — Asterisk, with SIP (5060/udp), ARI (8088/tcp),
  and a small RTP range (10000-10020/udp) published to the host.
- `voicera_poc_bridge` — `asterisk_bridge/voicera_bridge.py`, built from
  its own small Dockerfile (see `../asterisk_bridge/requirements.txt`),
  on `voicera_network` alongside `voice_server` (no `host.docker.internal`
  hop needed, since it's all one repo now).

Check both came up cleanly:
```bash
docker logs voicera_poc_asterisk --tail 20   # should end with "Asterisk Ready."
docker logs voicera_poc_bridge --tail 10     # should end with "Starting ARI event listener."
```

## Placing the real call

1. Point a softphone at `127.0.0.1:5060` (or the host's LAN IP), SIP
   username `softphone`, password `softphone` (see
   `conf/pjsip.conf` — change these before using anything but a throwaway
   local Asterisk).
2. Dial extension **7000**.
3. You should hear the greeting ("Hello! This is a live demo of Voicera
   running over Asterisk...") synthesized by Sarvam TTS, then be able to
   talk to the agent (Sarvam STT → Qwen via OpenRouter → Sarvam TTS).

## Watching it work

```bash
docker logs -f voicera_poc_bridge     # Asterisk-side call lifecycle
docker logs -f voicera_voice_server | grep -E --line-buffered \
  "Connected to Sarvam|Bot started speaking|Bot stopped speaking|Transcript:|Client connected|Call ended|ERROR"
```

Or point a browser at a [Dozzle](https://github.com/amir20/dozzle) instance
for a live merged-log view across containers:
```bash
docker run -d --name dozzle --restart unless-stopped -p 9999:8080 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro amir20/dozzle:latest
```

## Mechanical-only smoke test (no phone needed)

To re-verify the plumbing without a softphone, originate a silent test call
(won't exercise real STT transcription, since there's no real audio source,
but confirms the ARI/RTP/WS wiring):

```bash
docker exec voicera_poc_asterisk asterisk -rx \
  "channel originate Local/7000@voicera-poc application Wait 15"
```

## Tearing down

```bash
docker compose -f asterisk_poc/docker-compose.yml down
```

(Voicera's own stack is separate — `docker compose down` from the repo
root if you want to stop that too.)

## Explicitly out of scope

Outbound calls, multiple concurrent calls, DTMF/IVR, WS/ARI reconnect
resilience beyond what `ARIClient`/`RTPServer` already do, security
hardening (auth beyond the throwaway PoC credentials above, TLS/WSS),
multi-tenant agent routing.
