#!/usr/bin/env bash
# Umbrella runner for safe manual real-CLI E2E checks.
# Runs tier 1 and tier 2 only. Never calls the dangerous manual tier.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bash "$SCRIPT_DIR/01_readonly.sh" || exit $?
bash "$SCRIPT_DIR/02_mutations_saved.sh" || exit $?
