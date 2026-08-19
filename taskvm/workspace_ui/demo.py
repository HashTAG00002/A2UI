"""python -m taskvm.workspace_ui.demo — the portable dev/demo launcher.

This is the production-shape entry point behind ``scripts/dev.sh``: it
assembles ONE demo session over a builtin web app through the REAL
composition root (``taskvm.workspace_ui.composition.compose_task_runtime``
→ ``taskvm.runtime.bootstrap.compose_runtime``) and serves the projection
UI (``workspace_ui.serve`` → ``taskvm.projection.app.create_app``).

What is real here
-----------------
- kernel + domain plan (the same shape as ``tests/e2e_ui/test_runtime_e2e.py``);
- the five runtime ports: real ``ActionContractSerializer``, real
  ``VisibleVerifier``, real ``ModelCallLedger``;
- the CUA model: by default the REAL ``HttpCUAModel`` over
  ``HttpModelPort`` (provider env: ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY``
  / ``TASKVM_MODEL``; one predict = one provider request = one ledger
  record);
- the substrate: a real ``builtin_web`` ``SubstrateSession`` created via
  the substrate registry (name routing; the provider resolves its own app
  URL from ``--app``/``--app-host``).

What is honestly limited here (documented, never hidden)
--------------------------------------------------------
1. ``--offline`` swaps the CUA for a deterministic placeholder that always
   returns an honest FAIL decision. The UI / governance / SSE planes are
   fully live in offline mode; autonomous completion is NOT claimed.
2. The demo kernel plan is a hand-assembled fixture (2 nodes). The real
   intent→architecture pipeline (State Compiler + Task Architect) is the
   architect plane and is exercised by the evaluation/smoke suites — a
   dev launcher is not the place to fake it.
3. The composition fast-path extractor (``VisibleLabelExtractor``) matches
   ``label: value`` visible tokens. The builtin apps render tables /
   definition lists, so observed values may honestly stay empty on those
   pages — verification then fails closed instead of guessing. The full
   handle-cache compile product (architect plane) is the production
   observation path.

Usage
-----
    python -m taskvm.workspace_ui.demo --port 3016 --app calendar
    python -m taskvm.workspace_ui.demo --offline          # no provider key
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

from taskvm.domain import (
    ActionContract,
    NodeKind,
    TaskIntent,
    TaskVariable,
    WorkflowGraph,
    WorkflowNode,
)
from taskvm.kernel import TaskVMKernel
from taskvm.projection.store import ProjectionSessionStore, SurfaceDecl
from taskvm.runtime.ports import CUADecision, CUADecisionKind
from taskvm.workspace_ui import serve
from taskvm.workspace_ui.composition import (
    build_runtime_ports,
    compose_task_runtime,
)

DEMO_SID = "demo"
DEMO_GOAL = "把日历事件「产品发布」改期到 2026-08-18"
DEMO_SEED_EVENTS = [{
    "eid": "e1", "title": "产品发布", "date": "2026-08-14",
    "time": "10:00", "calendar": "work", "rsvp": "accepted",
}]
_DESIRED_DATE = "2026-08-18"


class _OfflineCUA:
    """Deterministic offline placeholder — an honest FAIL, never a fake
    success."""

    def predict_action(self, *, goal: str, observation, **kw) -> CUADecision:
        return CUADecision(
            kind=CUADecisionKind.FAIL,
            reason="offline demo mode: no model configured — the UI, "
                   "governance and SSE planes are live; autonomous "
                   "completion requires a provider key")


def _http_ok(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_app(app_url: str, what: str) -> None:
    for _ in range(50):
        if _http_ok(f"{app_url}/health"):
            return
        time.sleep(0.2)
    sys.exit(f"ERROR: builtin app {what} not healthy at {app_url} — "
             f"start it first (scripts/dev.sh does this), or pass "
             f"--app-host/--app-port")


def _seed_app(app_url: str, sid: str) -> None:
    """Seed the demo event through the app's own no-leak seed route."""
    body = json.dumps({"task_id": "demo", "seed_state":
                       {"events": DEMO_SEED_EVENTS}}).encode()
    req = urllib.request.Request(
        f"{app_url}/api/inject_task/{sid}", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status != 200:
                print(f"warn: seed returned HTTP {r.status}", file=sys.stderr)
    except Exception as e:  # seeding is best-effort; the app may keep state
        print(f"warn: could not seed demo event ({e})", file=sys.stderr)


def _make_kernel() -> TaskVMKernel:
    kernel = TaskVMKernel(DEMO_SID, TaskIntent(goal=DEMO_GOAL))
    kernel.init_task_state([
        TaskVariable(semantic_key="event_date", label="Date",
                     observed=DEMO_SEED_EVENTS[0]["date"],
                     desired=_DESIRED_DATE, value_type="date"),
    ])
    kernel.set_plan(WorkflowGraph(nodes=(
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION,
                     label="改期「产品发布」",
                     contract=ActionContract(
                         contract_id="c1",
                         semantic_goal=DEMO_GOAL,
                         desired_state={"event_date": _DESIRED_DATE},
                         completion_condition=f"event_date=={_DESIRED_DATE}")),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("a1",)),
    )))
    return kernel


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m taskvm.workspace_ui.demo",
        description="TaskVM dev/demo launcher (real composition root)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3016)
    ap.add_argument("--app", default="calendar",
                    help="builtin app name (calendar/taskboard/drive/mail/"
                         "outlook_cal); default ports come from the "
                         "substrate provider's own table")
    ap.add_argument("--app-host", default="127.0.0.1")
    ap.add_argument("--app-port", type=int, default=None,
                    help="override the builtin app port (default: the "
                         "provider's table for --app)")
    ap.add_argument("--offline", action="store_true",
                    help="deterministic placeholder CUA (honest FAIL; no "
                         "provider call is made or claimed)")
    ap.add_argument("--model", default=None,
                    help="model name override (default: TASKVM_MODEL env "
                         "or the port default)")
    args = ap.parse_args(argv)

    # Resolve the app port WITHOUT importing the substrate provider (the
    # workspace_ui plane must not hold concrete-substrate knowledge — the
    # debt gate in tests/substrate forbids it). Each builtin app owns its
    # DEFAULT_PORT constant; the substrate launcher mirrors that table.
    if args.app_port is None:
        from importlib import import_module
        try:
            app_mod = import_module(f"taskvm.apps.{args.app}.app")
        except ImportError:
            sys.exit(f"ERROR: unknown builtin app {args.app!r}")
        args.app_port = int(getattr(app_mod, "DEFAULT_PORT"))
    target = f"http://{args.app_host}:{args.app_port}"
    _wait_app(target, f"{args.app} @ {target}")
    _seed_app(target, DEMO_SID)

    kernel = _make_kernel()
    compose_kwargs: dict = {}
    if args.app_port is not None:
        compose_kwargs["base_url"] = target
    if args.offline:
        compose_kwargs["ports"] = build_runtime_ports(
            cua_model=_OfflineCUA())
    if args.model:
        compose_kwargs["model"] = args.model
    runtime = compose_task_runtime(
        kernel, host=args.app_host, sid=DEMO_SID, app=args.app,
        **compose_kwargs)

    store = ProjectionSessionStore()
    store.register(DEMO_SID, kernel, runtime=runtime,
                   surfaces=(SurfaceDecl(surface_id=args.app,
                                         display_name=args.app),))
    app = serve(store)

    print("=" * 58)
    print(f"  TaskVM demo  →  http://{args.host}:{args.port}")
    print(f"  session '{DEMO_SID}' on builtin app '{args.app}' ({target})")
    print(f"  goal: {DEMO_GOAL}")
    if args.offline:
        print("  CUA: OFFLINE deterministic placeholder (honest FAIL)")
    elif not os.environ.get("OPENAI_API_KEY"):
        print("  CUA: real HttpCUAModel — WARNING: OPENAI_API_KEY not set;"
              " autonomy will fail at the first provider call")
    else:
        print("  CUA: real HttpCUAModel (1 predict = 1 request = 1 record)")
    print("  stop: ./scripts/stop.sh   (Ctrl-C also works)")
    print("=" * 58)
    app.run(host=args.host, port=args.port, threaded=True,
            debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
