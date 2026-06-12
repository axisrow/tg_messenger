#!/usr/bin/env bash
# Safe optional real-CLI checks. This script is called by run_safe.sh, but
# env-gated checks SKIP when their prerequisites are missing.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

e2e_init
e2e_require_creds
e2e_require_saved_id_confirmed

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tg-messenger-e2e-optional.XXXXXX")"
SAVED_PEER="$E2E_SAVED_ID"

cleanup_all() {
  e2e_stop_registered_backgrounds
  e2e_cleanup_created_messages
  rm -rf "$TMP_DIR"
}
trap cleanup_all EXIT
trap 'cleanup_all; exit 130' INT
trap 'cleanup_all; exit 143' TERM

e2e_require_services_enabled() {
  if [ "${E2E_RUN_SERVICES:-0}" != "1" ]; then
    e2e_skip_step "set E2E_RUN_SERVICES=1 to run timed local service checks"
    return 77
  fi
}

e2e_require_llm_allowed() {
  e2e_require_setting TG_AGENT_MODEL || return 77
  if [ "${E2E_ALLOW_LLM:-0}" != "1" ]; then
    e2e_skip_step "set E2E_ALLOW_LLM=1 to allow sending dialog context to the configured LLM"
    return 77
  fi
}

step_serve_http() {
  local port="${E2E_SERVE_PORT:-18090}"
  local log="$TMP_DIR/serve.log"
  local pid
  local status

  e2e_require_services_enabled || return 77
  e2e_start_background serve "$log" tg serve --host 127.0.0.1 --port "$port"
  pid="$E2E_LAST_BG_PID"
  e2e_wait_for_http "http://127.0.0.1:$port/login" 30
  status=$?
  e2e_stop_background_pid "$pid"
  if [ "$status" -eq 77 ]; then
    return 77
  fi
  if [ "$status" -ne 0 ]; then
    echo "serve did not return HTTP 200/401 from /login" >&2
    cat "$log"
    return 1
  fi
}

step_chat_repl_send() {
  local marker
  local log="$TMP_DIR/chat-send.log"
  local history
  local id

  marker="$(e2e_marker chat-send)"
  printf '%s\n' "$marker" | tg chat "$SAVED_PEER" >"$log" 2>&1 || {
    cat "$log"
    return 1
  }
  e2e_mutation_pause
  history="$(e2e_recent_history "$SAVED_PEER" 20)" || return 1
  id="$(e2e_extract_message_id "$history" "$marker")"
  if [ -z "$id" ]; then
    echo "chat REPL marker was not found: $marker" >&2
    echo "$history"
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$id" "$marker"
  echo "chat message id: $id"
}

step_chat_repl_react() {
  local marker
  local target_id
  local log="$TMP_DIR/chat-react.log"

  marker="$(e2e_marker chat-react)"
  target_id="$(e2e_send_marker_to_peer "$SAVED_PEER" "$marker" 20)" || return 1
  e2e_register_message "$SAVED_PEER" "$target_id" "$marker"
  if ! printf '/react %s %s\n' "$target_id" "${E2E_REACTION_EMOTICON:-👍}" |
    tg chat "$SAVED_PEER" >"$log" 2>&1; then
    if grep -Eiq 'reaction|premium|forbidden|reject|not allowed|saved messages|emoji' "$log"; then
      e2e_skip_step "Telegram rejected chat /react for Saved Messages"
      return 77
    fi
    cat "$log"
    return 1
  fi
  e2e_mutation_pause
}

step_suggest_dry_run() {
  local output="$TMP_DIR/suggest.txt"
  e2e_require_llm_allowed || return 77
  e2e_require_env E2E_SUGGEST_DM || return 77
  tg suggest "$E2E_SUGGEST_DM" >"$output" || return 1
  if [ ! -s "$output" ]; then
    echo "suggest produced empty output" >&2
    return 1
  fi
}

step_suggest_learn() {
  local output="$TMP_DIR/suggest-learn.txt"
  e2e_require_llm_allowed || return 77
  e2e_require_env E2E_SUGGEST_DM || return 77
  if [ "${E2E_SUGGEST_LEARN:-0}" != "1" ]; then
    e2e_skip_step "set E2E_SUGGEST_LEARN=1 to run suggest --learn"
    return 77
  fi
  tg suggest "$E2E_SUGGEST_DM" --learn >"$output" || return 1
  grep -F "learned style profile" "$output" >/dev/null || {
    cat "$output"
    return 1
  }
}

