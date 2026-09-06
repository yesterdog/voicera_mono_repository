#!/usr/bin/env bash
# One-shot bootstrap for the Jambonz PoC's runtime state (i.e. the pieces
# that live in FerretDB + Jambonz MySQL + Redis rather than in files).
#
# Idempotent — safe to rerun. Each step checks for existing state and either
# skips or updates in place, so it also serves as a post-recovery repair
# after a `docker compose -f jambonz_poc/docker-compose.yml down -v` or if
# voice_server gets recreated with a different container IP.
#
# Prerequisites (do these before running me):
#   1. `bash generate-config.sh`           — writes .env.jambonz + drachtio.conf.xml
#   2. Bring up infra (mysql/redis/influxdb):
#        docker compose --env-file .env.jambonz up -d jambonz_mysql jambonz_redis jambonz_influxdb
#      wait for mysql healthy, then:
#        docker compose --env-file .env.jambonz run --rm jambonz_db_create
#   3. Bring up the rest:
#        docker compose --env-file .env.jambonz up -d
#
# Then run me:  bash bootstrap.sh
#
# What I do (all idempotent):
#   1. Insert the `jambonz-poc-agent` doc into voicera_backend's AgentConfig
#      collection (FerretDB), so voice_server has an agent to answer with.
#   2. Log in to jambonz api-server. If admin still has the vendor default
#      password "admin" and force_change=true, change it to JAMBONZ_ADMIN_PW
#      from .env.jambonz (generated) and continue.
#   3. Find or create the SIP Account (sip_realm from SOFTPHONE_REALM,
#      registration_hook → customer_auth_server).
#   4. Find or create the Application (call_hook / call_status_hook →
#      voice_server's current IP + /jambonz/answer?agent_id=jambonz-poc-agent).
#      If it exists with a stale IP, update it in place.
#   5. Bind the Application as the Account's device_calling_application_sid.
#   6. Seed Redis `default:active-fs` with `jambonz_drachtio_fs:5063` so
#      sbc-inbound has a feature-server to route to.
#
# Nothing here touches the Asterisk stack (voicera_poc_*).

set -euo pipefail
cd "$(dirname "$0")"

ENVFILE=./.env.jambonz
if [[ ! -f "$ENVFILE" ]]; then
  echo "ERROR: $ENVFILE not found. Run ./generate-config.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1090
set -a; source "$ENVFILE"; set +a

VOICE_SERVER_CONTAINER=voicera_voice_server
JAMBONZ_API_HOST=http://localhost:3005
AGENT_ID=jambonz-poc-agent

need() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing $1" >&2; exit 1; }; }
need curl
need docker
need python3

# ---------- 1. Seed jambonz-poc-agent into FerretDB ----------
echo "==> [1/5] Upserting AgentConfig doc '$AGENT_ID' into FerretDB ..."
docker exec voicera_backend python - <<PY
from pymongo import MongoClient
from datetime import datetime, timezone
import os
c = MongoClient(host=os.environ['MONGODB_HOST'], port=int(os.environ['MONGODB_PORT']),
                username=os.environ['MONGODB_USER'], password=os.environ['MONGODB_PASSWORD'])
db = c[os.environ['MONGODB_DATABASE']]
now = datetime.now(timezone.utc).isoformat()
doc = {
    'agent_type': 'jambonz-poc',
    'agent_id': '$AGENT_ID',
    'org_id': '',
    'agent_config': {
        'interaction_mode': 'conversational',
        'language': 'English',
        'system_prompt': 'You are a helpful voice assistant demoing a phone call proof-of-concept over Jambonz. Keep answers short and conversational, one or two sentences.',
        'greeting_message': "Hello! You've reached Voicera via Jambonz. How can I help you today?",
        'stt_model': {'name': 'Sarvam', 'args': {}},
        'tts_model': {'name': 'Sarvam', 'args': {}},
        'llm_model': {'name': 'qwen', 'args': {'model': 'qwen/qwen-2.5-7b-instruct'}},
    },
    'created_at': now,
    'updated_at': now,
}
r = db['AgentConfig'].replace_one({'agent_id': '$AGENT_ID'}, doc, upsert=True)
print(f"  matched={r.matched_count} modified={r.modified_count} upserted_id={r.upserted_id or 'existing'}")
PY

# ---------- 2. Log in as admin ----------
echo "==> [2/5] Logging in to jambonz api-server as admin ..."
login_response=$(curl -sf -X POST "$JAMBONZ_API_HOST/v1/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"${JAMBONZ_ADMIN_PW:-admin}\"}" || true)

if [[ -z "$login_response" ]]; then
  # Try default vendor password
  login_response=$(curl -sf -X POST "$JAMBONZ_API_HOST/v1/login" \
    -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"admin"}' || true)
  if [[ -n "$login_response" ]] && grep -q '"force_change":true' <<<"$login_response"; then
    echo "  First-run login accepted default 'admin' pw; changing to \$JAMBONZ_ADMIN_PW ..."
    TOKEN=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' <<<"$login_response")
    USER_SID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["user_sid"])' <<<"$login_response")
    if [[ -z "${JAMBONZ_ADMIN_PW:-}" ]]; then
      echo "  JAMBONZ_ADMIN_PW not in .env.jambonz — regenerating with ./generate-config.sh --force may be needed." >&2
      exit 1
    fi
    curl -sf -X PUT "$JAMBONZ_API_HOST/v1/Users/$USER_SID" \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -d "{\"old_password\":\"admin\",\"new_password\":\"$JAMBONZ_ADMIN_PW\"}" >/dev/null
    login_response=$(curl -sf -X POST "$JAMBONZ_API_HOST/v1/login" \
      -H 'Content-Type: application/json' \
      -d "{\"username\":\"admin\",\"password\":\"$JAMBONZ_ADMIN_PW\"}")
  fi
