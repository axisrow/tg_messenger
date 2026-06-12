#!/usr/bin/env bash
# Tier 2: safe reversible mutations confined to Saved Messages.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

e2e_init
e2e_require_creds
e2e_require_saved_id_confirmed

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tg-messenger-e2e-mutations.XXXXXX")"
SAVED_PEER="$E2E_SAVED_ID"
BASE_ID=""
REPLY_ID=""
FORWARD_SOURCE_ID=""
FORWARD_COPY_ID=""
FORWARD_LIST_SOURCE_ID=""
FORWARD_LIST_REPLY_ID=""
FORWARD_LIST_COPY_ID_1=""
FORWARD_LIST_COPY_ID_2=""
FILE_ID=""
AS_FILE_ID=""
CAPTION_FILE_ID=""
FOR_ME_DELETE_ID=""
EDIT_MARKER=""
DELETE_CREATED_OK=0
SAVED_MARKERS=()
REACTION_CHAT_PID=""
REACTION_TAIL_PID=""

cleanup_reaction_processes() {
  if [ -n "$REACTION_CHAT_PID" ]; then
    kill "$REACTION_CHAT_PID" >/dev/null 2>&1 || true
    wait "$REACTION_CHAT_PID" >/dev/null 2>&1 || true
    REACTION_CHAT_PID=""
  fi
  if [ -n "$REACTION_TAIL_PID" ]; then
    kill "$REACTION_TAIL_PID" >/dev/null 2>&1 || true
    wait "$REACTION_TAIL_PID" >/dev/null 2>&1 || true
    REACTION_TAIL_PID=""
  fi
}

cleanup_all() {
  cleanup_reaction_processes
  e2e_cleanup_created_messages
  rm -rf "$TMP_DIR"
}
trap cleanup_all EXIT
trap 'cleanup_all; exit 130' INT
trap 'cleanup_all; exit 143' TERM

remember_saved_marker() {
  SAVED_MARKERS+=("$1")
}

send_marker_to_peer() {
  local peer="$1"
  local marker="$2"
  local limit="${3:-15}"
  local history
  local id

  tg send "$peer" "$marker" >/dev/null || return 1
  e2e_mutation_pause
  history="$(e2e_recent_history "$peer" "$limit")" || return 1
  id="$(e2e_extract_message_id "$history" "$marker")"
  if [ -z "$id" ]; then
    echo "sent marker was not found in recent history: $marker" >&2
    echo "$history"
    return 1
  fi
  printf '%s\n' "$id"
}

step_send_read_verify() {
  local marker
  marker="$(e2e_marker send)"
  if ! BASE_ID="$(send_marker_to_peer "$SAVED_PEER" "$marker" 20)" || [ -z "$BASE_ID" ]; then
    echo "failed to send or recover base message id" >&2
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$BASE_ID" "$marker"
  remember_saved_marker "$marker"
  echo "base message id: $BASE_ID"
}

step_reply_to_base() {
  local marker
  if [ -z "$BASE_ID" ]; then
    echo "base message id is missing" >&2
    return 1
  fi
  marker="$(e2e_marker reply)"
  tg send "$SAVED_PEER" "$marker" --reply-to "$BASE_ID" >/dev/null || return 1
  e2e_mutation_pause
  local history
  history="$(e2e_recent_history "$SAVED_PEER" 20)" || return 1
  REPLY_ID="$(e2e_extract_message_id "$history" "$marker")"
  if [ -z "$REPLY_ID" ]; then
    echo "reply marker was not found: $marker" >&2
    echo "$history"
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$REPLY_ID" "$marker"
  remember_saved_marker "$marker"
  echo "reply message id: $REPLY_ID"
}

step_edit_base() {
  if [ -z "$BASE_ID" ]; then
    echo "base message id is missing" >&2
    return 1
  fi
  EDIT_MARKER="$(e2e_marker edited)"
  tg edit "$SAVED_PEER" "$BASE_ID" "$EDIT_MARKER" >/dev/null || return 1
  e2e_mutation_pause
  local history
  history="$(e2e_recent_history "$SAVED_PEER" 20)" || return 1
  if ! printf '%s\n' "$history" | grep -F -- "$EDIT_MARKER" >/dev/null; then
    echo "edited marker was not found: $EDIT_MARKER" >&2
    echo "$history"
    return 1
  fi
  remember_saved_marker "$EDIT_MARKER"
}

