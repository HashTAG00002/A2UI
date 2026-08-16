#!/usr/bin/env bash
# run.sh — the documented one-command entry (kept for compatibility).
# The real launcher lives in scripts/dev.sh (portable: no personal paths,
# PID/log management under .run/, graceful stop via scripts/stop.sh).
# Stop the stack: ./scripts/stop.sh
exec "$(dirname -- "${BASH_SOURCE[0]}")/scripts/dev.sh" "$@"
