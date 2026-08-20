"""python -m taskvm.workspace_ui.a9_latency_probe — the A9.0 full-chain
latency audit probe (2026-08-20 edition, owner symptom driven).

ZERO model calls by construction. Two stacks are measured:

  A. a SYNTHETIC session stack (real Flask + real SSE + the real A2UI
     transport + the real governance routes; only the kernel is
     hand-built the same way tests/e2e_ui builds it and the live-shot
     bytes are a generated PNG). This measures the HTTP / governance /
     SSE / thumbnail / client-render legs WITHOUT touching the shared
     sim or the shared bridges (agentRM.3 runs GATE-G0 on 3049 — never
     disturbed).
  B. the SHARED sim at :3000 through read-only probes (first-byte +
     a real Chromium page-load module census — quantifying the
     "sim cold start feels minutes-long on a slow link" symptom).

Model legs (compiler/architect/CUA) are NOT re-measured this round:
the 2026-08-19 archive (eval_results/latency_audit_20260819 +
taskvm_demo_run_20260819, 18 real gpt-5.6-sol calls) stays the model
baseline; the waterfall cites it by reference.

Usage:
    python -m taskvm.workspace_ui.a9_latency_probe \
        [--port 3116] [--sim-url http://127.0.0.1:3000] \
        [--out eval_results/a9_latency_20260820] [--skip-browser]

Outputs (all JSON, no screenshots except the browser evidence PNGs):
    <out>/probe_legs.json      — per-leg timings (min/mean/max, bytes)
    <out>/sse_probe.json       — SSE first-frame + ready-signal latency
    <out>/browser_probe.json   — Chromium client-side measurements +
                                 performance marks (gov click→ack etc.)
    <out>/sim_census.json      — sim:3000 module census (read-only)
    <out>/env_audit.json       — residual daemons / port occupancy
"""
from __future__ import annotations

import argparse
import io
import json
import socket
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from taskvm.domain import (
    ActionContract, NodeKind, TaskIntent, TaskVariable,
    WorkflowGraph, WorkflowNode,
)
from taskvm.kernel import TaskVMKernel
from taskvm.projection.store import ProjectionSessionStore, SurfaceDecl
from taskvm.workspace_ui import serve
from taskvm.workspace_ui.app_open import (
    AppState, register_app_routes,
)
from taskvm.workspace_ui.a2ui_transport import (
    A2uiTransport, kernel_stage_payload, register_a2ui_routes,
)

SID = "a9probe"


# ── the synthetic (zero-model) world ────────────────────────────────────────

def _contract(cid: str, key: str, value: str) -> ActionContract:
    return ActionContract(
        contract_id=cid, semantic_goal=f"set {key} to {value}",
        desired_state={key: value},
        completion_condition=f"{key} shows {value}")


def _make_kernel(sid: str = SID) -> TaskVMKernel:
    """The same hand-built kernel shape tests/e2e_ui uses — real
    kernel, real plan graph, zero model calls."""
    intent = TaskIntent(goal="把发布会改到周五", scope=("发布",))
    kernel = TaskVMKernel(sid, intent)
    kernel.init_task_state([
        TaskVariable(semantic_key="release_date", label="发布日期",
                     observed="2026-08-14", desired="2026-08-18",
                     value_type="date"),
    ])
    graph = WorkflowGraph(nodes=(
        WorkflowNode(node_id="seq1", kind=NodeKind.SEQUENCE, label="发布流程"),
        WorkflowNode(node_id="a1", kind=NodeKind.ACTION, label="设置发布日期",
                     parent_id="seq1",
                     contract=_contract("c1", "release_date", "2026-08-18")),
        WorkflowNode(node_id="v1", kind=NodeKind.VERIFY,
                     label="核对日历", depends_on=("a1",),
                     verification="日历显示发布日期为 2026-08-18"),
        WorkflowNode(node_id="t1", kind=NodeKind.TERMINAL, label="完成",
                     depends_on=("v1",)),
    ))
    kernel.set_plan(graph)
    return kernel


