#!/usr/bin/env bash
#
# run.sh — bring up everything the Personal Discord bot needs, then run it.
#
# Usage:
#   ./run.sh                 run preflight checks, start Ollama if needed, run the bot
#   ./run.sh --check         preflight only: report status, start nothing, exit 0/1
#   ./run.sh --install       (re)install Python dependencies, then run
#   ./run.sh --no-ollama     don't try to start Ollama (just report if unreachable)
#   ./run.sh --log-level L   set LOG_LEVEL for the bot (DEBUG, INFO, WARNING, ERROR)
#   ./run.sh --help          show this help
#
# What it takes care of:
#   * .venv           created and populated from src/requirements.txt on first run
#   * Deno            ~/.deno/bin added to PATH (yt-dlp needs it for YouTube's
#                     JS challenges; without it playback extraction fails)
#   * FFmpeg          verified (fatal if missing — all playback needs it)
#   * .env            verified present with a real DISCORD_TOKEN
#   * davey version   verified 0.1.x when discord.py is 2.7.x — davey 0.2.0
#                     silently breaks voice (see src/requirements.txt)
#   * Ollama          health-checked and started if down (systemd unit first,
#                     then a background `ollama serve`), model pull verified
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$ROOT/src"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
REQS="$SRC/requirements.txt"
ENV_FILE="$SRC/.env"
OLLAMA_LOG="$ROOT/ollama-serve.log"

MODE="run"          # run | check
FORCE_INSTALL=0
MANAGE_OLLAMA=1
LOG_LEVEL_ARG=""
ERRORS=0
WARNINGS=0

# ---------------------------------------------------------------- output ----
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RED=$'\033[31m'; C_YEL=$'\033[33m'; C_GRN=$'\033[32m'; C_CYN=$'\033[36m'; C_OFF=$'\033[0m'
else
  C_RED=""; C_YEL=""; C_GRN=""; C_CYN=""; C_OFF=""
fi

info() { printf '%s\n' "$*"; }
ok()   { printf '%s[ ok ]%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '%s[warn]%s %s\n' "$C_YEL" "$C_OFF" "$*"; WARNINGS=$((WARNINGS + 1)); }
fail() { printf '%s[fail]%s %s\n' "$C_RED" "$C_OFF" "$*"; ERRORS=$((ERRORS + 1)); }
err()  { printf '%s[err ]%s %s\n' "$C_RED" "$C_OFF" "$*"; }

usage() { sed -n '3,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

# --------------------------------------------------------------- options ----
while [ $# -gt 0 ]; do
  case "$1" in
    -c|--check)      MODE="check" ;;
    -i|--install)    FORCE_INSTALL=1 ;;
    --no-ollama)     MANAGE_OLLAMA=0 ;;
    -l|--log-level)  shift; LOG_LEVEL_ARG="${1:-}" ;;
    -h|--help)       usage; exit 0 ;;
    *) err_msg="unknown option: $1"; printf '%s\n' "$err_msg" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
if [ -n "$LOG_LEVEL_ARG" ]; then
  export LOG_LEVEL="$LOG_LEVEL_ARG"
fi

# ----------------------------------------------------------------- utils ----
# Read KEY from .env (default if absent/empty). Quotes are stripped.
env_get() {
  local key="$1" def="${2:-}" val=""
  if [ -f "$ENV_FILE" ]; then
    val="$(sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1)"
    val="${val%\"}"; val="${val#\"}"
    val="${val%\'}"; val="${val#\'}"
  fi
  printf '%s' "${val:-$def}"
}

OLLAMA_HOST="$(env_get OLLAMA_HOST 'http://localhost:11434')"
OLLAMA_MODEL="$(env_get OLLAMA_MODEL 'llama3.2')"

