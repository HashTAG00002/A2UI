#!/usr/bin/env bash
# scripts/stop.sh — gracefully stop the dev stack started by scripts/dev.sh.
#
# PID-file driven: only processes WE recorded under .run/ are signalled
# (TERM, then KILL after a grace period). No port carpet-bombing, no
# killing unrelated processes. On Linux, a best-effort /proc cmdline check
# guards against a PID having been recycled by an unrelated process.
set -u

RUN_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/.run"
if [ ! -d "$RUN_DIR" ]; then
    echo "no .run/ directory — nothing to stop (start with scripts/dev.sh)"
    exit 0
fi

# pid looks like ours? (best-effort; non-Linux falls back to plain kill)
_ours() { # pid
    pid="$1"
    if [ -r "/proc/$pid/cmdline" ] && command -v tr >/dev/null 2>&1; then
        cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
        case "$cmd" in
            *taskvm*) return 0 ;;
            "") return 0 ;;      # kernel thread / unreadable → treat as gone
            *) echo "  skip pid $pid (recycled? cmd: ${cmd:0:60})"; return 1 ;;
        esac
    fi
    return 0
}

FAILED=0
shopt -s nullglob
for pidfile in "$RUN_DIR"/*.pid; do
    name="$(basename -- "$pidfile" .pid)"
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [ -z "$pid" ]; then
        rm -f "$pidfile"
        continue
    fi
    if kill -0 "$pid" 2>/dev/null && _ours "$pid"; then
        kill -TERM "$pid" 2>/dev/null || true
        i=0
        while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 25 ]; do
            sleep 0.2; i=$((i + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "• $name (pid $pid) did not exit — sending KILL"
            kill -KILL "$pid" 2>/dev/null || true
            FAILED=1
        else
            echo "• $name (pid $pid) stopped"
        fi
    else
        echo "• $name (pid $pid) not running"
    fi
    rm -f "$pidfile"
done

if [ "$FAILED" -eq 0 ]; then
    echo "dev stack stopped (logs remain under .run/)"
fi
exit "$FAILED"