def _probe_png(width: int = 390, height: int = 844) -> bytes:
    """A deterministic phone-shaped PNG for the live-shot side channel
    (the shot legs are measured end-to-end; only the pixels are
    synthetic — honestly labeled in every output record)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), (18, 24, 38))
    d = ImageDraw.Draw(img)
    d.rectangle((24, 80, width - 24, 200), fill=(38, 52, 84))
    d.text((34, 110), "TaskVM A9 latency probe", fill=(230, 236, 248))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_synthetic_app(shot_png: bytes):
    """Real Flask app: projection routes + APP routes + a2ui routes,
    one synthetic session, one A2UI surface attached, one live shot."""
    store = ProjectionSessionStore()
    kernel = _make_kernel()
    store.register(SID, kernel, surfaces=(
        SurfaceDecl(surface_id="mobilegym:wechat", display_name="微信"),))
    a2ui = A2uiTransport(session_lookup=store.get)
    state = AppState(store, sid=SID, bridge_url="http://127.0.0.1:3019",
                     sim_url="http://127.0.0.1:3000", model=None,
                     surfaces=({"id": "mobilegym:wechat", "name": "微信"},),
                     initial_app="", offline=False, a2ui=a2ui)
    state.push_screenshot(SID, "image/png", shot_png)
    app = serve(store)
    register_app_routes(app, store, state)
    register_a2ui_routes(app, a2ui, store, state)
    # the §20.1 signal sequence a real bootstrap pushes (T0 goal → T1
    # variable labels → T2 real DAG) — same payloads, zero model calls
    a2ui.push_stage(SID, "goal", {"goal": "把发布会改到周五"})
    a2ui.push_stage(SID, "t1", {"variables": [{"label": "发布日期"}]})
    a2ui.push_stage(SID, "t2", kernel_stage_payload(kernel))
    # mint the A2UI surface (the same call the composition root makes;
    # validation gates included — a failure here is a real failure)
    attach = a2ui.attach_session(SID, store.get(SID))
    return app, state, attach


# ── timing helpers ──────────────────────────────────────────────────────────

def _timed(fn: Callable[[], Any]) -> dict[str, Any]:
    t0 = time.monotonic()
    out = fn()
    return {"ms": round((time.monotonic() - t0) * 1000, 2), "out": out}


def _repeats(name: str, fn: Callable[[], Any], n: int = 5,
             **extra: Any) -> dict[str, Any]:
    samples: list[float] = []
    last: Any = None
    for _ in range(n):
        t0 = time.monotonic()
        last = fn()
        samples.append(round((time.monotonic() - t0) * 1000, 2))
    return {
        "leg": name,
        "n": n,
        "min_ms": min(samples),
        "mean_ms": round(statistics.mean(samples), 2),
        "max_ms": max(samples),
        "samples_ms": samples,
        "last": last,
        **extra,
    }


def _get(url: str, timeout: float = 30.0) -> tuple[int, int]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = r.read()
        return r.status, len(data)


def _post(url: str, body: dict | None = None,
          timeout: float = 30.0) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        # 4xx IS an honest governance answer (409 conflict / 404 unknown
        # checkpoint …) — read the body, never hide the status
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_flask(app, port: int) -> None:
    app.run(host="127.0.0.1", port=port, threaded=True,
            debug=False, use_reloader=False)


def _wait_up(base: str, path: str = "/api/app/status", tries: int = 60) -> None:
    for _ in range(tries):
        try:
            _get(base + path, timeout=2)
            return
        except Exception:
            time.sleep(0.15)
    raise RuntimeError(f"synthetic app did not come up at {base}")


# ── the probes ──────────────────────────────────────────────────────────────

def probe_http_legs(base: str, shot_png: bytes) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []

    def _g(path: str):
        def run():
            status, nbytes = _get(base + path)
            return {"status": status, "bytes": nbytes}
        return run

    legs.append(_repeats("GET /api/app/status (shell status)",
                         _g("/api/app/status")))
    legs.append(_repeats("GET /api/app/surface_shots (wall feed)",
                         _g("/api/app/surface_shots")))

    def _bootstrap():
        status, nbytes = _get(base + "/api/app/a2ui/bootstrap")
        return {"status": status, "bytes": nbytes,
                "png_bytes_on_wire": nbytes > 100_000}
    legs.append(_repeats("GET /api/app/a2ui/bootstrap (ordered replay)",
                         _bootstrap,
                         note="the A5 invariant: NO screenshot bytes in "
                              "the A2UI stream"))

    # thumbnail pipeline: full PNG vs ≤240px thumb vs hash-dedup (0-body)
    def _full_shot():
        status, nbytes = _get(base + "/api/app/screenshot")
        return {"status": status, "bytes": nbytes}
    legs.append(_repeats("GET /api/app/screenshot (FULL live shot)",
                         _full_shot,
                         source_bytes=len(shot_png)))

    def _thumb():
        status, nbytes = _get(base + "/api/app/screenshot?thumb=1&w=240")
        return {"status": status, "bytes": nbytes}
    legs.append(_repeats("GET /api/app/screenshot?thumb=1&w=240 (thumbnail)",
                         _thumb))

    fp = None
    with urllib.request.urlopen(base + "/api/app/screenshot") as r:
        fp = r.headers.get("X-Shot-Hash", "")

    def _dedup():
        status, nbytes = _get(
            base + f"/api/app/screenshot?thumb=1&w=240&h={fp}")
        return {"status": status, "bytes": nbytes,
                "zero_body": nbytes == 0}
    legs.append(_repeats(
        "GET /api/app/screenshot?…&h=<hash> (unchanged screen)",
        _dedup,
        note="unchanged screen ⇒ zero-body 200 (X-Shot-Same: 1) — the "
             "countermeasure to the 0819 audit's 2 MB-per-poll finding"))

    # governance legs — ZERO model calls by construction
    def _start_refused():
        status, body = _post(base + "/api/app/governance/start")
        # the synthetic session registered no runtime → honest 409;
        # what is measured is the ACCEPTANCE round trip, not the refusal
        return {"status": status, "ok": body.get("ok")}
    legs.append(_repeats(
        "POST /api/app/governance/start (acceptance; 409 no-runtime)",
        _start_refused,
        note="synthetic session has no runtime: the honest 409 IS the "
             "acceptance path — the real start (driver) is the same "
             "single-owner path the frozen route runs"))

    ck_label = f"探针检查点 {int(time.time())}"

    def _checkpoint():
        status, body = _post(base + "/api/app/governance/checkpoint",
                             {"label": ck_label})
        return {"status": status, "ok": body.get("ok")}
    legs.append(_repeats(
        "POST /api/app/governance/checkpoint (REAL kernel write)",
        _checkpoint,
        note="a REAL governance-port checkpoint (kernel write, zero "
             "model calls) — the acceptance of a user governance "
             "command"))
    return legs


def probe_sse(base: str) -> dict[str, Any]:
    """Connect to the a2ui SSE stream and time: first byte, first
    `ready` progress frame, first frame overall. Uses requests'
    streaming so no frame is buffered by the client."""
    import requests
    t0 = time.monotonic()
    rec: dict[str, Any] = {"leg": "GET /api/app/a2ui/sse (island stream)",
                           "frames": []}
    with requests.get(base + "/api/app/a2ui/sse?after=0", stream=True,
                      timeout=30) as r:
        first_byte_ms = None
        ready_ms = None
        first_frame_ms = None
        total_bytes = 0
        deadline = time.monotonic() + 6.0
        for raw in r.iter_lines(decode_unicode=True):
            if time.monotonic() > deadline:
                break
            if first_byte_ms is None:
                first_byte_ms = round((time.monotonic() - t0) * 1000, 2)
            if raw is None or raw == "":
                continue
            total_bytes += len(raw)
            if first_frame_ms is None:
                first_frame_ms = round((time.monotonic() - t0) * 1000, 2)
            rec["frames"].append(raw[:160])
            if "ready" in raw and ready_ms is None:
                ready_ms = round((time.monotonic() - t0) * 1000, 2)
                break
            if len(rec["frames"]) > 40:
                break
        rec.update(
            first_byte_ms=first_byte_ms,
            first_frame_ms=first_frame_ms,
            ready_progress_ms=ready_ms,
            bytes_read=total_bytes,
            note="the reconnect path (`after=N`) replays the ordered "
                 "tail; progress ring replays the small morph hints")
    return rec


def probe_flask_concurrency(base: str) -> dict[str, Any]:
    """Two CONCURRENT slow-ish GETs must overlap (threaded=True): the
    wall time of N parallel requests ≈ one request, not N×."""
    import requests
    urls = [base + "/api/app/status"] * 4
    t0 = time.monotonic()
    threads = []

    def hit(u: str) -> None:
        try:
            requests.get(u, timeout=10)
        except Exception:
            pass
    for u in urls:
        th = threading.Thread(target=hit, args=(u,))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    parallel_ms = (time.monotonic() - t0) * 1000
    t0 = time.monotonic()
    for u in urls:
        try:
            requests.get(u, timeout=10)
        except Exception:
            pass
    serial_ms = (time.monotonic() - t0) * 1000
    return {
        "leg": "Flask threaded model (4× concurrent vs serial)",
        "parallel_ms": round(parallel_ms, 2),
        "serial_ms": round(serial_ms, 2),
        "overlap_ratio": round(serial_ms / max(parallel_ms, 0.001), 2),
        "note": "app.run(threaded=True): one thread per request — "
                "concurrent GETs overlap (audit hypothesis ①, "
                "re-verified 2026-08-20)",
    }


def probe_env_audit(sim_url: str) -> dict[str, Any]:
    """Residual daemons / port occupancy snapshot (read-only)."""
    def _sh(cmd: str) -> str:
        try:
            return subprocess.run(
                ["bash", "-c", cmd], capture_output=True, text=True,
                timeout=10).stdout.strip()
        except Exception as e:
            return f"<unavailable: {e}>"
    ports = _sh(
        "ss -tlnp 2>/dev/null | grep -E ':(3000|3016|3019|3026|3029|3049|"
        "3116|3119)\\b' || true")
    procs = _sh(
        "ps aux | grep -E 'app_open|mobilegym.bridge|gate_g0' |"
        " grep -v grep | awk '{print $2, $11, $12, $13, $14, $15}' | head -12")
    sim_first_byte = None
    try:
        t0 = time.monotonic()
        status, nbytes = _get(sim_url + "/", timeout=10)
        sim_first_byte = {"ms": round((time.monotonic() - t0) * 1000, 2),
                          "status": status, "bytes": nbytes}
    except Exception as e:
        sim_first_byte = {"error": str(e)}
    return {
        "leg": "environment audit (read-only)",
        "ports_listening": ports,
        "related_processes": procs,
        "sim_first_byte": sim_first_byte,
        "note": "sim:3000 常驻健康检查;bridge/APP/GATE-G0 端口占用快照"
                "(agentRM.3 的 3049 绝不触碰)",
    }


# ── the browser leg (Chromium, real /a2ui island) ───────────────────────────

def probe_browser(base: str, out_dir: str) -> dict[str, Any]:
    """A real Chromium loads the BUILT island and we measure:
      - shell first render (navigation → governance-shell in DOM);
      - the optimistic receipt: native click → [data-pending=true]
        on the Start button (the <100ms contract, MEASURED);
      - performance marks (gov-*-click → ack) from page.evaluate;
      - the screenshot wall's thumbnail load + click-to-zoom modal.
    Screenshots land in out_dir as evidence PNGs."""
    from playwright.sync_api import sync_playwright
    rec: dict[str, Any] = {"leg": "Chromium client leg (built island)"}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        marks: list[dict[str, Any]] = []
        page.on("console", lambda m: marks.append(
            {"type": "console", "text": m.text[:120]}) if m.type == "error"
            else None)

        t0 = time.monotonic()
        page.goto(base + "/a2ui", wait_until="domcontentloaded")
        page.wait_for_selector("[data-testid='governance-shell']",
                               timeout=15000)
        rec["shell_first_render_ms"] = round(
            (time.monotonic() - t0) * 1000, 2)

        # the staged timeline + wall hydrate from the SSE replay
        try:
            page.wait_for_selector("[data-testid='surface-card']",
                                   timeout=8000)
            rec["wall_card_ms"] = round((time.monotonic() - t0) * 1000, 2)
        except Exception:
            rec["wall_card_ms"] = None
        try:
            page.wait_for_selector("[data-testid='surface-thumb']",
                                   timeout=8000)
            rec["wall_thumb_ms"] = round((time.monotonic() - t0) * 1000, 2)
        except Exception:
            rec["wall_thumb_ms"] = None

        # ── the <100ms optimistic receipt, MEASURED in the browser ──
        # (checkpoint is enabled only while running; the START button is
        #  the owner's exact symptom — its optimistic pending state is
        #  local, so we measure the DOM flip, not the server answer)
        receipt = page.evaluate("""() => {
            const btn = document.querySelector(
                '[data-governance-action="open-evidence"]');
            return btn ? {found: true} : {found: false};
        }""")
        rec["evidence_button_found"] = receipt.get("found")

        click_receipt = page.evaluate("""() => {
            return new Promise((resolve) => {
                const start = document.querySelector(
                    '[data-governance-action="start"]');
                if (!start) { resolve({found: false}); return; }
                const t0 = performance.now();
                start.click();
                // the optimistic pending flip is synchronous with the
                // React event — read it on the next microtask
                requestAnimationFrame(() => {
                    resolve({
                        found: true,
                        pending: start.dataset.pending,
                        ariaBusy: start.getAttribute('aria-busy'),
                        ms: performance.now() - t0,
                    });
                });
            });
        }""")
        rec["start_click_receipt"] = click_receipt

        # checkpoint click (a REAL governance write) → mark round trip
        page.evaluate("""() => { window.__gov_marks = []; }""")
        ck = page.evaluate("""async () => {
            const btn = document.querySelector(
                '[data-governance-action="checkpoint"]');
            if (!btn || btn.disabled) return {found: false};
            const t0 = performance.now();
            btn.click();
            const pendingAt = performance.now() - t0;
            // wait for the ack receipt (last-action updates)
            for (let i = 0; i < 200; i++) {
                await new Promise(r => setTimeout(r, 25));
                const la = document.querySelector('[data-testid="last-action"]');
                if (la && la.textContent.includes('checkpoint')) {
                    return {found: true, pending_flip_ms: pendingAt,
                            ack_ms: performance.now() - t0,
                            text: la.textContent};
                }
            }
            return {found: true, pending_flip_ms: pendingAt,
                    ack_ms: null, text: '(no ack in 5s)'};
        }""")
        rec["checkpoint_click"] = ck

        # the shot modal (click-to-zoom lazy full image)
        zoom = None
        try:
            page.click("[data-testid='surface-card']", timeout=4000)
            page.wait_for_selector("[data-testid='shot-modal-img']",
                                   timeout=8000)
            zoom = {"modal_opened": True}
        except Exception as e:
            zoom = {"modal_opened": False, "error": str(e)[:120]}
        rec["click_to_zoom"] = zoom

        # collect the island's own performance marks/measures
        perf = page.evaluate("""() => {
            const entries = performance.getEntriesByType('measure')
                .concat(performance.getEntriesByType('mark'));
            return entries.map(e => ({name: e.name,
                                      type: e.entryType,
                                      duration: e.duration || null}));
        }""")
        rec["performance_entries"] = perf
        rec["console_errors"] = [m for m in marks
                                 if m.get("type") == "console"]

        page.screenshot(path=f"{out_dir}/browser_island.png", full_page=True)
        browser.close()
    return rec


def probe_sim_census(sim_url: str, out_dir: str) -> dict[str, Any]:
    """A real Chromium loads the sim page READ-ONLY (no interaction):
    count the ESM module requests + bytes + load time — the mechanical
    reason a 4.5-6s-RTT remote link takes minutes on a cold load."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        reqs: list[dict[str, Any]] = []
        page.on("response", lambda r: reqs.append(
            {"url": r.url[:110], "status": r.status}))
        t0 = time.monotonic()
        try:
            page.goto(sim_url + "/", wait_until="load", timeout=60000)
            load_ms = round((time.monotonic() - t0) * 1000, 2)
        except Exception as e:
            load_ms = None
            out = {"leg": "sim:3000 module census (read-only load)",
                   "error": str(e)[:160]}
            browser.close()
            return out
        time.sleep(1.0)   # let late module fetches land
        total = len(reqs)
        js = [r for r in reqs if ".ts" in r["url"] or ".tsx" in r["url"]
              or ".js" in r["url"]]
        out = {
            "leg": "sim:3000 module census (read-only load)",
            "local_load_ms": load_ms,
            "total_requests": total,
            "js_module_requests": len(js),
            "sample_urls": [r["url"] for r in js[:12]],
            "note": "Vite DEV mode: the first paint recursively pulls the "
                    "whole ESM graph. On the owner's measured 4.5-6s "
                    "per-request penalty (6 parallel connections) this "
                    "graph is the multi-minute cold start. A vite build "
                    "would collapse it to a handful of files (moved to "
                    "the mobilegym owner, M2 in the 0819 audit).",
        }
        browser.close()
        return out


