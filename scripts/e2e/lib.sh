#!/usr/bin/env bash
# Shared helpers for manual real-CLI E2E smoke tests.
#
# These scripts intentionally drive the installed `tg-messenger` command. They
# must not import Python internals or be wired into pytest/CI.

set -uo pipefail

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  echo "scripts/e2e/lib.sh is a helper; source it from an E2E script." >&2
  exit 2
fi

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_PROJECT_ROOT="$(cd "$E2E_DIR/../.." && pwd)"

E2E_TG_BIN="${E2E_TG_BIN:-tg-messenger}"
E2E_PROFILE="${E2E_PROFILE:-default}"
E2E_MUTATION_SLEEP="${E2E_MUTATION_SLEEP:-2}"
E2E_VERBOSE="${E2E_VERBOSE:-0}"

E2E_PASS=0
E2E_FAIL=0
E2E_SKIP=0
E2E_STEP_INDEX=0
E2E_RUN_ID="${E2E_RUN_ID:-$(date +%Y%m%d%H%M%S)-$$}"

E2E_CREATED_PEERS=()
E2E_CREATED_IDS=()
E2E_CREATED_MARKERS=()

e2e_init() {
  cd "$E2E_PROJECT_ROOT" || exit 2
  if ! command -v "$E2E_TG_BIN" >/dev/null 2>&1; then
    e2e_die "cannot find '$E2E_TG_BIN' in PATH; install the project first"
  fi
}

e2e_die() {
  printf 'FAIL prerequisite: %s\n' "$*" >&2
  exit 1
}

e2e_pass() {
  E2E_PASS=$((E2E_PASS + 1))
  printf 'PASS %s\n' "$1"
}

e2e_fail() {
  E2E_FAIL=$((E2E_FAIL + 1))
  printf 'FAIL %s\n' "$1"
  if [ "${2:-}" != "" ]; then
    printf '%s\n' "$2" | tail -n 40 | sed 's/^/  /'
  fi
}

e2e_skip() {
  E2E_SKIP=$((E2E_SKIP + 1))
  printf 'SKIP %s\n' "$1"
  if [ "${2:-}" != "" ]; then
    printf '%s\n' "$2" | sed 's/^/  /'
  fi
}

e2e_step() {
  local name="$1"
  shift
  local output_file
  local status
  E2E_STEP_INDEX=$((E2E_STEP_INDEX + 1))
  output_file="$(mktemp "${TMPDIR:-/tmp}/tg-messenger-e2e-step.XXXXXX")"

  "$@" >"$output_file" 2>&1
  status=$?

  if [ "$status" -eq 0 ]; then
    e2e_pass "$name"
    if [ "$E2E_VERBOSE" = "1" ] && [ -s "$output_file" ]; then
      sed 's/^/  /' "$output_file"
    fi
  elif [ "$status" -eq 77 ]; then
    e2e_skip "$name" "$(cat "$output_file")"
  else
    e2e_fail "$name" "$(cat "$output_file")"
  fi
  rm -f "$output_file"
}

e2e_summary() {
  printf '\nSummary: PASS=%s FAIL=%s SKIP=%s\n' "$E2E_PASS" "$E2E_FAIL" "$E2E_SKIP"
  if [ "$E2E_FAIL" -gt 0 ]; then
    return 1
  fi
  return 0
}

tg() {
  "$E2E_TG_BIN" --profile "$E2E_PROFILE" "$@"
}

e2e_have_dotenv_key() {
  local key="$1"
  [ -f "$E2E_PROJECT_ROOT/.env" ] && grep -Eq "^[[:space:]]*${key}[[:space:]]*=" "$E2E_PROJECT_ROOT/.env"
}

e2e_have_creds() {
  if [ -n "${TG_API_ID:-}" ] && [ -n "${TG_API_HASH:-}" ]; then
    return 0
  fi
  e2e_have_dotenv_key TG_API_ID && e2e_have_dotenv_key TG_API_HASH
}

e2e_require_creds() {
  if ! e2e_have_creds; then
    e2e_die "TG_API_ID/TG_API_HASH are required in env or .env"
  fi
}

