#!/usr/bin/env bash
# scripts/app_mobilegym.sh — launch the TaskVM APP on MobileGym, LONG-LIVED.
#
# The difference vs demo_open_mobilegym.sh (which runs a one-shot demo in
# the FOREGROUND and dies with the terminal): this script starts the full
# APP stack as DETACHED daemons (pid files under .run/) and RETURNS.
# Close the terminal, walk away — the APP stays up until you stop it.
#
#   [0] interpreter + env discovery (same discipline as dev.sh)
#   [1] OPENAI_API_KEY: from env, else parsed out of .mrules §8 (the key
#       documented there — no new secret is introduced by this script)
#   [2] sim (:3000, Vite)      — reuse if healthy; best-effort restart if
#                                down; NEVER killed by us (long-lived infra)
#   [3] bridge (:3019)         — reuse if healthy; else spawn DETACHED
#                                (closed flag whitelist, B-09) + pid file
#   [4] world reset (SETUP PLANE, once, up-front): POST /api/reset/<sid>
#       → MobileGym's factory world (12 wechat contacts incl. the real
#         黄勇, 4 chats, 5 moments; 62 alipay transfers; X ships EMPTY
#         posts — don't write X tasks). Re-run with --reset to re-prime.
#   [5] APP (:3016, 0.0.0.0)   — the Codex-like shell (empty state, first
#         instruction in the BROWSER drives the real pipeline). Reuse if
#         already healthy; else spawn DETACHED + pid file.
#
# Usage:
#   ./scripts/app_mobilegym.sh             # start-or-adopt everything
#   ./scripts/app_mobilegym.sh --reset     # also re-prime the factory world
#   ./scripts/stop.sh                      # stop APP + bridge (sim survives)
#
# Knobs: SID=app UI_PORT=3016 BRIDGE_PORT=3019 SIM_URL=http://127.0.0.1:3000
#        MOBILEGYM_DIR=../mobilegym  TASKVM_MODEL=gpt-5.6-sol
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SIM_URL="${SIM_URL:-http://127.0.0.1:3000}"
BRIDGE_PORT="${BRIDGE_PORT:-3019}"
UI_PORT="${UI_PORT:-3016}"
SID="${SID:-app}"
MOBILEGYM_DIR="${MOBILEGYM_DIR:-$REPO/../mobilegym}"
RESET=0
[ "${1:-}" = "--reset" ] && RESET=1

die() { echo "ERROR: $*" >&2; exit 1; }

# ── phase 0: interpreter + env discovery (portable, dev.sh-style) ───────────
PY="${TASKVM_PYTHON:-}"
if [ -z "$PY" ]; then
    for _cand in "$REPO/../conda/envs/taskvm/bin/python" python3; do
        command -v "$_cand" >/dev/null 2>&1 && { PY="$_cand"; break; }
    done
fi
[ -n "$PY" ] || die "no interpreter — set TASKVM_PYTHON"
command -v "$PY" >/dev/null 2>&1 || die "interpreter not found: $PY"
"$PY" -c 'import flask, aiohttp, requests' >/dev/null 2>&1 \
    || die "$PY lacks flask/aiohttp/requests — use the taskvm conda env (TASKVM_PYTHON)"

_py_bin="$(command -v "$PY")"
_py_home="$(cd -- "$(dirname -- "$_py_bin")/.." && pwd)"
if [ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ] && [ -d "$_py_home/opt/ms-playwright" ]; then
    export PLAYWRIGHT_BROWSERS_PATH="$_py_home/opt/ms-playwright"
fi
[ -d "${PLAYWRIGHT_BROWSERS_PATH:-/nonexistent}" ] \
    || die "Playwright browsers not found — set PLAYWRIGHT_BROWSERS_PATH"
# PREPEND (container shells often pre-set LD_LIBRARY_PATH for CUDA/hadoop;
# a plain -z guard would skip the export and chromium dies on libatk-bridge)
if [ -d "$REPO/.chromelibs/lib" ]; then
    export LD_LIBRARY_PATH="$REPO/.chromelibs/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
[ -f "$MOBILEGYM_DIR/bench_env/__init__.py" ] \
    || die "bench_env not found under $MOBILEGYM_DIR — set MOBILEGYM_DIR"
export PYTHONPATH="$MOBILEGYM_DIR${PYTHONPATH:+:$PYTHONPATH}"

# ── phase 1: OPENAI_API_KEY — env first, else the .mrules §8 value ─────────
if [ -z "${OPENAI_API_KEY:-}" ]; then
    _key="$(sed -n 's/.*OPENAI_API_KEY=\([0-9a-zA-Z]\+\).*/\1/p' "$REPO/.mrules" | head -1)"
    [ -n "$_key" ] || die "no OPENAI_API_KEY in env and none found in .mrules"
    export OPENAI_API_KEY="$_key"
    echo "• OPENAI_API_KEY adopted from .mrules §8"
fi

_http_ok() { "$PY" - "$1" <<'PYEOF'
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
}

RUN_DIR="$REPO/.run"; mkdir -p "$RUN_DIR"