fi

if [[ -z "$login_response" ]]; then
  echo "ERROR: cannot log in to jambonz api-server. Is jambonz_api_server up? Is JAMBONZ_ADMIN_PW correct?" >&2
  exit 1
fi

TOKEN=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' <<<"$login_response")
AUTH_H="Authorization: Bearer $TOKEN"
echo "  logged in."

# ---------- 3. Find/create Account ----------
echo "==> [3/5] Ensuring Account exists (sip_realm=$SOFTPHONE_REALM) ..."
SP_SID=$(curl -sf "$JAMBONZ_API_HOST/v1/ServiceProviders" -H "$AUTH_H" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["service_provider_sid"])')

ACCT_SID=$(curl -sf "$JAMBONZ_API_HOST/v1/Accounts" -H "$AUTH_H" \
  | python3 -c "
import json,sys
for a in json.load(sys.stdin):
    if a.get('sip_realm')=='$SOFTPHONE_REALM':
        print(a['account_sid']); break
")

if [[ -z "$ACCT_SID" ]]; then
  ACCT_SID=$(curl -sf -X POST "$JAMBONZ_API_HOST/v1/Accounts" \
    -H "$AUTH_H" -H 'Content-Type: application/json' \
    -d "{\"service_provider_sid\":\"$SP_SID\",\"name\":\"voicera-poc\",\"sip_realm\":\"$SOFTPHONE_REALM\",\"registration_hook\":{\"url\":\"http://jambonz_customer_auth_server:4000/auth\",\"method\":\"POST\"}}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["sid"])')
  echo "  created Account $ACCT_SID"
else
  echo "  Account $ACCT_SID already exists"
fi

# ---------- 4. Find/create/update Application ----------
echo "==> [4/5] Ensuring Application exists + points at current voice_server IP ..."
VS_IP=$(docker inspect "$VOICE_SERVER_CONTAINER" \
  --format '{{range $k,$v := .NetworkSettings.Networks}}{{if eq $k "voicera_network"}}{{$v.IPAddress}}{{end}}{{end}}')
if [[ -z "$VS_IP" ]]; then
  echo "ERROR: could not resolve voice_server IP. Is $VOICE_SERVER_CONTAINER running on voicera_network?" >&2
  exit 1
fi
CALL_HOOK="http://$VS_IP:7860/jambonz/answer?agent_id=$AGENT_ID"
STATUS_HOOK="http://$VS_IP:7860/jambonz/status?agent_id=$AGENT_ID"

APP_SID=$(curl -sf "$JAMBONZ_API_HOST/v1/Applications" -H "$AUTH_H" \
  | python3 -c "
import json,sys
for a in json.load(sys.stdin):
    if a.get('name')=='voicera-inbound' and a.get('account_sid')=='$ACCT_SID':
        print(a['application_sid']); break
")

APP_BODY=$(cat <<JSON
{"account_sid":"$ACCT_SID","name":"voicera-inbound","call_hook":{"url":"$CALL_HOOK","method":"POST"},"call_status_hook":{"url":"$STATUS_HOOK","method":"POST"}}
JSON
)

if [[ -z "$APP_SID" ]]; then
  APP_SID=$(curl -sf -X POST "$JAMBONZ_API_HOST/v1/Applications" \
    -H "$AUTH_H" -H 'Content-Type: application/json' -d "$APP_BODY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["sid"])')
  echo "  created Application $APP_SID → $CALL_HOOK"
else
  curl -sf -X PUT "$JAMBONZ_API_HOST/v1/Applications/$APP_SID" \
    -H "$AUTH_H" -H 'Content-Type: application/json' \
    -d "{\"call_hook\":{\"url\":\"$CALL_HOOK\",\"method\":\"POST\"},\"call_status_hook\":{\"url\":\"$STATUS_HOOK\",\"method\":\"POST\"}}" >/dev/null
  echo "  updated Application $APP_SID → $CALL_HOOK (voice_server IP may have drifted; refresh is idempotent)"
fi

# Bind Application as Account's device_calling_application_sid (idempotent — 204 either way)
curl -sf -X PUT "$JAMBONZ_API_HOST/v1/Accounts/$ACCT_SID" \
  -H "$AUTH_H" -H 'Content-Type: application/json' \
  -d "{\"device_calling_application_sid\":\"$APP_SID\"}" >/dev/null
echo "  Application bound as Account's device_calling_application_sid."

# ---------- 5. Seed Redis active-fs ----------
echo "==> [5/5] Ensuring Redis default:active-fs contains jambonz_drachtio_fs:5063 ..."
docker exec jambonz_redis redis-cli SADD default:active-fs jambonz_drachtio_fs:5063 >/dev/null
docker exec jambonz_redis redis-cli SMEMBERS default:active-fs | sed 's/^/  /'

echo ""
echo "Done. Softphone should be able to register at sip:129.121.140.95:5062 with"
echo "  user: softphone  pass: $SOFTPHONE_SIP_PASSWORD  realm: $SOFTPHONE_REALM"
echo "then dial any number → routed to voice_server /jambonz/answer → $AGENT_ID."
