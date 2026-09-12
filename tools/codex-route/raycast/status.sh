#!/usr/bin/env bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Codex Route: Status
# @raycast.mode compact

# Optional parameters:
# @raycast.icon ℹ️
# @raycast.packageName Codex Route
# @raycast.description Show the current Codex default route

set -euo pipefail
SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE" ]]; do
  DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
ROOT="$(cd "$(dirname "$SOURCE")/.." && pwd)"
exec python3 "$ROOT/codex_route.py" status