# Query the Ollama HTTP API. Args: <path> [model-name-to-require]
ollama_api() {
  python3 - "${OLLAMA_HOST%/}$1" "${2:-}" <<'PY'
import json, sys, urllib.request

url, model = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(url, timeout=3) as resp:
        data = json.load(resp)
except Exception:
    sys.exit(1)
if not model:
    sys.exit(0)
names = [m.get("name", "") for m in data.get("models", [])]
sys.exit(0 if any(n == model or n.startswith(model + ":") for n in names) else 1)
PY
}

wait_ollama() {  # wait_ollama <seconds>
  local timeout="$1" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if ollama_api /api/tags; then return 0; fi
    sleep 1
    waited=$((waited + 1))
  done
  ollama_api /api/tags
}

host_is_local() {
  case "$OLLAMA_HOST" in
    http://localhost*|http://127.0.0.1*|http://[::1]*) return 0 ;;
    *) return 1 ;;
  esac
}

# ------------------------------------------------------------- preflight ----
check_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found (need Python 3.11+) — install it first"
    return 1
  fi
  if python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)'; then
    ok "python3 $(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  else
    fail "python3 is too old ($(python3 --version 2>&1 | awk '{print $2}')) — need 3.11+"
    return 1
  fi
}

ensure_venv() {
  if [ -x "$PY" ]; then
    if [ "$FORCE_INSTALL" -eq 1 ] && [ "$MODE" = "run" ]; then
      info "updating dependencies from requirements.txt ..."
      "$PY" -m pip install --disable-pip-version-check -r "$REQS"
      ok "dependencies up to date"
    fi
    return 0
  fi
  if [ "$MODE" = "check" ]; then
    fail "virtualenv missing ($VENV) — run ./run.sh --install"
    return 1
  fi
  info "creating virtualenv at $VENV ..."
  python3 -m venv "$VENV"
  info "installing dependencies from requirements.txt ..."
  "$PY" -m pip install --disable-pip-version-check -r "$REQS"
  ok "virtualenv ready ($(basename "$PY"))"
}

# Imports every requirement + guards the discord.py↔davey API pairing.
check_python_deps() {
  [ -x "$PY" ] || { fail "cannot check packages: virtualenv missing"; return 1; }
  local out rc=0
  out="$(
    "$PY" - <<'PY' 2>&1
import importlib
import importlib.metadata
import importlib.util
import sys

# Every package the bot imports at runtime.
REQUIRED = ("discord", "davey", "nacl", "yt_dlp", "ollama", "spotipy", "dotenv")
missing = [name for name in REQUIRED if importlib.util.find_spec(name) is None]
if missing:
    print("missing packages: " + ", ".join(missing))
    sys.exit(1)

import discord
import davey

# discord.py 2.7.x speaks the davey 0.1.x API. davey 0.2.0 moved MLS key
# installation behind prepare_transition()/execute_transition() which
# discord.py 2.7.x never calls -> handshake completes but audio is silent.
d_version = getattr(discord, "__version__", "?")
y_version = getattr(davey, "__version__", "?")
if d_version.startswith("2.7.") and not y_version.startswith("0.1."):
    print(f"discord.py {d_version} needs davey 0.1.x, but davey {y_version} is installed"
          " (voice would be silent)")
    sys.exit(2)

print(f"discord.py {d_version}, davey {y_version},"
      f" yt-dlp {importlib.metadata.version('yt-dlp')}")
PY
  )" || rc=$?
  if [ "$rc" -eq 1 ]; then
    fail "$out"
    info "       fix: ./run.sh --install"
    return 1
  fi
  if [ "$rc" -eq 2 ]; then
    fail "$out"
    info "       fix: $PY -m pip install 'davey==0.1.7'"
    return 1
  fi
  ok "python packages: $out"
}

check_ffmpeg() {
  if ! command -v ffmpeg >/dev/null 2>&1; then
    fail "ffmpeg not found — required for all playback (sudo apt install ffmpeg)"
    return 1
  fi
  ok "ffmpeg $(ffmpeg -version 2>/dev/null | sed -n 's/.*version \([0-9][0-9.]*\).*/\1/p' | head -n 1)"
}