step_react_best_effort() {
  local peer="$SAVED_PEER"
  local target_id="$BASE_ID"

  if [ -z "$target_id" ]; then
    echo "reaction target id is missing" >&2
    return 1
  fi

  if ! tg react "$peer" "$target_id" "${E2E_REACTION_EMOTICON:-👍}"; then
    e2e_skip_step "Telegram rejected reaction for peer $peer; Saved Messages may restrict reactions"
    return 77
  fi
  e2e_mutation_pause
}

step_forward_saved_to_saved() {
  local marker
  local history
  marker="$(e2e_marker forward-source)"
  if ! FORWARD_SOURCE_ID="$(send_marker_to_peer "$SAVED_PEER" "$marker" 20)" || [ -z "$FORWARD_SOURCE_ID" ]; then
    echo "failed to send or recover forward source id" >&2
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$FORWARD_SOURCE_ID" "$marker"
  remember_saved_marker "$marker"

  tg forward "$SAVED_PEER" "$FORWARD_SOURCE_ID" "$SAVED_PEER" >/dev/null || return 1
  e2e_mutation_pause
  history="$(e2e_recent_history "$SAVED_PEER" 30)" || return 1
  FORWARD_COPY_ID="$(e2e_extract_message_id_except "$history" "$marker" "$FORWARD_SOURCE_ID")"
  if [ -z "$FORWARD_COPY_ID" ]; then
    echo "forwarded copy was not found for marker: $marker" >&2
    echo "$history"
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$FORWARD_COPY_ID" "$marker"
  echo "forward copy id: $FORWARD_COPY_ID"
}

step_forward_saved_list_to_saved() {
  local marker
  local reply_marker
  local history
  marker="$(e2e_marker forward-list-source)"
  if ! FORWARD_LIST_SOURCE_ID="$(send_marker_to_peer "$SAVED_PEER" "$marker" 20)" ||
    [ -z "$FORWARD_LIST_SOURCE_ID" ]; then
    echo "failed to send or recover forward list source id" >&2
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$FORWARD_LIST_SOURCE_ID" "$marker"
  remember_saved_marker "$marker"

  reply_marker="$(e2e_marker forward-list-reply)"
  tg send "$SAVED_PEER" "$reply_marker" --reply-to "$FORWARD_LIST_SOURCE_ID" >/dev/null || return 1
  e2e_mutation_pause
  history="$(e2e_recent_history "$SAVED_PEER" 30)" || return 1
  FORWARD_LIST_REPLY_ID="$(e2e_extract_message_id "$history" "$reply_marker")"
  if [ -z "$FORWARD_LIST_REPLY_ID" ]; then
    echo "forward list reply source was not found: $reply_marker" >&2
    echo "$history"
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$FORWARD_LIST_REPLY_ID" "$reply_marker"
  remember_saved_marker "$reply_marker"

  tg forward "$SAVED_PEER" "$FORWARD_LIST_SOURCE_ID,$FORWARD_LIST_REPLY_ID" "$SAVED_PEER" >/dev/null ||
    return 1
  e2e_mutation_pause
  history="$(e2e_recent_history "$SAVED_PEER" 50)" || return 1
  FORWARD_LIST_COPY_ID_1="$(e2e_extract_message_id_except "$history" "$marker" "$FORWARD_LIST_SOURCE_ID")"
  FORWARD_LIST_COPY_ID_2="$(e2e_extract_message_id_except "$history" "$reply_marker" "$FORWARD_LIST_REPLY_ID")"
  if [ -z "$FORWARD_LIST_COPY_ID_1" ] || [ -z "$FORWARD_LIST_COPY_ID_2" ]; then
    echo "forwarded list copies were not found" >&2
    echo "$history"
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$FORWARD_LIST_COPY_ID_1" "$marker"
  e2e_register_message "$SAVED_PEER" "$FORWARD_LIST_COPY_ID_2" "$reply_marker"
}

step_send_file_with_caption() {
  local file="$TMP_DIR/e2e-file.txt"
  local marker
  local history
  marker="$(e2e_marker file-caption)"
  printf 'temporary e2e file: %s\n' "$marker" >"$file"
  tg send "$SAVED_PEER" "$marker" --file "$file" >/dev/null || return 1
  e2e_mutation_pause
  history="$(e2e_recent_history "$SAVED_PEER" 30)" || return 1
  FILE_ID="$(e2e_extract_message_id "$history" "$marker")"
  if [ -z "$FILE_ID" ]; then
    echo "file caption marker was not found: $marker" >&2
    echo "$history"
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$FILE_ID" "$marker"
  remember_saved_marker "$marker"
}

