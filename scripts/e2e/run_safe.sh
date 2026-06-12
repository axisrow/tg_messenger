#!/usr/bin/env bash
# Umbrella runner for safe manual real-CLI E2E checks.
# Runs safe tiers only. Never calls guided event checks or dangerous parity.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bash "$SCRIPT_DIR/01_readonly.sh" || exit $?
bash "$SCRIPT_DIR/02_saved_messages.sh" || exit $?
bash "$SCRIPT_DIR/03_optional_safe.sh" || exit $?
