"""python -m taskvm.workspace_ui.demo_open — the OPEN prototype launcher.

Task C (2026-08-18): this is no longer a builtin-only demo. It is the
launcher a REAL human uses to drive an arbitrary natural-language goal
through the genuine TaskVM pipeline on either open substrate:

    goal + substrate + model/config
      -> substrate_registry.create_session(...)      [the ONE port entry]
      -> bootstrap_real_full(...)                    [the ONE composition]
      -> same runtime, same projection/governance UI, same SSE plane

Substrates (``--substrate``)
----------------------------
* ``mobilegym`` (DEFAULT) — the resident MobileGym bridge over the Vite
  sim. The launcher provides exactly the allowed glue (re-prompt §Task C):
  a ``--bridge-url``, app/device parameters, a minimal health check, and
  an OPTIONAL ``--start-bridge`` that spawns the bridge subprocess from a
  CLOSED flag whitelist identical to the bench factory's (B-09:
  ``--port`` / ``--sim-url`` / ``--screenshot-dir`` only — a CUA-loop
  injection flag can never appear on this launch line).
* ``builtin_web`` (explicit fallback/test substrate) — same registry
  entry, same bootstrap, real builtin web app.

Both routes go through ``substrate_registry.create_session(...)`` and the
SAME ``bootstrap_real_full`` — the identical execution path the benchmark
factory consumes (Task D evidence: ``taskvm_bench/evaluation/
mobilegym_factory.py`` lazily imports the very same function; verified by
``tests/e2e_ui/test_demo_open_launcher.py``).

Fail-closed discipline (never a silent fallback)
------------------------------------------------
* bridge missing and no ``--start-bridge`` → exit, telling the user the
  exact manual start command;
* bridge spawned but never healthy → terminate + exit;
* sim unreachable / sid not activated on the bridge → the launcher's
  one probe ``observe()`` fails → exit, naming what is missing (start the
  Vite sim / activate the sid via the bridge's setup route) — the
  launcher NEVER falls back to builtin_web, never resets/seeds the world
  itself (setup-plane powers stay on the evaluation side, contract §4);
* architect cannot produce a valid DAG → loud failure with the
  architect's own error (no hand-built plan, unchanged).

Boundary notes
--------------
* ``taskvm/`` never imports ``taskvm_bench`` (repo-wide architecture
  gate); the bridge subprocess is launched as a module COMMAND LINE, not
  an import, and the launcher speaks to it only via HTTP + the substrate
  port.
* No ``mobilegym`` special-casing exists in compiler/architect/runtime —
  substrate differences live only in ``taskvm/substrate/`` (contract §6).
  There is no second TaskVM for MobileGym and no benchmark-only path.

What is honestly limited here (documented, never hidden)
--------------------------------------------------------
1. ``--offline`` swaps the CUA for a deterministic FAIL placeholder; the
   compiler/architect calls still happen for real (separate ports) unless
   ``OPENAI_API_KEY`` is unset, in which case bootstrap fails at the
   first provider call — honestly, not silently.
2. One session per launcher run (one ``--sid``); multi-surface open
   routing is the A-01 resolver's job downstream, not the launcher's.

Usage
-----
    # MobileGym (default substrate) — bridge already running:
    OPENAI_API_KEY=... python -m taskvm.workspace_ui.demo_open \\
        --goal "给妈妈发一条微信：晚饭我请客" --app wechat

    # spawn the bridge too (closed whitelist, killed on exit):
    python -m taskvm.workspace_ui.demo_open --goal "..." \\
        --start-bridge --sim-url http://localhost:3000

    # builtin_web explicit fallback/test substrate:
    python -m taskvm.workspace_ui.demo_open --substrate builtin_web \\
        --app calendar --goal "把日历事件「产品发布」改期到 2026-08-18"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import Callable

from taskvm.architect import ModelCallLedger
from taskvm.projection.store import ProjectionSessionStore
from taskvm.runtime.ports import CUADecision, CUADecisionKind
from taskvm.substrate import substrate_registry
from taskvm.workspace_ui import serve
from taskvm.workspace_ui.composition import bootstrap_real_full

#: repo root (this file is <repo>/taskvm/workspace_ui/demo_open.py) — the
#: spawned bridge subprocess needs it on PYTHONPATH to import taskvm.
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".."))

#: bridge defaults mirror taskvm/substrate/mobilegym/bridge.py
DEFAULT_BRIDGE_PORT = 3019
DEFAULT_SIM_URL = "http://localhost:3000"

#: the CLOSED bridge launch-flag whitelist — byte-for-byte the bench
#: factory's B-09 discipline (taskvm_bench/evaluation/mobilegym_factory.py
#: ``_BRIDGE_ALLOWED_FLAGS``): if a flag is not in this set the launcher
#: refuses to pass it, so no CUA-loop injection can ever appear on an
#: open-launcher start line either.
_BRIDGE_ALLOWED_FLAGS = frozenset({"--port", "--sim-url", "--screenshot-dir"})

#: how long a spawned bridge may take to become healthy (it boots a real
#: Playwright browser against the sim; the bench factory uses the same 90s).
_BRIDGE_STARTUP_TIMEOUT_S = 90.0

#: per-substrate app defaults — one ``--app`` flag, substrate-owned default
_APP_DEFAULTS = {"mobilegym": "wechat", "builtin_web": "calendar"}


class _OfflineCUA:
    """Deterministic offline placeholder — an honest FAIL, never a fake
    success (same contract as demo.py's ``_OfflineCUA``)."""

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


def _seed_app(app_url: str, sid: str, seed_state: dict | None) -> None:
    """Best-effort no-leak seed — an open goal may not need any seed at
    all (the app already has whatever state a real user left it in), so
    an empty/omitted ``--seed-json`` simply skips this call."""
    if not seed_state:
        return
    body = json.dumps({"task_id": sid, "seed_state": seed_state}).encode()
    req = urllib.request.Request(
        f"{app_url}/api/inject_task/{sid}", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status != 200:
                print(f"warn: seed returned HTTP {r.status}", file=sys.stderr)
    except Exception as e:
        print(f"warn: could not seed ({e})", file=sys.stderr)


# ── mobilegym launcher glue (health check / optional owned spawn) ───────────

def _bridge_url(args: argparse.Namespace) -> str:
    return (args.bridge_url or f"http://127.0.0.1:{args.bridge_port}").rstrip("/")


def _spawn_bridge_argv(args: argparse.Namespace) -> list[str]:
    """The bridge subprocess launch line — CLOSED whitelist (B-09).

    Mirrors the bench factory's ``_bridge_argv``: only ``--port`` /
    ``--sim-url`` / ``--screenshot-dir`` may ever appear (``''`` disables
    per-step PNGs). There is no code path that adds an injection flag,
    and the assert keeps a future edit from widening it silently."""
    argv = [sys.executable, "-m", "taskvm.substrate.mobilegym.bridge",
            "--port", str(args.bridge_port),
            "--sim-url", args.sim_url,
            "--screenshot-dir", ""]
    flags = {a for a in argv if a.startswith("--")}
    unknown = flags - _BRIDGE_ALLOWED_FLAGS
    assert not unknown, f"bridge launch flags not whitelisted: {unknown}"
    return argv


def _ensure_mobilegym_bridge(
        args: argparse.Namespace,
        *, spawn: Callable[..., subprocess.Popen] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic) \
        -> subprocess.Popen | None:
    """Fail-closed bridge glue: connect to a healthy bridge, or (with
    ``--start-bridge``) spawn one the launcher OWNS and kills on exit.

    Returns the owned ``Popen`` (``None`` when merely connected to an
    already-healthy bridge — never killed by us). Any failure exits with
    an explicit what-is-missing message; there is NO builtin_web
    fallback anywhere in this path."""
    url = _bridge_url(args)
    if _http_ok(f"{url}/health"):
        return None
    manual = (f"start it manually:\n"
              f"    {sys.executable} -m taskvm.substrate.mobilegym.bridge "
              f"--port {args.bridge_port} --sim-url {args.sim_url}\n"
              f"or pass --start-bridge to let this launcher own it")
    if not args.start_bridge:
        sys.exit(f"ERROR: no healthy MobileGym bridge at {url} — {manual}")
    argv = _spawn_bridge_argv(args)
    spawn = spawn or subprocess.Popen
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = spawn(argv, cwd=_REPO_ROOT, env=env,
                 stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    deadline = monotonic() + _BRIDGE_STARTUP_TIMEOUT_S
    while monotonic() < deadline:
        if proc.poll() is not None:
            sys.exit(f"ERROR: MobileGym bridge subprocess exited rc="
                     f"{proc.returncode} before becoming healthy "
                     f"(argv={argv}). Likely causes: the sim at "
                     f"{args.sim_url} is down (see --sim-url), or a "
                     f"bridge-side dependency (e.g. the MobileGym "
                     f"'bench_env' package) is not on PYTHONPATH — the "
                     f"spawned bridge inherits THIS process's "
                     f"PYTHONPATH ({env.get('PYTHONPATH', '')!r})")
        if _http_ok(f"{url}/health", timeout=1.0):
            return proc
        sleep(0.5)
    proc.terminate()
    sys.exit(f"ERROR: MobileGym bridge at {url} did not become healthy "
             f"within {_BRIDGE_STARTUP_TIMEOUT_S}s (argv={argv})")


def _probe_observe(substrate, sid: str, bridge_url: str) -> None:
    """One real observe() through the port BEFORE bootstrap: fail fast
    with a human message naming what is missing (sim down / sid not
    activated). Read-only; the launcher never resets or seeds (setup
    powers stay on the evaluation plane, contract §4)."""
    try:
        surface = substrate.list_surfaces()[0]
        substrate.observe(surface)
    except Exception as e:                     # SubstrateUnavailable et al.
        msg = str(e)
        if "409" in msg or "mismatch" in msg or "not active" in msg:
            sys.exit(
                f"ERROR: bridge {bridge_url} is healthy but session "
                f"{sid!r} is not activated on it (bridge said: {msg}). "
                f"The open launcher deliberately has NO reset/seed power "
                f"(evaluation-plane capability, substrate contract §4). "
                f"Activate the session on the bridge's setup route, e.g.:\n"
                f"    curl -X POST {bridge_url}/api/reset/{sid}\n"
                f"or run a bench trial once — then relaunch.")
        sys.exit(
            f"ERROR: first observe() through the mobilegym port failed "
            f"({type(e).__name__}: {msg}). The bridge is at "
            f"{bridge_url} — the most common cause is the sim itself: "
            f"is the Vite dev server running and reachable at "
            f"{DEFAULT_SIM_URL}? (see --sim-url when using "
            f"--start-bridge). Fail-closed: no fallback substrate.")


# ── substrate routing — BOTH routes through the one registry port ───────────

def _open_substrate(args: argparse.Namespace):
    """``--substrate`` → ``substrate_registry.create_session(...)`` → the
    same downstream path for both substrates. Returns ``(session, world)``
    where ``world`` is a human-readable description for the banner."""
    if args.substrate == "mobilegym":
        bridge_url = _bridge_url(args)
        cfg = {"sid": args.sid, "bridge_url": bridge_url, "app": args.app}
        session = substrate_registry.create_session("mobilegym", cfg)
        _probe_observe(session, args.sid, bridge_url)
        world = (f"mobilegym bridge {bridge_url} · app '{args.app}' "
                 f"(sim: {args.sim_url})")
        return session, world
    # builtin_web — explicit fallback/test substrate, same port entry
    if args.app_port is None:
        from importlib import import_module
        try:
            app_mod = import_module(f"taskvm.apps.{args.app}.app")
        except ImportError:
            sys.exit(f"ERROR: unknown builtin app {args.app!r}")
        args.app_port = int(getattr(app_mod, "DEFAULT_PORT"))
    target = f"http://{args.app_host}:{args.app_port}"
    _wait_app(target, f"{args.app} @ {target}")
    _seed_app(target, args.sid,
              json.loads(args.seed_json) if args.seed_json else None)
    cfg = {"app": args.app, "host": args.app_host, "sid": args.sid,
           "base_url": target}
    session = substrate_registry.create_session("builtin_web", cfg)
    return session, f"builtin app '{args.app}' ({target})"


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m taskvm.workspace_ui.demo_open",
        description="TaskVM OPEN prototype launcher — arbitrary NL goal "
                     "→ substrate_registry → bootstrap_real_full → the "
                     "ONE composition/runtime/projection path (Task C)")
    ap.add_argument("--goal", required=True,
                     help="ANY natural-language goal — never templated, "
                          "goes to the model as-is")
    ap.add_argument("--substrate", default="mobilegym",
                     choices=("mobilegym", "builtin_web"),
                     help="open substrate (default: mobilegym; "
                          "builtin_web is the explicit fallback/test "
                          "substrate — both route through the same "
                          "registry + composition)")
    ap.add_argument("--sid", default="open-demo",
                     help="session id (default: open-demo)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3016,
                    help="projection/governance UI port (default: 3016)")
    ap.add_argument("--app", default=None,
                     help="app on the chosen substrate — mobilegym: "
                          "wechat (default)/alipay/x; builtin_web: "
                          "calendar (default)/taskboard/drive/mail/"
                          "outlook_cal")
    ap.add_argument("--seed-json", default=None,
                     help="[builtin_web] optional JSON object posted to "
                          "the app's no-leak seed route before the goal "
                          "runs; omit for an un-seeded run")
    ap.add_argument("--app-host", default="127.0.0.1",
                    help="[builtin_web] builtin app host")
    ap.add_argument("--app-port", type=int, default=None,
                    help="[builtin_web] override the builtin app port "
                         "(default: the app's own DEFAULT_PORT)")
    ap.add_argument("--bridge-url", default=None,
                    help="[mobilegym] bridge base URL (default: "
                         "http://127.0.0.1:--bridge-port)")
    ap.add_argument("--bridge-port", type=int, default=DEFAULT_BRIDGE_PORT,
                    help="[mobilegym] bridge port for health check and "
                         "spawned bridges (default: 3019)")
    ap.add_argument("--sim-url", default=DEFAULT_SIM_URL,
                    help="[mobilegym --start-bridge] Vite sim URL passed "
                         "to the spawned bridge (default: "
                         "http://localhost:3000)")
    ap.add_argument("--start-bridge", action="store_true",
                    help="[mobilegym] spawn the bridge subprocess from "
                         "the CLOSED flag whitelist (same as the bench "
                         "factory, B-09) and kill it on exit; without "
                         "this flag an unhealthy bridge fails closed")
    ap.add_argument("--offline", action="store_true",
                    help="deterministic placeholder CUA (honest FAIL); "
                         "compiler/architect calls still need a "
                         "provider key")
    ap.add_argument("--model", default=None,
                    help="model override (default: TASKVM_MODEL env or "
                          "the port default, e.g. gpt-5.6-sol)")
    return ap


def main(argv: list[str] | None = None) -> None:
    args = _build_arg_parser().parse_args(argv)
    if args.app is None:
        args.app = _APP_DEFAULTS[args.substrate]

    spawned_bridge = None
    try:
        # mobilegym glue: connect to (or own) a healthy bridge — fail
        # closed with instructions, never a builtin_web fallback.
        if args.substrate == "mobilegym":
            spawned_bridge = _ensure_mobilegym_bridge(args)

        substrate, world = _open_substrate(args)

        if not args.offline and not os.environ.get("OPENAI_API_KEY"):
            sys.exit(
                "ERROR: no OPENAI_API_KEY set — the open-goal pipeline "
                "needs a REAL provider call for the compiler+architect "
                "stage (there is no hand-built fallback plan). Set "
                "OPENAI_API_KEY/OPENAI_BASE_URL/TASKVM_MODEL, or use "
                "--offline to at least exercise the UI/governance/SSE "
                "planes with a fixed fixture via demo.py instead.")

        print("=" * 62)
        print(f"  TaskVM OPEN launcher  →  http://{args.host}:{args.port}")
        print(f"  substrate '{args.substrate}' · session '{args.sid}'")
        print(f"  world: {world}")
        if spawned_bridge is not None:
            print("  bridge: SPAWNED by this launcher (killed on exit)")
        print(f"  goal (verbatim, un-templated): {args.goal!r}")
        print("  pipeline: goal -> StateCompiler -> TaskArchitect -> "
              "Kernel -> AutonomyRuntime")
        print("  (the SAME bootstrap_real_full the benchmark consumes)")
        print("=" * 62)

        ledger = ModelCallLedger()
        store = ProjectionSessionStore()
        cua_model = _OfflineCUA() if args.offline else None
        try:
            bootstrap_real_full(
                goal=args.goal, sid=args.sid, substrate=substrate,
                ledger=ledger, store=store, model=args.model,
                cua_model=cua_model)
        except Exception as e:
            # Honest, loud failure — never a hand-built fallback plan
            # (RM contract: a real model-capability limit surfaces as-is).
            sys.exit(f"ERROR: real-full bootstrap failed for goal "
                      f"{args.goal!r} — {type(e).__name__}: {e}")

        print(f"  compiler+architect calls so far: {ledger.total()} "
              f"(roles: {sorted(set(r.role for r in ledger.records))})")
        if args.offline:
            print("  CUA: OFFLINE deterministic placeholder (honest FAIL)")
        else:
            print("  CUA: real HttpCUAModel (1 predict = 1 request = 1 "
                  "record)")
        print("  start the autonomous loop with:")
        print(f"    curl -X POST http://{args.host}:{args.port}"
              f"/governance/{args.sid}/start")
        print("  stop: Ctrl-C (a spawned bridge dies with this process)")
        print("=" * 62)

        app = serve(store)
        app.run(host=args.host, port=args.port, threaded=True,
                debug=False, use_reloader=False)
    finally:
        # the launcher owns ONLY the bridge it spawned — a merely
        # connected bridge belongs to its deployment and is never killed.
        if spawned_bridge is not None:
            spawned_bridge.terminate()
            try:
                spawned_bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:   # honest escalation
                spawned_bridge.kill()
                spawned_bridge.wait(timeout=5)


if __name__ == "__main__":
    main()
