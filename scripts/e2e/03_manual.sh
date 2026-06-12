#!/usr/bin/env bash
# Tier 3: dangerous/account-visible manual checks. This script is never called
# by run_safe.sh. It requires E2E_I_UNDERSTAND=1 and asks before every step.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

e2e_init
e2e_require_creds
e2e_require_danger_guard

SERVICE_SECONDS="${E2E_SERVICE_SECONDS:-20}"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tg-messenger-e2e-manual.XXXXXX")"
trap 'e2e_cleanup_created_messages; rm -rf "$TMP_DIR"' EXIT

manual_real_peer_send_delete() {
  if [ -z "${E2E_REAL_PEER:-}" ]; then
    e2e_skip_step "E2E_REAL_PEER is not set"
    return 77
  fi
  if ! e2e_confirm "Send, mark-read and delete a test message in E2E_REAL_PEER=$E2E_REAL_PEER?"; then
    e2e_skip_step "operator declined"
    return 77
  fi
  local marker
  local history
  local id
  marker="$(e2e_marker real-peer)"
  tg send "$E2E_REAL_PEER" "$marker" >/dev/null || return 1
  e2e_mutation_pause
  history="$(e2e_recent_history "$E2E_REAL_PEER" 20)" || return 1
  id="$(e2e_extract_message_id "$history" "$marker")"
  if [ -z "$id" ]; then
    echo "real peer marker not found: $marker" >&2
    echo "$history"
    return 1
  fi
  e2e_register_message "$E2E_REAL_PEER" "$id" "$marker"
  tg mark-read "$E2E_REAL_PEER" >/dev/null || return 1
  tg delete "$E2E_REAL_PEER" "$id" >/dev/null
}

manual_username_cycle() {
  if [ -z "${E2E_USERNAME_TEST_NAME:-}" ]; then
    e2e_skip_step "E2E_USERNAME_TEST_NAME is not set"
    return 77
  fi
  if ! e2e_confirm "Change public username to @$E2E_USERNAME_TEST_NAME and then clear it?"; then
    e2e_skip_step "operator declined"
    return 77
  fi
  tg username set "$E2E_USERNAME_TEST_NAME" >/dev/null || return 1
  e2e_mutation_pause
  tg username clear >/dev/null
}

manual_heartbeat_at() {
  if [ -z "${E2E_HEARTBEAT_PEER:-}" ] || [ -z "${E2E_HEARTBEAT_AT:-}" ]; then
    e2e_skip_step "E2E_HEARTBEAT_PEER and E2E_HEARTBEAT_AT are required"
    return 77
  fi
  local text="${E2E_HEARTBEAT_TEXT:-$(e2e_marker heartbeat-at)}"
  if ! e2e_confirm "Schedule an uncancellable Telegram send to $E2E_HEARTBEAT_PEER at $E2E_HEARTBEAT_AT?"; then
    e2e_skip_step "operator declined"
    return 77
  fi
  tg heartbeat plan "$E2E_HEARTBEAT_PEER" --at "$E2E_HEARTBEAT_AT" --template "$text" >/dev/null
}

manual_moderate_enforce() {
  if [ "${E2E_RUN_MODERATE_ENFORCE:-0}" != "1" ]; then
    e2e_skip_step "E2E_RUN_MODERATE_ENFORCE=1 is not set"
    return 77
  fi
  if ! e2e_confirm "Run moderate --enforce for $SERVICE_SECONDS seconds?"; then
    e2e_skip_step "operator declined"
    return 77
  fi
  e2e_run_for_seconds "$SERVICE_SECONDS" tg moderate --enforce
}

manual_ghostwrite_enforce() {
  if [ "${E2E_RUN_GHOSTWRITE_ENFORCE:-0}" != "1" ]; then
    e2e_skip_step "E2E_RUN_GHOSTWRITE_ENFORCE=1 is not set"
    return 77
  fi
  if ! e2e_confirm "Run ghostwrite --enforce for $SERVICE_SECONDS seconds?"; then
    e2e_skip_step "operator declined"
    return 77
  fi
  e2e_run_for_seconds "$SERVICE_SECONDS" tg ghostwrite --enforce
}

