#!/usr/bin/env bash
# scripts/dev.sh — TaskVM portable dev launcher (portability rules below).
#
# Starts the builtin web apps (the demo substrate), then the TaskVM
# projection server (python -m taskvm.workspace_ui.demo). Every service
# gets a PID file + log under .run/; stop everything with scripts/stop.sh.
#
# Portability rules honoured here:
#   - repo root resolved from this script's own location (no $PWD assumptions)
#   - interpreter: $TASKVM_PYTHON if set, else the ambient `python3`
#   - NO hardcoded /mnt/... personal paths anywhere
#   - Playwright browsers: standard discovery, plus one *derived* fallback —
#     if the interpreter's own env bundles opt/ms-playwright (conda-style),
#     use it; an explicit PLAYWRIGHT_BROWSERS_PATH always wins
#   - port-in-use is REFUSED with a pointed message (never carpet-killed)
#   - health-checked startup; a failing service aborts the launch and
#     stops whatever already started, pointing at its log file
#
# Environment knobs:
#   TASKVM_PYTHON       interpreter to use (default: python3)
#   TASKVM_UI_PORT      projection UI port      (default: 3016)
#   TASKVM_DEMO_APP     builtin app for the demo session (default: calendar)
#   TASKVM_DEMO_OFFLINE set non-empty → demo runs with the honest offline
#                       placeholder CUA (no provider call is made or claimed)
#   OPENAI_BASE_URL / OPENAI_API_KEY / TASKVM_MODEL  → real CUA provider
set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${TASKVM_PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || {
    echo "ERROR: python interpreter not found: $PY (set TASKVM_PYTHON)" >&2
    exit 1
}

RUN_DIR="$REPO/.run"
mkdir -p "$RUN_DIR"

# ── optional, env-derived browser discovery (no personal paths) ────────────
if [ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
    _py_bin="$(command -v "$PY")"
    _py_home="$(cd -- "$(dirname -- "$_py_bin")/.." && pwd)"
    if [ -d "$_py_home/opt/ms-playwright" ]; then
        export PLAYWRIGHT_BROWSERS_PATH="$_py_home/opt/ms-playwright"
    fi
fi
if [ -z "${LD_LIBRARY_PATH:-}" ] && [ -d "$REPO/.chromelibs/lib" ]; then
    export LD_LIBRARY_PATH="$REPO/.chromelibs/lib"
fi

# name:port — mirrors each app's own DEFAULT_PORT (and the substrate
# provider's table); keep in sync if an app ever moves.
APPS=(calendar:3013 taskboard:3014 drive:3015 mail:3017 outlook_cal:3018)
UI_PORT="${TASKVM_UI_PORT:-3016}"
DEMO_APP="${TASKVM_DEMO_APP:-calendar}"
DEMO_ARGS=(--app "$DEMO_APP")
if [ -n "${TASKVM_DEMO_OFFLINE:-}" ]; then
    DEMO_ARGS+=(--offline)
fi
DEMO_PORT=""
for entry in "${APPS[@]}"; do
    if [ "${entry%%:*}" = "$DEMO_APP" ]; then
        DEMO_PORT="${entry##*:}"
    fi
done
if [ -z "$DEMO_PORT" ]; then
    echo "ERROR: TASKVM_DEMO_APP='$DEMO_APP' is not a builtin app" >&2
    exit 1
fi

_port_free() { # exits 0 if FREE
    "$PY" - "$1" <<'PYEOF'
import socket, sys
s = socket.socket()
busy = s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0
s.close()
sys.exit(1 if busy else 0)
PYEOF
}

_wait_health() { # url name logfile
    "$PY" - "$1" "$2" "$3" <<'PYEOF'
import sys, time, urllib.request
url, name, log = sys.argv[1], sys.argv[2], sys.argv[3]
for _ in range(75):
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            if r.status == 200:
                sys.exit(0)
    except Exception:
        time.sleep(0.2)
print(f"ERROR: {name} did not become healthy at {url}; log: {log}",
      file=sys.stderr)
sys.exit(1)
PYEOF
}

cd "$REPO"

# ── refuse busy ports up-front (point at stop.sh, never kill) ───────────────
for entry in "${APPS[@]}" "ui:$UI_PORT"; do
    name="${entry%%:*}"; port="${entry##*:}"
    if ! _port_free "$port"; then
        echo "ERROR: port $port ($name) is already in use." >&2
        echo "       If it is a previous dev stack: ./scripts/stop.sh" >&2
        exit 1
    fi
done

# ── start the builtin apps ──────────────────────────────────────────────────
for entry in "${APPS[@]}"; do
    name="${entry%%:*}"; port="${entry##*:}"
    "$PY" -m "taskvm.apps.$name.app" --port "$port" \
        >"$RUN_DIR/$name.log" 2>&1 &
    pid=$!
    echo "$pid" >"$RUN_DIR/$name.pid"
    echo "• $name    http://127.0.0.1:$port  (pid $pid, log .run/$name.log)"
done

# ── health-check them (abort + clean up on failure) ─────────────────────────
for entry in "${APPS[@]}"; do
    name="${entry%%:*}"; port="${entry##*:}"
    if ! _wait_health "http://127.0.0.1:$port/health" "$name" \
            "$RUN_DIR/$name.log"; then
        "$REPO/scripts/stop.sh" || true
        exit 1
    fi
done

# ── start the projection UI (demo session via the real composition root) ────
"$PY" -m taskvm.workspace_ui.demo --host 127.0.0.1 --port "$UI_PORT" \
    "${DEMO_ARGS[@]}" >"$RUN_DIR/ui.log" 2>&1 &
pid=$!
echo "$pid" >"$RUN_DIR/ui.pid"
if ! _wait_health "http://127.0.0.1:$UI_PORT/api/sessions" \
        "workspace-ui" "$RUN_DIR/ui.log"; then
    "$REPO/scripts/stop.sh" || true
    exit 1
fi

echo ""
echo "=============================================="
echo "  TaskVM dev stack ready"
echo "  UI:          http://127.0.0.1:$UI_PORT"
echo "  demo app:    http://127.0.0.1:$DEMO_PORT"
echo "  stop:        ./scripts/stop.sh"
echo "  logs/pids:   .run/"
echo "=============================================="