step_suggest_send_saved() {
  local before_history
  local before_id
  local after_history
  local sent_id

  e2e_require_llm_allowed || return 77
  if [ "${E2E_SUGGEST_SEND:-0}" != "1" ]; then
    e2e_skip_step "set E2E_SUGGEST_SEND=1 to run suggest --send"
    return 77
  fi
  e2e_require_env E2E_SUGGEST_DM || return 77
  if [ "$E2E_SUGGEST_DM" != "$SAVED_PEER" ]; then
    e2e_skip_step "suggest --send is only automated when E2E_SUGGEST_DM equals E2E_SAVED_ID"
    return 77
  fi

  before_history="$(e2e_recent_history "$SAVED_PEER" 1)" || return 1
  before_id="$(e2e_extract_first_message_id "$before_history")"
  tg suggest "$SAVED_PEER" --send >/dev/null || return 1
  e2e_mutation_pause
  after_history="$(e2e_recent_history "$SAVED_PEER" 3)" || return 1
  sent_id="$(e2e_extract_first_message_id "$after_history")"
  if [ -z "$sent_id" ] || [ "$sent_id" = "$before_id" ]; then
    echo "could not identify suggest --send message id" >&2
    echo "$after_history"
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$sent_id" ""
}

step_moderate_dry_run() {
  local log="$TMP_DIR/moderate.log"
  local pid

  e2e_require_services_enabled || return 77
  e2e_start_background moderate "$log" tg moderate
  pid="$E2E_LAST_BG_PID"
  if ! e2e_wait_for_file_pattern "$log" "Moderating (dry-run)" 20; then
    e2e_stop_background_pid "$pid"
    echo "moderate dry-run did not reach startup line" >&2
    cat "$log"
    return 1
  fi
  e2e_stop_background_pid "$pid"
}

step_ghostwrite_dry_run() {
  local log="$TMP_DIR/ghostwrite.log"
  local pid

  e2e_require_services_enabled || return 77
  e2e_require_llm_allowed || return 77
  e2e_start_background ghostwrite "$log" tg ghostwrite
  pid="$E2E_LAST_BG_PID"
  if ! e2e_wait_for_file_pattern "$log" "Ghostwriting (dry-run)" 20; then
    e2e_stop_background_pid "$pid"
    echo "ghostwrite dry-run did not reach startup line" >&2
    cat "$log"
    return 1
  fi
  e2e_stop_background_pid "$pid"
}

step_heartbeat_run_startup() {
  local plans
  local log="$TMP_DIR/heartbeat-run.log"
  local pid

  e2e_require_services_enabled || return 77
  plans="$(tg heartbeat list)" || return 1
  if ! printf '%s\n' "$plans" | grep -Fx "No plans." >/dev/null; then
    e2e_skip_step "heartbeat run skipped because stored plans exist"
    return 77
  fi
  e2e_start_background heartbeat-run "$log" tg heartbeat run
  pid="$E2E_LAST_BG_PID"
  if ! e2e_wait_for_file_pattern "$log" "Heartbeat scheduler running" 20; then
    e2e_stop_background_pid "$pid"
    echo "heartbeat run did not reach startup line" >&2
    cat "$log"
    return 1
  fi
  e2e_stop_background_pid "$pid"
}

e2e_step "serve localhost /login HTTP assertion" step_serve_http
e2e_step "chat REPL sends a Saved Messages line" step_chat_repl_send
e2e_step "chat REPL /react best-effort" step_chat_repl_react
e2e_step "suggest dry-run" step_suggest_dry_run
e2e_step "suggest --learn optional" step_suggest_learn
e2e_step "suggest --send to Saved Messages optional" step_suggest_send_saved
e2e_step "moderate dry-run timed startup" step_moderate_dry_run
e2e_step "ghostwrite dry-run timed startup" step_ghostwrite_dry_run
e2e_step "heartbeat run startup with no stored plans" step_heartbeat_run_startup

e2e_summary
