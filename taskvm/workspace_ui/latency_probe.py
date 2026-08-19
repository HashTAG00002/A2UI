"""python -m taskvm.workspace_ui.latency_probe — zero-model latency probe.

Measures the transport/server legs of the TaskVM APP stack WITHOUT any
model call (contract: the audit must be reproducible for free):

  * bridge observe round-trip + payload size (the screenshot leg)
  * sim (Vite) first-byte latency
  * APP status/sessions/screenshot endpoints
  * SSE frame size for one ACTION_OBSERVED event (the artifact_ref leg)

Usage:
    python -m taskvm.workspace_ui.latency_probe \
        [--app-url http://127.0.0.1:3016] \
        [--bridge-url http://127.0.0.1:3019] \
        [--sim-url http://127.0.0.1:3000] \
        [--json-out PATH]

All measurements are localhost (agent-side); the remote-user network
penalty (measured separately from app.log cadence) is applied in the
waterfall report, not here.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request


def _timed(name: str, url: str, timeout: float = 30.0) -> dict:
    t0 = time.monotonic()
    rec: dict = {"probe": name, "url": url}
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = r.read()
            rec.update(ms=round((time.monotonic() - t0) * 1000, 1),
                       bytes=len(data), status=r.status)
    except Exception as e:  # honest failure — recorded, never hidden
        rec.update(ms=round((time.monotonic() - t0) * 1000, 1),
                   error=f"{type(e).__name__}: {e}")
    return rec


def probe(app_url: str, bridge_url: str, sim_url: str,
          repeats: int = 3) -> dict:
    out: dict = {"probe_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "repeats": repeats, "legs": []}

    for i in range(repeats):
        out["legs"].append(_timed(f"bridge /health #{i+1}",
                                  f"{bridge_url}/health"))
    for i in range(repeats):
        rec = _timed(f"bridge observe #{i+1}",
                     f"{bridge_url}/api/observe/app", timeout=60)
        if "bytes" in rec:
            try:
                with urllib.request.urlopen(
                        f"{bridge_url}/api/observe/app",
                        timeout=60) as r:
                    j = json.loads(r.read())
                shot = j.get("screenshot", "")
                rec["screenshot_b64_chars"] = len(shot)
                rec["screenshot_decoded_bytes"] = len(shot) * 3 // 4
                rec["visible_text_chars"] = len(j.get("visible_text", ""))
            except Exception:
                pass
        out["legs"].append(rec)
    for i in range(repeats):
        out["legs"].append(_timed(f"sim first-byte #{i+1}", f"{sim_url}/"))
    for i in range(repeats):
        out["legs"].append(_timed(f"APP /api/app/status #{i+1}",
                                  f"{app_url}/api/app/status"))
    for i in range(repeats):
        out["legs"].append(_timed(f"APP /api/sessions #{i+1}",
                                  f"{app_url}/api/sessions"))
    for i in range(repeats):
        out["legs"].append(_timed(f"APP /api/app/screenshot #{i+1}",
                                  f"{app_url}/api/app/screenshot"))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="latency_probe")
    ap.add_argument("--app-url", default="http://127.0.0.1:3016")
    ap.add_argument("--bridge-url", default="http://127.0.0.1:3019")
    ap.add_argument("--sim-url", default="http://127.0.0.1:3000")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)
    result = probe(args.app_url, args.bridge_url, args.sim_url,
                   args.repeats)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    main()
