#!/usr/bin/env bash
# Guided manual checks for live event streams. Not called by run_safe.sh.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

e2e_init
e2e_require_creds

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tg-messenger-e2e-guided.XXXXXX")"
E2E_GUIDED_SECONDS="${E2E_GUIDED_SECONDS:-60}"

cleanup_all() {
  e2e_stop_registered_backgrounds
  rm -rf "$TMP_DIR"
}
trap cleanup_all EXIT
trap 'cleanup_all; exit 130' INT
trap 'cleanup_all; exit 143' TERM

guided_confirm() {
  local prompt="$1"
  local answer
  e2e_require_interactive
  printf '%s [y/N] ' "$prompt" >&2
  read -r answer
  case "$answer" in
    y|Y|yes|YES)
      return 0
      ;;
  esac
  e2e_skip_step "operator declined guided step"
  return 77
}

step_guided_listen() {
  local log="$TMP_DIR/listen.log"
  local pid

  if [ "${E2E_RUN_LISTEN_GUIDED:-0}" != "1" ]; then
    e2e_skip_step "set E2E_RUN_LISTEN_GUIDED=1 to run the guided listen scenario"
    return 77
  fi
  guided_confirm "Start tg listen and trigger an incoming DM/bot reply within ${E2E_GUIDED_SECONDS}s?" ||
    return $?
  e2e_start_tg_background listen "$log" listen
  pid="$E2E_LAST_BG_PID"
  sleep "$E2E_GUIDED_SECONDS"
  e2e_stop_background_pid "$pid"
  if ! grep -F "← [" "$log" >/dev/null; then
    echo "listen did not observe an incoming event line" >&2
    cat "$log"
    return 1
  fi
}

step_guided_watch() {
  local log="$TMP_DIR/watch.log"
  local pid

  if [ "${E2E_RUN_WATCH_GUIDED:-0}" != "1" ]; then
    e2e_skip_step "set E2E_RUN_WATCH_GUIDED=1 to run the guided watch scenario"
    return 77
  fi
  guided_confirm "Start tg watch and perform a throwaway group deletion scenario within ${E2E_GUIDED_SECONDS}s?" ||
    return $?
  e2e_start_tg_background watch "$log" watch
  pid="$E2E_LAST_BG_PID"
  sleep "$E2E_GUIDED_SECONDS"
  e2e_stop_background_pid "$pid"
  if ! grep -F "Saved Messages" "$log" >/dev/null; then
    echo "watch did not observe a Saved Messages backup line" >&2
    cat "$log"
    return 1
  fi
}

e2e_step "guided listen incoming event" step_guided_listen
e2e_step "guided watch deletion backup" step_guided_watch

e2e_summary