# ── main ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="a9_latency_probe")
    ap.add_argument("--port", type=int, default=3116)
    ap.add_argument("--sim-url", default="http://127.0.0.1:3000")
    ap.add_argument("--out", default="eval_results/a9_latency_20260820")
    ap.add_argument("--skip-browser", action="store_true")
    args = ap.parse_args(argv)

    import os
    os.makedirs(args.out, exist_ok=True)
    base = f"http://127.0.0.1:{args.port}"

    shot_png = _probe_png()
    app, _state, attach = build_synthetic_app(shot_png)
    port = args.port if args.port else _free_port()
    th = threading.Thread(target=_run_flask, args=(app, port), daemon=True)
    th.start()
    _wait_up(base)

    results: dict[str, Any] = {
        "probe_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "synthetic_stack": {
            "port": port,
            "attach": attach,
            "shot_png_bytes": len(shot_png),
            "note": "real Flask/SSE/a2ui-transport/governance routes; "
                    "hand-built kernel (tests/e2e_ui pattern) + generated "
                    "PNG shot; ZERO model calls, ZERO shared-sim access",
        },
    }

    results["http_legs"] = probe_http_legs(base, shot_png)
    with open(f"{args.out}/probe_legs.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    sse = probe_sse(base)
    with open(f"{args.out}/sse_probe.json", "w", encoding="utf-8") as f:
        json.dump(sse, f, ensure_ascii=False, indent=2)

    results["flask_concurrency"] = probe_flask_concurrency(base)
    results["env_audit"] = probe_env_audit(args.sim_url)
    with open(f"{args.out}/env_audit.json", "w", encoding="utf-8") as f:
        json.dump({"flask_thread_model": results["flask_concurrency"],
                   **results["env_audit"]}, f, ensure_ascii=False, indent=2)

    if not args.skip_browser:
        try:
            browser_probe = probe_browser(base, args.out)
            with open(f"{args.out}/browser_probe.json", "w",
                      encoding="utf-8") as f:
                json.dump(browser_probe, f, ensure_ascii=False, indent=2)
        except Exception as e:
            with open(f"{args.out}/browser_probe.json", "w",
                      encoding="utf-8") as f:
                json.dump({"error": f"{type(e).__name__}: {e}"},
                          ensure_ascii=False, indent=2, fp=f)
        try:
            census = probe_sim_census(args.sim_url, args.out)
        except Exception as e:
            census = {"error": f"{type(e).__name__}: {e}"}
        with open(f"{args.out}/sim_census.json", "w",
                  encoding="utf-8") as f:
            json.dump(census, f, ensure_ascii=False, indent=2)

    print(json.dumps({k: v for k, v in results.items()
                      if k != "http_legs"},
                     ensure_ascii=False, indent=2)[:4000])
    print(f"\n[probe] legs → {args.out}/probe_legs.json")


if __name__ == "__main__":
    main()