# ── phase 2: sim (long-lived infra; best-effort restart, never killed) ─────
if ! _http_ok "$SIM_URL"; then
    echo "• sim $SIM_URL is DOWN — trying a best-effort restart (vite)…"
    NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
    [ -n "$NODE_BIN" ] || die "no node on PATH — set NODE_BIN, or start the sim yourself:
    cd $MOBILEGYM_DIR && node node_modules/.bin/vite --port 3000 --host"
    ( cd "$MOBILEGYM_DIR" && setsid nohup "$NODE_BIN" node_modules/.bin/vite \
        --port "${SIM_URL##*:}" --host >/dev/null 2>&1 < /dev/null & )
    for _ in $(seq 1 40); do _http_ok "$SIM_URL" && break; sleep 0.5; done
    _http_ok "$SIM_URL" || die "sim still down at $SIM_URL — start it manually (see above)"
fi
echo "• sim healthy: $SIM_URL"

# ── phase 3: bridge — reuse if healthy, else spawn DETACHED ────────────────
BRIDGE_URL="http://127.0.0.1:$BRIDGE_PORT"
if _http_ok "$BRIDGE_URL/health"; then
    echo "• bridge already healthy (adopted): $BRIDGE_URL/health"
else
    echo "• starting bridge (detached): $PY -m taskvm.substrate.mobilegym.bridge --port $BRIDGE_PORT --sim-url $SIM_URL"
    ( cd "$REPO" && setsid nohup "$PY" -m taskvm.substrate.mobilegym.bridge \
        --port "$BRIDGE_PORT" --sim-url "$SIM_URL" \
        >"$RUN_DIR/mobilegym-bridge.log" 2>&1 < /dev/null & echo $! >"$RUN_DIR/mobilegym-bridge.pid" )
    _ok=0
    for _ in $(seq 1 180); do
        _http_ok "$BRIDGE_URL/health" && { _ok=1; break; }
        sleep 0.5
    done
    [ "$_ok" = 1 ] || { echo "--- bridge log tail ---"; tail -20 "$RUN_DIR/mobilegym-bridge.log"; \
                        die "bridge not healthy in 90s (log: $RUN_DIR/mobilegym-bridge.log)"; }
    echo "• bridge healthy: $BRIDGE_URL/health (pid $(cat "$RUN_DIR/mobilegym-bridge.pid"))"
fi

# ── phase 4: SETUP PLANE — activate the sid on MobileGym's factory world ───
# The APP itself has NO reset power (contract §4); THIS script is the
# operator's hand and primes the world once, up-front.
_need_reset=$RESET
if [ "$_need_reset" = 0 ]; then
    # reset when the sid is not yet active on the bridge
    if ! _http_ok "$BRIDGE_URL/api/observe/$SID"; then _need_reset=1; fi
fi
if [ "$_need_reset" = 1 ]; then
    resp=$(curl -sS --max-time 120 -X POST "$BRIDGE_URL/api/reset/$SID") \
        || die "reset failed for sid $SID"
    echo "$resp" | grep -q '"status": *"ok"' \
        || die "reset rejected: $resp"
    echo "• world reset OK (sid=$SID): MobileGym factory world"
else
    echo "• world kept as-is (sid=$SID already active; use --reset to re-prime)"
fi

# ── phase 5: the APP — reuse if healthy, else spawn DETACHED ───────────────
APP_URL="http://127.0.0.1:$UI_PORT"
APP_ARGV=( "$PY" -m taskvm.workspace_ui.app_open
           --host 0.0.0.0 --port "$UI_PORT" --sid "$SID"
           --bridge-port "$BRIDGE_PORT" --sim-url "$SIM_URL" --start-bridge )
if _http_ok "$APP_URL/api/app/status"; then
    echo "• APP already healthy (adopted): $APP_URL"
else
    echo "• starting APP (detached): ${APP_ARGV[*]}"
    ( cd "$REPO" && setsid nohup "${APP_ARGV[@]}" \
        >"$RUN_DIR/app.log" 2>&1 < /dev/null & echo $! >"$RUN_DIR/app.pid" )
    _ok=0
    for _ in $(seq 1 120); do
        _http_ok "$APP_URL/api/app/status" && { _ok=1; break; }
        sleep 0.5
    done
    [ "$_ok" = 1 ] || { echo "--- app log tail ---"; tail -30 "$RUN_DIR/app.log"; \
                        die "APP not healthy in 60s (log: $RUN_DIR/app.log)"; }
    echo "• APP healthy (pid $(cat "$RUN_DIR/app.pid"))"
fi

# ── summary ────────────────────────────────────────────────────────────────
_IPS="$(hostname -I 2>/dev/null || true)"
echo ""
echo "=============================================================="
echo "  TaskVM APP is UP (long-lived — closing this terminal is fine)"
echo ""
echo "  ▸ APP (open this in your browser):"
echo "      http://127.0.0.1:$UI_PORT        (via IDE port forwarding)"
[ -n "$_IPS" ] && for _ip in $_IPS; do
    echo "      http://$_ip:$UI_PORT"
done
echo "      → empty state “How can I help you today?”"
echo "        type the first instruction; the REAL pipeline runs"
echo "        (gpt-5.6-sol: StateCompiler → TaskArchitect → CUA)"
echo ""
echo "  ▸ Phone sim (watch it being driven, also in your browser):"
echo "      $SIM_URL"
echo ""
echo "  ports: $UI_PORT=APP · $BRIDGE_PORT=bridge · ${SIM_URL##*:}=sim"
echo "  pids:  .run/app.pid .run/mobilegym-bridge.pid  logs: .run/*.log"
echo "  stop:  ./scripts/stop.sh   (sim survives by design)"
echo "  re-prime factory world:  ./scripts/app_mobilegym.sh --reset"
echo "=============================================================="