e2e_require_saved_id() {
  if [ -z "${E2E_SAVED_ID:-}" ]; then
    e2e_die "E2E_SAVED_ID is required; use your own numeric Telegram user id"
  fi
  case "$E2E_SAVED_ID" in
    *[!0-9]*)
      e2e_die "E2E_SAVED_ID must be a positive numeric self-dialog id, got '$E2E_SAVED_ID'"
      ;;
  esac
}

e2e_require_saved_id_confirmed() {
  e2e_require_saved_id
  if [ "${E2E_SAVED_ID_CONFIRM:-}" != "$E2E_SAVED_ID" ]; then
    e2e_die "E2E_SAVED_ID_CONFIRM must equal E2E_SAVED_ID after you verify it is your own Saved Messages/self-dialog id"
  fi
}

e2e_marker() {
  local step="$1"
  printf 'e2e-%s-%s' "$E2E_RUN_ID" "$step"
}

e2e_mutation_pause() {
  sleep "$E2E_MUTATION_SLEEP"
}

e2e_extract_message_id() {
  local text="$1"
  local marker="$2"
  # Coupled to `tg-messenger read` lines like: `→ [123] e2e-marker`.
  printf '%s\n' "$text" | awk -v marker="$marker" '
    index($0, marker) > 0 {
      if (match($0, /\[[0-9]+\]/)) {
        print substr($0, RSTART + 1, RLENGTH - 2)
        exit
      }
    }
  '
}

e2e_extract_message_id_except() {
  local text="$1"
  local marker="$2"
  local excluded_id="$3"
  printf '%s\n' "$text" | awk -v marker="$marker" -v excluded_id="$excluded_id" '
    index($0, marker) > 0 {
      if (match($0, /\[[0-9]+\]/)) {
        id = substr($0, RSTART + 1, RLENGTH - 2)
        if (id != excluded_id) {
          print id
          exit
        }
      }
    }
  '
}

e2e_register_message() {
  local peer="$1"
  local id="$2"
  local marker="${3:-}"
  if [ -n "$peer" ] && [ -n "$id" ]; then
    E2E_CREATED_PEERS+=("$peer")
    E2E_CREATED_IDS+=("$id")
    E2E_CREATED_MARKERS+=("$marker")
  fi
}

e2e_delete_registered_messages() {
  local ok=0
  local i
  if [ "${#E2E_CREATED_IDS[@]}" -eq 0 ]; then
    echo "no created messages registered"
    return 0
  fi
  for i in "${!E2E_CREATED_IDS[@]}"; do
    if [ -z "${E2E_CREATED_IDS[$i]}" ]; then
      continue
    fi
    if tg delete "${E2E_CREATED_PEERS[$i]}" "${E2E_CREATED_IDS[$i]}"; then
      echo "deleted ${E2E_CREATED_PEERS[$i]}:${E2E_CREATED_IDS[$i]}"
      E2E_CREATED_IDS[$i]=""
      e2e_mutation_pause
    else
      echo "could not delete ${E2E_CREATED_PEERS[$i]}:${E2E_CREATED_IDS[$i]}" >&2
      ok=1
    fi
  done
  return "$ok"
}

e2e_cleanup_created_messages() {
  local i
  if [ "${#E2E_CREATED_IDS[@]}" -eq 0 ]; then
    return 0
  fi
  for i in "${!E2E_CREATED_IDS[@]}"; do
    if [ -n "${E2E_CREATED_IDS[$i]}" ]; then
      tg delete "${E2E_CREATED_PEERS[$i]}" "${E2E_CREATED_IDS[$i]}" >/dev/null 2>&1 || true
    fi
  done
}

e2e_clear_registered_messages() {
  E2E_CREATED_PEERS=()
  E2E_CREATED_IDS=()
  E2E_CREATED_MARKERS=()
}

e2e_recent_history() {
  local peer="$1"
  local limit="${2:-20}"
  tg read "$peer" --limit "$limit"
}

e2e_skip_step() {
  echo "$*"
  return 77
}
