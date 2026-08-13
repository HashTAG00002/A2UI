#!/usr/bin/env bash
# TaskVM demo launcher — kills any process on each port, then starts all services.
set -e

PYTHON=/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/conda/envs/taskvm/bin/python
REPO=/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/a2ui

export PLAYWRIGHT_BROWSERS_PATH=/mnt/dolphinfs/ssd_pool/docker/user/hadoop-mt-ocr/yangwenkui03/conda/envs/taskvm/opt/ms-playwright
export LD_LIBRARY_PATH=$REPO/.chromelibs/lib:$LD_LIBRARY_PATH

cd "$REPO"

# Kill any process occupying each port
for PORT in 3013 3014 3015 3016 3017 3018; do
    fuser -k ${PORT}/tcp 2>/dev/null || true
done
sleep 1

# Start backend app servers
$PYTHON -m taskvm.apps.calendar.app   --port 3013 &
$PYTHON -m taskvm.apps.taskboard.app  --port 3014 &
$PYTHON -m taskvm.apps.drive.app      --port 3015 &
$PYTHON -m taskvm.apps.mail.app       --port 3017 &
$PYTHON -m taskvm.apps.outlook_cal.app --port 3018 &

# Give apps a moment to bind
sleep 2

# Start workspace UI (foreground — Ctrl-C to stop everything)
# --executor gui_agent: real browser gestures via GUI Agent (§12.16 compliant)
# --genui: GenUI decoder renders the rw-zone (real model call → A2UI v0.9)
# --debug: auto-reload on code changes
echo ""
echo "=========================================="
echo "  TaskVM demo ready at http://localhost:3016"
echo "  Task: launch_full (4-App fanout, Parallel workflow)"
echo "  Executor: gui_agent (real browser gestures)"
echo "  GenUI: ON (model-decoded A2UI surface)"
echo "=========================================="
$PYTHON -m taskvm.workspace_ui.server --port 3016 --task launch_full --executor gui_agent --genui --debug