step_send_file_with_caption_option() {
  local file="$TMP_DIR/e2e-caption-option.txt"
  local marker
  local history
  marker="$(e2e_marker caption-option)"
  printf 'temporary e2e caption-option file: %s\n' "$marker" >"$file"
  tg send "$SAVED_PEER" --file "$file" --caption "$marker" >/dev/null || return 1
  e2e_mutation_pause
  history="$(e2e_recent_history "$SAVED_PEER" 30)" || return 1
  CAPTION_FILE_ID="$(e2e_extract_message_id "$history" "$marker")"
  if [ -z "$CAPTION_FILE_ID" ]; then
    echo "explicit --caption marker was not found: $marker" >&2
    echo "$history"
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$CAPTION_FILE_ID" "$marker"
  remember_saved_marker "$marker"
}

step_send_file_as_file() {
  local file="$TMP_DIR/e2e-as-file.txt"
  local marker
  local history
  marker="$(e2e_marker as-file)"
  printf 'temporary e2e as-file: %s\n' "$marker" >"$file"
  tg send "$SAVED_PEER" "$marker" --file "$file" --as-file >/dev/null || return 1
  e2e_mutation_pause
  history="$(e2e_recent_history "$SAVED_PEER" 30)" || return 1
  AS_FILE_ID="$(e2e_extract_message_id "$history" "$marker")"
  if [ -z "$AS_FILE_ID" ]; then
    echo "as-file marker was not found: $marker" >&2
    echo "$history"
    return 1
  fi
  e2e_register_message "$SAVED_PEER" "$AS_FILE_ID" "$marker"
  remember_saved_marker "$marker"
}

step_delete_for_me_saved() {
  local marker
  local history
  local after_history
  marker="$(e2e_marker delete-for-me)"
  if ! FOR_ME_DELETE_ID="$(send_marker_to_peer "$SAVED_PEER" "$marker" 20)" || [ -z "$FOR_ME_DELETE_ID" ]; then
    echo "failed to send or recover delete-for-me message id" >&2
    return 1
  fi
  history="$(e2e_recent_history "$SAVED_PEER" 20)" || return 1
  if ! printf '%s\n' "$history" | grep -F -- "$marker" >/dev/null; then
    echo "delete-for-me marker was not visible before deletion: $marker" >&2
    echo "$history"
    return 1
  fi
  tg delete "$SAVED_PEER" "$FOR_ME_DELETE_ID" --for-me >/dev/null || return 1
  e2e_mutation_pause
  after_history="$(e2e_recent_history "$SAVED_PEER" 20)" || return 1
  if printf '%s\n' "$after_history" | grep -F -- "$marker" >/dev/null; then
    echo "delete-for-me marker still visible: $marker" >&2
    echo "$after_history"
    return 1
  fi
}

step_mark_read_saved() {
  tg mark-read "$SAVED_PEER" >/dev/null
}

step_reaction_roundtrip() {
  local peer="$SAVED_PEER"
  local marker
  local target_id
  local history
  local chat_log="$TMP_DIR/chat-reactions.log"
  local stdin_fifo="$TMP_DIR/chat-stdin.fifo"

  marker="$(e2e_marker reaction-roundtrip)"
  tg send "$peer" "$marker" >/dev/null || return 1
  e2e_mutation_pause
  history="$(e2e_recent_history "$peer" 20)" || return 1
  target_id="$(e2e_extract_message_id "$history" "$marker")"
  if [ -z "$target_id" ]; then
    echo "round-trip target was not found: $marker" >&2
    echo "$history"
    return 1
  fi
  e2e_register_message "$peer" "$target_id" "$marker"
  remember_saved_marker "$marker"

  mkfifo "$stdin_fifo" || return 1
  tail -f /dev/null >"$stdin_fifo" &
  REACTION_TAIL_PID=$!
  tg chat "$peer" <"$stdin_fifo" >"$chat_log" 2>&1 &
  REACTION_CHAT_PID=$!
  sleep 3
  if ! kill -0 "$REACTION_CHAT_PID" >/dev/null 2>&1; then
    cleanup_reaction_processes
    echo "chat process exited before reaction was sent" >&2
    cat "$chat_log"
    return 1
  fi

  if ! tg react "$peer" "$target_id" "${E2E_REACTION_EMOTICON:-👍}" >/dev/null 2>&1; then
    cleanup_reaction_processes
    e2e_skip_step "Telegram rejected reaction round-trip for peer $peer"
    return 77
  fi

  sleep "${E2E_REACTION_WAIT:-8}"
  cleanup_reaction_processes

  if ! grep -F '* reaction [' "$chat_log" >/dev/null; then
    echo "chat output did not include a real reaction event" >&2
    cat "$chat_log"
    return 1
  fi
}

