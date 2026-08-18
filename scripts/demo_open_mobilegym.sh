#!/usr/bin/env bash
# scripts/demo_open_mobilegym.sh — ONE-SHOT open-scenario MobileGym demo.
#
# The FULL manual flow, as one reproducible command:
#
#   [0] discover interpreter + env (taskvm conda python, Playwright browsers,
#       .chromelibs, bench_env PYTHONPATH)            — mirroring scripts/dev.sh
#   [1] refuse busy ports (pointed message, never a carpet kill)
#   [2] health-check the Vite sim (:3000); if down, best-effort restart with
#       node; the sim is long-lived infra and is NEVER killed by this script
#   [3] start the MobileGym bridge as THIS script's own subprocess, from the
#       CLOSED flag whitelist (--port/--sim-url/--screenshot-dir only — a
#       CUA-loop injection flag can never appear on this line, B-09)
#   [4] wait for the bridge /health (it boots a real headless Chromium)
#   [5] SETUP PLANE (contract §4 — demo_open itself deliberately has NO
#       reset/seed power; this script does it once, up-front):
#         POST /api/reset/<sid>            → MobileGym's OWN factory world
#             (apps/*/data/defaults.json, baked into the sim's JS bundle —
#              12 wechat contacts incl. the REAL 黄勇/wxid_huangyong_brave,
#              4 chats, 5 moments, etc. NOTHING is injected by default)
#         POST /api/inject_task/<sid>  (--seed-huangyong only) → an extra
#             AI-off 黄勇 REPLACEMENT contact, for bench-fixture-style runs
#             that need to dodge the real 黄勇's aiConfig auto-reply
#   [6] run demo_open in the FOREGROUND (Ctrl-C ends the demo):
#         substrate_registry → bootstrap_real_full → REAL compiler+architect
#         +CUA provider calls → projection/governance UI + SSE
#   [7] trap EXIT/INT/TERM: kill the owned bridge only. The sim is left alone.
#
# Usage:
#   ./scripts/demo_open_mobilegym.sh                          # default goal
#   ./scripts/demo_open_mobilegym.sh "给微信里的黄勇发一条消息：我马上到"
#   GOAL="..." SID=demo-x UI_PORT=3017 BRIDGE_PORT=3029 APP=wechat \
#       ./scripts/demo_open_mobilegym.sh
#   ./scripts/demo_open_mobilegym.sh --dry-run    # phases 0-5 only, no key
#   ./scripts/demo_open_mobilegym.sh --offline    # honest-FAIL placeholder CUA
#                                               # (compiler/architect still real)
#   ./scripts/demo_open_mobilegym.sh --seed-huangyong   # + AI-off 黄勇替身
#                                               # (only needed for the bench's
#                                               #  top3_expense_to_wechat-style
#                                               #  scripted round-trip check)
#
# Required env: OPENAI_API_KEY (gateway default https://aigc.sankuai.com/v1/
# openai/native, model default gpt-5.6-sol — see taskvm/architect/http_port.py;
# override with OPENAI_BASE_URL / TASKVM_MODEL / --model).
#
# Optional env: MOBILEGYM_DIR (default: sibling ../mobilegym), NODE_BIN (only
# needed to auto-restart a downed sim), SHOT_DIR (bridge per-step PNG dir).
set -euo pipefail

# ── knobs ───────────────────────────────────────────────────────────────────
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SIM_URL="${SIM_URL:-http://127.0.0.1:3000}"
BRIDGE_PORT="${BRIDGE_PORT:-3019}"
UI_PORT="${UI_PORT:-3016}"
SID="${SID:-open-demo}"
APP="${APP:-wechat}"                       # mobilegym surface: wechat|alipay|x
MOBILEGYM_DIR="${MOBILEGYM_DIR:-$REPO/../mobilegym}"
SHOT_DIR="${SHOT_DIR:-}"                    # non-empty → bridge --screenshot-dir
DRY_RUN=0; OFFLINE=0; SEED_HUANGYONG=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --offline) OFFLINE=1 ;;
        # opt-IN (was opt-out): MobileGym's OWN reset already restores its
        # real factory world (apps/*/data/defaults.json, compiled into the
        # sim's JS bundle) — 12 wechat contacts incl. the REAL 黄勇
        # (wxid_huangyong_brave), 4 chats, 5 moments, etc. This flag
        # additionally seeds a clean AI-OFF 黄勇 REPLACEMENT contact, only
        # useful when your goal is specifically the bench's
        # top3_expense_to_wechat-style fixture (the real 黄勇 has
        # aiConfig.enabled=true and will auto-reply, complicating a
        # scripted round-trip check) — most open goals do NOT need this.
        --seed-huangyong) SEED_HUANGYONG=1 ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        -*) echo "ERROR: unknown flag $1" >&2; exit 1 ;;
        *) break ;;
    esac
    shift
