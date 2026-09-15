#!/usr/bin/env bash
# Stop the Voicera Docker stack started via docker-compose.yaml.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

[[ -f docker-compose.yaml ]] || {
    echo "Error: docker-compose.yaml not found at repo root." >&2
    exit 1
}

# Optional box-local overlay (gitignored) — see docker-compose.local.example.yaml.
compose_files=(-f docker-compose.yaml)
if [[ -f docker-compose.local.yaml ]]; then
    compose_files+=(-f docker-compose.local.yaml)
fi
docker compose "${compose_files[@]}" down "$@"
