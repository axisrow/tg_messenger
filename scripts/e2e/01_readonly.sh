#!/usr/bin/env bash
# Tier 1: read-only smoke checks. Safe to run periodically.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

e2e_init
e2e_require_creds
e2e_require_saved_id

TMP_DOWNLOAD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/tg-messenger-e2e-download.XXXXXX")"
trap 'rm -rf "$TMP_DOWNLOAD_DIR"' EXIT

step_help_root() { tg --help >/dev/null; }
step_help_profiles() { tg profiles --help >/dev/null; }
step_help_moderate_rules() { tg moderate-rules --help >/dev/null; }
step_help_heartbeat() { tg heartbeat --help >/dev/null; }
step_help_username() { tg username --help >/dev/null; }
step_help_ghostwrite_dialogs() { tg ghostwrite-dialogs --help >/dev/null; }
step_help_core_commands() {
  tg dialogs --help >/dev/null &&
    tg read --help >/dev/null &&
    tg send --help >/dev/null &&
    tg react --help >/dev/null &&
    tg chat --help >/dev/null &&
    tg serve --help >/dev/null
}

step_profiles() { tg profiles >/dev/null; }
step_dialogs_dm() { tg dialogs >/dev/null; }
step_dialogs_groups() { tg dialogs --groups >/dev/null; }
step_dialogs_find() {
  local query="${E2E_DIALOG_QUERY:-$E2E_SAVED_ID}"
  tg dialogs --find "$query" >/dev/null
}
step_read_saved() { tg read "$E2E_SAVED_ID" --limit 5 >/dev/null; }
step_search_saved() {
  if [ -z "${E2E_SEARCH_QUERY:-}" ]; then
    e2e_skip_step "E2E_SEARCH_QUERY is not set"
    return 77
  fi
  tg search "$E2E_SAVED_ID" "$E2E_SEARCH_QUERY" --limit 5 >/dev/null
}
step_read_download() {
  tg read "$E2E_SAVED_ID" --limit 5 --download "$TMP_DOWNLOAD_DIR" >/dev/null
}
step_username_suggest() {
  local base="${E2E_USERNAME_BASE:-e2esmoke}"
  tg username suggest "$base" --limit 1 >/dev/null
}
step_heartbeat_list() { tg heartbeat list >/dev/null; }
step_moderate_rules_list() { tg moderate-rules list >/dev/null; }
step_ghostwrite_dialogs_list() { tg ghostwrite-dialogs list >/dev/null; }

e2e_step "root help" step_help_root
e2e_step "profiles help" step_help_profiles
e2e_step "moderate-rules help" step_help_moderate_rules
e2e_step "heartbeat help" step_help_heartbeat
e2e_step "username help" step_help_username
e2e_step "ghostwrite-dialogs help" step_help_ghostwrite_dialogs
e2e_step "core command help" step_help_core_commands

e2e_step "profiles list" step_profiles
e2e_step "dialogs dm" step_dialogs_dm
e2e_step "dialogs groups" step_dialogs_groups
e2e_step "dialogs find" step_dialogs_find
e2e_step "read saved messages" step_read_saved
e2e_step "search saved messages" step_search_saved
e2e_step "read with media download dir" step_read_download
e2e_step "username suggest low-limit" step_username_suggest
e2e_step "heartbeat list" step_heartbeat_list
e2e_step "moderate-rules list" step_moderate_rules_list
e2e_step "ghostwrite-dialogs list" step_ghostwrite_dialogs_list

e2e_summary
