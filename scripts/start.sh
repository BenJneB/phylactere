#!/usr/bin/env bash
# Lance le serveur Phylactère en développement (Ubuntu).
# Les arguments sont transmis au serveur, par exemple : scripts/start.sh --reload
set -euo pipefail

SERVER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../server" && pwd)"

# Surcharges de dev (petit modèle, OCR CPU), sauf si on en désigne d'autres.
export PHYLACTERE_CONFIG="${PHYLACTERE_CONFIG:-$SERVER_DIR/config.dev.toml}"

exec uv --directory "$SERVER_DIR" run --extra cpu python -m phylactere "$@"
