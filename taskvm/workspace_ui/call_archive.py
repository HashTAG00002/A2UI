"""taskvm.workspace_ui.call_archive — full-fidelity model-call recording.

A ``RecordingModelPort`` wraps ANY ``ModelPort`` and, for every real
provider request, writes ONE txt file with the COMPLETE verbatim exchange:

  - the full system prompt (exact text sent)
  - the full user prompt (exact text sent)
  - the full raw response text (exact text received, after <think>-strip
    at the port level — see http_port._strip_think; the unstripped text is
    what this wrapper sees only if it wraps OUTSIDE HttpModelPort, which
    it does: it wraps the HttpModelPort itself, so it records exactly what
    the provider returned to the port BEFORE think-stripping)
  - the parsed JSON (if any), usage tokens, latency, model name, params

Images (``image_data_url`` — the multimodal screenshot part) are decoded
and saved as standalone PNG files under ``images/``; the txt references
them by relative path. NO base64 blob ever appears inside a txt file.

Role attribution: the three production roles (state_compiler /
task_architect / cua) each carry a distinctive fixed system-prompt prefix;
the wrapper matches on those prefixes (exact, not fuzzy). An unmatched
prompt is recorded as role=unknown — honest, never guessed.

Activation: purely opt-in via the environment variable
``TASKVM_CALL_ARCHIVE_DIR`` (see app_open.run_goal wiring). Without the
variable this module is never constructed and changes nothing.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from typing import Any

from taskvm.architect.port import ModelReply

# exact system-prompt prefixes of the three production roles
_ROLE_PREFIXES = (
    ("cua", "你是一个图形界面操作代理"),
    ("state_compiler", "You are the State Compiler of a Task Virtual Machine"),
    ("task_architect", "You are the Task Architect of a Task Virtual Machine"),
)


def _role_of(system: str) -> str:
    for role, prefix in _ROLE_PREFIXES:
        if system.startswith(prefix):
            return role
    return "unknown"


class RecordingModelPort:
    """Wraps a ModelPort; records every call verbatim to the archive dir.

    Thread-safe (CUA driver thread / Flask governance threads / bootstrap
    thread all share one port instance): a lock serialises both the
    sequence counter and the file writes.
    """

    def __init__(self, inner: Any, archive_dir: str) -> None:
        self._inner = inner
        self._dir = os.path.abspath(archive_dir)
        self._img_dir = os.path.join(self._dir, "images")
        os.makedirs(self._img_dir, exist_ok=True)
        self._lock = threading.Lock()
        # sequence numbers CONTINUE from whatever already sits in the
        # archive dir — multiple sessions (or a restarted process) append,
        # they never overwrite an earlier session's files
        self._seq = self._max_existing_seq()
        self._index_path = os.path.join(self._dir, "INDEX.txt")
        if not os.path.exists(self._index_path):
            with open(self._index_path, "a", encoding="utf-8") as f:
                f.write("# TaskVM 模型调用档案索引（每次真实 provider 请求一行）\n"
                        "# 格式: 序号 | 角色 | 模型 | 耗时ms | tokens(p/c) "
                        "| ok | 文件\n")

    def _max_existing_seq(self) -> int:
        import re as _re
        best = 0
        try:
            for name in os.listdir(self._dir):
                m = _re.match(r"call_(\d{3})_", name)
                if m:
                    best = max(best, int(m.group(1)))
        except OSError:
            pass
        return best

    # ── ModelPort protocol ─────────────────────────────────────────────
    @property
    def default_model(self) -> str:
        return getattr(self._inner, "default_model", "")

    @property
    def base_url(self) -> str:
        return getattr(self._inner, "base_url", "")

    def complete_json(self, *, system: str, user: str,
                      model: str | None = None, max_tokens: int = 3072,
                      temperature: float | None = None,
                      image_data_url: str | None = None) -> ModelReply:
        t0 = time.monotonic()
        role = _role_of(system)
        error = ""
        reply: ModelReply | None = None
        try:
            reply = self._inner.complete_json(
                system=system, user=user, model=model,
                max_tokens=max_tokens, temperature=temperature,
                image_data_url=image_data_url)
            assert reply is not None  # success path — always a reply
            return reply
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            latency_ms = int((time.monotonic() - t0) * 1000)
            try:
                self._write_call(
                    role=role, system=system, user=user, model=model,
                    max_tokens=max_tokens, temperature=temperature,
                    image_data_url=image_data_url, reply=reply,
                    latency_ms=latency_ms, error=error)
            except Exception as e:  # archiving must never kill the run
                try:
                    with open(os.path.join(self._dir, "ARCHIVE_ERRORS.log"),
                              "a", encoding="utf-8") as f:
                        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                                f"archive write failed: {e}\n")
                except Exception:
                    pass

    # ── internals ──────────────────────────────────────────────────────
    def _write_call(self, *, role: str, system: str, user: str,
                    model: str | None, max_tokens: int,
                    temperature: float | None, image_data_url: str | None,
                    reply: ModelReply | None, latency_ms: int,
                    error: str) -> None:
        with self._lock:
            self._seq += 1
            n = self._seq
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            mdl = (model or self.default_model or "unknown")
            fname = f"call_{n:03d}_{role}.txt"

            image_lines: list[str] = []
            img_rel: str | None = None
            if isinstance(image_data_url, str) \
                    and image_data_url.startswith("data:image/"):
                try:
                    head, b64 = image_data_url.split(",", 1)
                    mime = head[5:].split(";", 1)[0] or "image/png"
                    ext = "png" if "png" in mime else \
                        ("jpg" if "jpeg" in mime or "jpg" in mime else "bin")
                    img_name = f"call_{n:03d}_image_1.{ext}"
                    with open(os.path.join(self._img_dir, img_name),
                              "wb") as f:
                        f.write(base64.b64decode(b64))
                    img_rel = f"images/{img_name}"
                    image_lines.append(
                        f"  [1] {img_rel}  ({mime}, "
                        f"{len(base64.b64decode(b64))} bytes 解码后)\n"
                        f"      (base64 原文约 {len(image_data_url)} 字符，"
                        f"已省略——见文件)")
                except Exception as e:
                    image_lines.append(f"  (图片解码失败: {e})")

            pt = getattr(reply, "prompt_tokens", None) if reply else None
            ct = getattr(reply, "completion_tokens", None) if reply else None
            raw = getattr(reply, "raw", "") if reply else ""
            parsed = getattr(reply, "parsed", None) if reply else None
            ok = (reply is not None and error == "")

            lines: list[str] = []
            lines.append("═" * 78)
            lines.append(f"TaskVM 模型调用档案 #{n:03d}")
            lines.append("═" * 78)
            lines.append(f"时间         : {ts}")
            lines.append(f"角色(role)   : {role}"
                         + ("  [按 system prompt 前缀精确匹配]"
                            if role != "unknown" else "  [未匹配已知角色]"))
            lines.append(f"模型         : {mdl}")
            lines.append(f"base_url     : {self.base_url or '(未知)'}")
            lines.append(f"参数         : max_tokens={max_tokens}"
                         f"  temperature={temperature}")
            lines.append(f"耗时         : {latency_ms} ms")
            lines.append(f"tokens       : prompt={pt}  completion={ct}")
            lines.append(f"结果         : "
                         + ("OK" if ok else f"FAILED — {error}"))
            lines.append("")
            lines.append("─" * 78)
            lines.append("【SYSTEM PROMPT — 完整原文，逐字】")
            lines.append("─" * 78)
            lines.append(system)
            lines.append("")
            lines.append("─" * 78)
            lines.append("【USER PROMPT — 完整原文，逐字】")
            lines.append("─" * 78)
            lines.append(user)
            lines.append("")
            if image_lines:
                lines.append("─" * 78)
                lines.append("【图片附件 — 已另存为文件，base64 不出现在本 txt】")
                lines.append("─" * 78)
                lines.extend(image_lines)
                lines.append("")
            lines.append("─" * 78)
            lines.append("【模型返回 raw — 完整原文，逐字】")
            lines.append("─" * 78)
            lines.append(raw if raw else "(无返回内容)")
            lines.append("")
            lines.append("─" * 78)
            lines.append("【解析后 JSON（http_port._extract_json 的产物）】")
            lines.append("─" * 78)
            try:
                lines.append(json.dumps(parsed, ensure_ascii=False, indent=2)
                             if parsed is not None else "(解析失败或无 JSON)")
            except Exception:
                lines.append(repr(parsed))
            lines.append("")
            lines.append("═" * 78)
            lines.append(f"(档案结束 #{n:03d})")

            with open(os.path.join(self._dir, fname), "w",
                      encoding="utf-8") as f:
                f.write("\n".join(lines))

            with open(self._index_path, "a", encoding="utf-8") as f:
                f.write(f"#{n:03d} | {role} | {mdl} | {latency_ms}ms | "
                        f"{pt}/{ct} | {'ok' if ok else 'FAIL'} | "
                        f"{fname}"
                        + (f" | 附图 {img_rel}" if img_rel else "")
                        + "\n")


def maybe_recording_port(port: Any) -> Any:
    """Wrap ``port`` for archiving iff TASKVM_CALL_ARCHIVE_DIR is set.

    The APP wires this at the single composition site (app_open.run_goal);
    without the env var the port passes through untouched.
    """
    d = os.environ.get("TASKVM_CALL_ARCHIVE_DIR", "").strip()
    if not d:
        return port
    return RecordingModelPort(port, d)