manual_heartbeat_run() {
  if [ "${E2E_RUN_HEARTBEAT:-0}" != "1" ]; then
    e2e_skip_step "E2E_RUN_HEARTBEAT=1 is not set"
    return 77
  fi
  if ! e2e_confirm "Run heartbeat run for $SERVICE_SECONDS seconds?"; then
    e2e_skip_step "operator declined"
    return 77
  fi
  e2e_run_for_seconds "$SERVICE_SECONDS" tg heartbeat run
}

manual_agent() {
  if [ "${E2E_RUN_AGENT:-0}" != "1" ]; then
    e2e_skip_step "E2E_RUN_AGENT=1 is not set"
    return 77
  fi
  if ! e2e_confirm "Run agent for $SERVICE_SECONDS seconds?"; then
    e2e_skip_step "operator declined"
    return 77
  fi
  e2e_run_for_seconds "$SERVICE_SECONDS" tg agent
}

manual_worker() {
  if [ -z "${E2E_FACTORY_URL:-}" ]; then
    e2e_skip_step "E2E_FACTORY_URL is not set"
    return 77
  fi
  if ! e2e_confirm "Run worker against E2E_FACTORY_URL=$E2E_FACTORY_URL for $SERVICE_SECONDS seconds?"; then
    e2e_skip_step "operator declined"
    return 77
  fi
  e2e_run_for_seconds "$SERVICE_SECONDS" tg worker --factory-url "$E2E_FACTORY_URL"
}

manual_serve() {
  if [ "${E2E_RUN_SERVE:-0}" != "1" ]; then
    e2e_skip_step "E2E_RUN_SERVE=1 is not set"
    return 77
  fi
  local port="${E2E_SERVE_PORT:-8090}"
  if ! e2e_confirm "Run serve on localhost:$port for $SERVICE_SECONDS seconds?"; then
    e2e_skip_step "operator declined"
    return 77
  fi
  e2e_run_for_seconds "$SERVICE_SECONDS" tg serve --port "$port"
}

manual_tui() {
  if [ "${E2E_RUN_TUI:-0}" != "1" ]; then
    e2e_skip_step "E2E_RUN_TUI=1 is not set"
    return 77
  fi
  if ! e2e_confirm "Launch tui in the foreground? Exit it manually to continue."; then
    e2e_skip_step "operator declined"
    return 77
  fi
  tg tui </dev/tty >/dev/tty 2>&1
}

manual_logout() {
  if [ -z "${E2E_DESTROY_PROFILE:-}" ]; then
    e2e_skip_step "E2E_DESTROY_PROFILE is not set"
    return 77
  fi
  if ! e2e_confirm "Log out and delete session profile '$E2E_DESTROY_PROFILE'?"; then
    e2e_skip_step "operator declined"
    return 77
  fi
  "$E2E_TG_BIN" --profile "$E2E_DESTROY_PROFILE" logout --yes
}

manual_profiles_remove() {
  if [ -z "${E2E_REMOVE_PROFILE:-}" ]; then
    e2e_skip_step "E2E_REMOVE_PROFILE is not set"
    return 77
  fi
  if ! e2e_confirm "Delete local session file for profile '$E2E_REMOVE_PROFILE'?"; then
    e2e_skip_step "operator declined"
    return 77
  fi
  tg profiles remove "$E2E_REMOVE_PROFILE" --yes
}

e2e_step "real dialog send/mark-read/delete" manual_real_peer_send_delete
e2e_step "username set/clear public identity cycle" manual_username_cycle
e2e_step "heartbeat --at scheduled send" manual_heartbeat_at
e2e_step "moderate --enforce timed run" manual_moderate_enforce
e2e_step "ghostwrite --enforce timed run" manual_ghostwrite_enforce
e2e_step "heartbeat run timed run" manual_heartbeat_run
e2e_step "agent timed run" manual_agent
e2e_step "worker timed run" manual_worker
e2e_step "serve timed run" manual_serve
e2e_step "tui foreground smoke" manual_tui
e2e_step "logout destructive profile check" manual_logout
e2e_step "profiles remove destructive local check" manual_profiles_remove

e2e_summary
