#!/usr/bin/env bash
# One-shot config generator for Jambonz PoC.
#   - Creates .env.jambonz with fresh random secrets (unless the file exists —
#     rerun with --force to overwrite).
#   - Renders jambonz/drachtio.conf.xml from the .template with envsubst.
#
# Read-only w.r.t. running containers. Safe to rerun to just re-render the XML.
#
# Usage:
#   bash ./generate-config.sh         # first run: generate .env.jambonz + XML
#   bash ./generate-config.sh --force # overwrite existing .env.jambonz
#   bash ./generate-config.sh --xml   # only re-render drachtio.conf.xml

set -euo pipefail
cd "$(dirname "$0")"

FORCE_ENV=0
XML_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE_ENV=1 ;;
    --xml)   XML_ONLY=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 1 ;;
  esac
done

gen() { openssl rand -hex 24; }

if [[ $XML_ONLY -eq 0 ]]; then
  if [[ -f .env.jambonz && $FORCE_ENV -eq 0 ]]; then
    echo "==> .env.jambonz exists — keeping it (use --force to regenerate)."
  else
    echo "==> Generating .env.jambonz with fresh secrets ..."
    cat > .env.jambonz <<EOF
# Generated $(date -u +%FT%TZ) by ./generate-config.sh
# DO NOT COMMIT.

MYSQL_ROOT_PASSWORD=$(gen)
MYSQL_PASSWORD=$(gen)

DRACHTIO_SECRET=$(gen)
JWT_SECRET=$(gen)
ENCRYPTION_SECRET=$(gen)

# Human-facing PoC credential (memorable, matches the Asterisk softphone
# convention in asterisk_poc/conf/pjsip.conf). Rotate before pointing this
# at anything but a throwaway local test.
SOFTPHONE_SIP_PASSWORD=softphone
SOFTPHONE_REALM=voicera-jambonz.local

# Placeholder — see rtpengine section in docker-compose.yml for the fix step.
RTPENGINE_LOCAL_IP=172.18.0.99
EOF
    chmod 600 .env.jambonz
    echo "  Wrote .env.jambonz (mode 600). Keep it secret."
  fi
fi

echo "==> Rendering jambonz/drachtio.conf.xml from template ..."
# shellcheck disable=SC1091
set -a; source .env.jambonz; set +a
envsubst < jambonz/drachtio.conf.xml.template > jambonz/drachtio.conf.xml
chmod 600 jambonz/drachtio.conf.xml
echo "  Wrote jambonz/drachtio.conf.xml"

echo ""
echo "Done. Next: docker compose --env-file .env.jambonz up -d jambonz_mysql jambonz_redis jambonz_influxdb"
echo "See docker-compose.yml top-of-file comments for the full bring-up sequence."