done
# Default goal targets the REAL out-of-the-box 黄勇 (wxid_huangyong_brave,
# already in apps/Wechat/data/defaults.json) — no seeding needed to run this.
GOAL="${1:-${GOAL:-给微信里的黄勇发一条消息：我马上到}}"

# Only built when --seed-huangyong is passed (see the flag's comment above).
SEED_JSON='{"task_id":"'"$SID"'","goal":"'"$GOAL"'","seed_state":{"wechat":{"add_chats":[{"id":"wxid_huangyong_demo","user":{"wxid":"wxid_huangyong_demo","name":"黄勇(替身)","avatar":"/@app-assets/Wechat/avatars/avatar_default.jpg"},"isMuted":false,"isSticky":false,"isAlert":false,"isOfficial":false,"messages":[]}],"add_contacts":[{"wxid":"wxid_huangyong_demo","name":"黄勇(替身)","avatar":"/@app-assets/Wechat/avatars/avatar_default.jpg","aiConfig":{"enabled":false}}]}}}'

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
# PREPEND (not append): this container's shells often pre-set LD_LIBRARY_PATH
# (CUDA/hadoop) — a plain "-z" guard would skip the export entirely and
# chromium dies with missing libatk-bridge-2.0.so.0 (observed in dry-run).
if [ -d "$REPO/.chromelibs/lib" ]; then
    export LD_LIBRARY_PATH="$REPO/.chromelibs/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
[ -f "$MOBILEGYM_DIR/bench_env/__init__.py" ] \
    || die "bench_env not found under $MOBILEGYM_DIR — set MOBILEGYM_DIR"
export PYTHONPATH="$MOBILEGYM_DIR${PYTHONPATH:+:$PYTHONPATH}"

# ── phase 1: refuse busy ports (point at stop/kill, never carpet-kill) ──────
_port_free() { "$PY" - "$1" <<'PYEOF'
import socket, sys
s = socket.socket()
busy = s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0
s.close()
sys.exit(1 if busy else 0)
PYEOF
}
for p in "$BRIDGE_PORT" "$UI_PORT"; do
    _port_free "$p" || die "port $p is busy — a previous bridge/UI? Kill it or set BRIDGE_PORT/UI_PORT"
done

# ── phase 2: sim health (long-lived infra; NEVER killed here) ───────────────
_http_ok() { "$PY" - "$1" <<'PYEOF'
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PYEOF
}
if ! _http_ok "$SIM_URL"; then
    echo "• sim $SIM_URL is DOWN — trying a best-effort restart (vite)…"
    NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
    [ -n "$NODE_BIN" ] || die "no node on PATH — set NODE_BIN, or start the sim yourself:
    cd $MOBILEGYM_DIR && node node_modules/.bin/vite --port 3000 --host"
    ( cd "$MOBILEGYM_DIR" && nohup "$NODE_BIN" node_modules/.bin/vite \
        --port "${SIM_URL##*:}" --host >/dev/null 2>&1 & )
    for _ in $(seq 1 40); do
        _http_ok "$SIM_URL" && break
        sleep 0.5
    done
    _http_ok "$SIM_URL" || die "sim still down at $SIM_URL — start it manually (see above)"
fi
echo "• sim healthy: $SIM_URL"