check_deno() {
  if ! command -v deno >/dev/null 2>&1; then
    warn "deno not found — YouTube extraction will fail (yt-dlp JS challenges)"
    info "       fix: curl -fsSL https://deno.land/install.sh | sh  (adds ~/.deno/bin)"
    return 1
  fi
  ok "deno $(deno --version 2>/dev/null | sed -n '1s/^deno \([^ ]*\).*/\1/p')"
}

check_env() {
  if [ ! -f "$ENV_FILE" ]; then
    fail ".env missing — run: cp $SRC/.env.example $ENV_FILE  (then fill it in)"
    return 1
  fi
  local token
  token="$(env_get DISCORD_TOKEN)"
  if [ -z "$token" ] || [ "$token" = "your-discord-bot-token" ]; then
    fail "DISCORD_TOKEN is not set in .env — get one at https://discord.com/developers/applications"
    return 1
  fi
  ok ".env present, DISCORD_TOKEN set"
}

check_ollama() {
  if ! ollama_api /api/tags; then
    if [ "$MODE" = "check" ] || [ "$MANAGE_OLLAMA" -eq 0 ]; then
      warn "Ollama not reachable at $OLLAMA_HOST"
      return 1
    fi
    if ! host_is_local; then
      warn "Ollama not reachable at $OLLAMA_HOST (remote host — not starting anything here)"
      return 1
    fi
    info "Ollama not reachable at $OLLAMA_HOST — starting it ..."
    if command -v systemctl >/dev/null 2>&1; then
      sudo -n systemctl start ollama 2>/dev/null || true
      if wait_ollama 8; then
        ok "Ollama started via systemd (ollama.service)"
        check_ollama_model
        return 0
      fi
    fi
    if command -v ollama >/dev/null 2>&1; then
      nohup ollama serve >>"$OLLAMA_LOG" 2>&1 &
      info "       launched 'ollama serve' in the background (log: $OLLAMA_LOG)"
      if wait_ollama 8; then
        ok "Ollama is up at $OLLAMA_HOST"
        check_ollama_model
        return 0
      fi
    fi
    warn "could not start Ollama — personality commands will fail until it runs"
    info "       start it manually: 'ollama serve' or 'sudo systemctl start ollama'"
    return 1
  fi
  ok "Ollama reachable at $OLLAMA_HOST"
  check_ollama_model
}

check_ollama_model() {
  if ollama_api /api/tags "$OLLAMA_MODEL"; then
    ok "Ollama model '$OLLAMA_MODEL' available"
  else
    warn "Ollama model '$OLLAMA_MODEL' not pulled — run: ollama pull $OLLAMA_MODEL"
  fi
}

# ------------------------------------------------------------------ main ----
info "== Preflight =="

# yt-dlp needs Deno on PATH; add the default install location if present.
if [ -d "$HOME/.deno/bin" ]; then
  case ":$PATH:" in
    *":$HOME/.deno/bin:"*) ;;
    *) export PATH="$HOME/.deno/bin:$PATH" ;;
  esac
fi

if check_python; then
  ensure_venv || true
fi
check_python_deps || true
check_ffmpeg || true
check_deno || true
check_env || true
check_ollama || true

if [ "$ERRORS" -gt 0 ]; then
  printf '\n%s%d error(s), %d warning(s)%s — fix the [fail] items above and retry.\n' \
    "$C_RED" "$ERRORS" "$WARNINGS" "$C_OFF"
  exit 1
fi
if [ "$MODE" = "check" ]; then
  printf '\nAll checks passed (%d warning(s)).\n' "$WARNINGS"
  exit 0
fi

printf '\n%sStarting bot%s  (python: %s | source: %s | log level: %s | Ctrl-C to stop)\n' \
  "$C_CYN" "$C_OFF" "$PY" "$SRC/main.py" "${LOG_LEVEL:-INFO}"

cd "$SRC"
set +e
"$PY" main.py
status=$?
set -e

if [ "$status" -ne 0 ]; then
  err "bot exited with status $status"
else
  info "bot exited cleanly"
fi
exit "$status"
