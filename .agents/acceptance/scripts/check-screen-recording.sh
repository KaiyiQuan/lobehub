#!/usr/bin/env bash
# Compatibility entry point; the implementation ships with the acceptance skill.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec bash "$SCRIPT_DIR/../../skills/acceptance/scripts/check-screen-recording.sh" "$@"