step_sqlite_moderate_rules() {
  local chat_id="-999$(date +%s)"
  local name="e2e-smoke-$E2E_RUN_ID"
  local rule_file="$TMP_DIR/moderation-rule.json"
  cat >"$rule_file" <<EOF
{
  "chat_id": $chat_id,
  "name": "$name",
  "enabled": true,
  "conditions": {
    "pattern": "e2e-never-match-$E2E_RUN_ID",
    "has_link": false,
    "is_forward": false,
    "from_new_member_within_sec": null,
    "max_messages_per_minute": null
  },
  "actions": {
    "delete": false,
    "mute_sec": null,
    "ban": false,
    "warn_text": null
  }
}
EOF
  local added=0
  local result=0
  if tg moderate-rules add "$rule_file" >/dev/null; then
    added=1
  else
    return 1
  fi
  tg moderate-rules list --chat "$chat_id" | grep -F "$name" >/dev/null || result=1
  if [ "$added" -eq 1 ]; then
    tg moderate-rules remove -- "$chat_id" "$name" >/dev/null || result=1
  fi
  return "$result"
}

step_sqlite_heartbeat() {
  local peer="999$(date +%s)"
  local marker
  local added=0
  local result=0
  marker="$(e2e_marker heartbeat)"
  if tg heartbeat plan "$peer" --interval 999999 --template "$marker" >/dev/null; then
    added=1
  else
    return 1
  fi
  tg heartbeat list | grep -F "$marker" >/dev/null || result=1
  if [ "$added" -eq 1 ]; then
    tg heartbeat remove "$peer" >/dev/null || result=1
  fi
  return "$result"
}

step_sqlite_ghostwrite() {
  local peer="998$(date +%s)"
  local added=0
  local result=0
  if tg ghostwrite-dialogs enable "$peer" >/dev/null; then
    added=1
  else
    return 1
  fi
  tg ghostwrite-dialogs list | grep -F "$peer" >/dev/null || result=1
  if [ "$added" -eq 1 ]; then
    tg ghostwrite-dialogs disable "$peer" >/dev/null || result=1
  fi
  return "$result"
}

step_delete_all_created() {
  e2e_delete_registered_messages && DELETE_CREATED_OK=1
}

step_verify_saved_markers_gone() {
  local history
  local marker
  if [ "${#SAVED_MARKERS[@]}" -eq 0 ]; then
    echo "no Saved Messages markers registered for cleanup verification"
    return 0
  fi
  history="$(e2e_recent_history "$SAVED_PEER" 40)" || return 1
  for marker in "${SAVED_MARKERS[@]}"; do
    if printf '%s\n' "$history" | grep -F -- "$marker" >/dev/null; then
      echo "marker still visible after cleanup: $marker" >&2
      echo "$history"
      return 1
    fi
  done
}

e2e_step "send marker to Saved Messages and recover id" step_send_read_verify
e2e_step "send reply in Saved Messages" step_reply_to_base
e2e_step "edit Saved Messages test message" step_edit_base
e2e_step "react best-effort" step_react_best_effort
e2e_step "forward Saved Messages message to Saved Messages" step_forward_saved_to_saved
e2e_step "forward Saved Messages id list to Saved Messages" step_forward_saved_list_to_saved
e2e_step "send generated file with caption" step_send_file_with_caption
e2e_step "send generated file with explicit --caption" step_send_file_with_caption_option
e2e_step "send generated file as document" step_send_file_as_file
e2e_step "delete Saved Messages message --for-me" step_delete_for_me_saved
e2e_step "mark Saved Messages read" step_mark_read_saved
e2e_step "reaction listen round-trip through chat" step_reaction_roundtrip
e2e_step "moderate-rules add/list/remove local CRUD" step_sqlite_moderate_rules
e2e_step "heartbeat plan/list/remove local CRUD" step_sqlite_heartbeat
e2e_step "ghostwrite-dialogs enable/list/disable local CRUD" step_sqlite_ghostwrite
e2e_step "delete all created Telegram messages" step_delete_all_created
if [ "$DELETE_CREATED_OK" -eq 1 ]; then
  e2e_clear_registered_messages
fi
e2e_step "verify Saved Messages markers are gone" step_verify_saved_markers_gone

e2e_summary