# ── phase 3: owned bridge subprocess (closed flag whitelist) ────────────────
RUN_DIR="$REPO/.run"; mkdir -p "$RUN_DIR"
BRIDGE_PID=""
cleanup() {
    # NOTE: deliberately NOT exec'ing demo_open — the trap must survive to
    # reap the owned bridge when the launcher exits (an exec would orphan it).
    if [ -n "${BRIDGE_PID:-}" ] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
        echo ""; echo "• stopping owned bridge (pid $BRIDGE_PID)…"
        kill "$BRIDGE_PID" 2>/dev/null || true
        wait "$BRIDGE_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

BRIDGE_URL="http://127.0.0.1:$BRIDGE_PORT"
BRIDGE_ARGV=( "$PY" -m taskvm.substrate.mobilegym.bridge
              --port "$BRIDGE_PORT" --sim-url "$SIM_URL" )
[ -n "$SHOT_DIR" ] && BRIDGE_ARGV+=( --screenshot-dir "$SHOT_DIR" )
echo "• starting bridge: ${BRIDGE_ARGV[*]}"
"${BRIDGE_ARGV[@]}" >"$RUN_DIR/mobilegym-bridge.log" 2>&1 &
BRIDGE_PID=$!
echo "$BRIDGE_PID" >"$RUN_DIR/mobilegym-bridge.pid"

# ── phase 4: wait for bridge health (it boots a real headless Chromium) ─────
_ok=0
for _ in $(seq 1 180); do
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
        echo "--- bridge log tail ---"; tail -20 "$RUN_DIR/mobilegym-bridge.log"
        die "bridge exited during startup (log: $RUN_DIR/mobilegym-bridge.log)"
    fi
    if _http_ok "$BRIDGE_URL/health"; then _ok=1; break; fi
    sleep 0.5
done
[ "$_ok" = 1 ] || die "bridge not healthy in 90s (log: $RUN_DIR/mobilegym-bridge.log)"
echo "• bridge healthy: $BRIDGE_URL/health"

# ── phase 5: SETUP PLANE — activate the sid, then optional seed ─────────────
resp=$(curl -sS --max-time 120 -X POST "$BRIDGE_URL/api/reset/$SID") \
    || die "reset failed for sid $SID"
echo "$resp" | grep -q '"status": *"ok"' \
    || die "reset rejected: $resp"
echo "• world reset OK (sid=$SID): $resp"
echo "  (this IS MobileGym's own factory world — 12 wechat contacts incl."
echo "   real 黄勇/wxid_huangyong_brave, 4 chats, 5 moments — nothing extra"
echo "   was injected; see apps/Wechat/data/defaults.json in the mobilegym repo)"

if [ "$SEED_HUANGYONG" -eq 1 ]; then
    resp=$(curl -sS --max-time 60 -X POST "$BRIDGE_URL/api/inject_task/$SID" \
        -H 'Content-Type: application/json' -d "$SEED_JSON") \
        || die "seed (inject_task) failed"
    echo "$resp" | grep -q '"status": *"ok"' \
        || die "seed rejected: $resp"
    echo "• extra seed OK (AI-off 黄勇替身 added — bench-fixture-style run)"
fi

# ── phase 6: the open launcher (foreground; Ctrl-C stops everything) ───────
DEMO_ARGV=( "$PY" -m taskvm.workspace_ui.demo_open
            --goal "$GOAL" --sid "$SID" --app "$APP"
            --host 127.0.0.1 --port "$UI_PORT" --bridge-port "$BRIDGE_PORT" )
[ -n "${TASKVM_MODEL:-}" ] && DEMO_ARGV+=( --model "$TASKVM_MODEL" )
[ "$OFFLINE" -eq 1 ] && DEMO_ARGV+=( --offline )

if [ "$DRY_RUN" -eq 1 ]; then
    echo ""
    echo "• DRY RUN OK — phases 0-5 verified (env/ports/sim/bridge/reset/seed)."
    echo "  The real run would now execute:"
    echo "    ${DEMO_ARGV[*]}"
    echo "  and then, from another terminal:"
    echo "    curl -X POST http://127.0.0.1:$UI_PORT/governance/$SID/start"
    exit 0
fi

[ -n "${OPENAI_API_KEY:-}" ] \
    || die "OPENAI_API_KEY is not set — the open pipeline makes REAL provider
  calls (compiler+architect+CUA; default gateway https://aigc.sankuai.com/v1/
  openai/native, model gpt-5.6-sol). Export it and re-run."

echo ""
echo "=============================================================="
echo "  next steps (from ANOTHER terminal / your browser):"
echo "    1. open the TaskVM UI:  http://127.0.0.1:$UI_PORT"
echo "       (variables / workflow / governance / ledger / live screenshots)"
echo "    2. start the autonomous loop:"
echo "         curl -X POST http://127.0.0.1:$UI_PORT/governance/$SID/start"
echo "    3. watch the model drive the phone — every observe() frame shows"
echo "       up as the surface card's screenshot in the UI"
echo "    4. Ctrl-C here stops the demo AND the owned bridge"
echo "=============================================================="
echo ""
# plain invocation (NOT exec) so the EXIT trap reaps the owned bridge after
# the launcher exits / Ctrl-C
"${DEMO_ARGV[@]}"
