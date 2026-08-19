"""store — per-session A2UI surface state (generation / components /
data-model revision), completely separate from the Kernel's semantic
state (workplan §4 `store.py`: "don't pollute kernel semantic state").

The store owns the ORDERED message stream a renderer consumes:

- ``bootstrap_messages()`` — createSurface + latest updateComponents +
  latest updateDataModel (for GET /a2ui/bootstrap);
- ``events_after(seq)`` — the monotonically-sequenced tail (for SSE
  reconnect; seq is per-session, strictly increasing);
- ``generation`` bumps ONLY when the component tree (structure) is
  replaced — ordinary data updates bump ``data_revision`` only, which is
  the invariant that keeps ordinary updates at 0 GenUI model calls.
"""
from __future__ import annotations

import threading
from typing import Any

from taskvm.genui import protocol


class SurfaceStateError(ValueError):
    """Honest store misuse (e.g. components before surface creation)."""


class SurfaceStore:
    """One session's A2UI surface stream. Plain data + a lock; no kernel,
    no model, no substrate — trivially testable and safe to embed."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._surface_id = protocol.surface_id_for_session(session_id)
        self._lock = threading.Lock()
        self._created = False
        self._generation = 0          # structural revisions (components)
        self._data_revision = 0       # data-model revisions
        self._seq = 0                 # monotonic message sequence
        self._stream: list[tuple[int, dict[str, Any]]] = []

    # ── identity ───────────────────────────────────────────────────────
    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def surface_id(self) -> str:
        return self._surface_id

    @property
    def generation(self) -> int:
        """How many times the component tree was (re)generated. Bumping
        this is the marker of a GenUI decoder invocation."""
        with self._lock:
            return self._generation

    @property
    def data_revision(self) -> int:
        with self._lock:
            return self._data_revision

    @property
    def seq(self) -> int:
        """The latest emitted message sequence number."""
        with self._lock:
            return self._seq

    # ── writes (each append is atomic + ordered) ───────────────────────
    def ensure_surface(self) -> dict[str, Any]:
        """Idempotent createSurface — the first message of the stream."""
        with self._lock:
            if self._created:
                return self._stream[0][1]
            msg = protocol.create_surface_message(self._surface_id)
            self._created = True
            self._append_locked(msg)
            return msg

    def set_components(self, components: list[dict[str, Any]]
                       ) -> dict[str, Any]:
        """Replace the component tree (STRUCTURAL change): bumps
        ``generation``. Callers must have passed validator
        .validate_components first — the store trusts, it does not
        re-prove (single-owner rule: validation lives in validator.py)."""
        with self._lock:
            if not self._created:
                raise SurfaceStateError(
                    "ensure_surface() must be called before set_components()")
            msg = protocol.update_components_message(
                self._surface_id, components)
            self._generation += 1
            self._append_locked(msg)
            return msg

    def set_data_model(self, value: Any, *, path: str = "/") -> dict[str, Any]:
        """Deterministic data-model refresh: bumps ``data_revision`` only.
        This is the ordinary-update path — 0 GenUI model calls."""
        with self._lock:
            if not self._created:
                raise SurfaceStateError(
                    "ensure_surface() must be called before set_data_model()")
            msg = protocol.update_data_model_message(
                self._surface_id, value, path=path)
            self._data_revision += 1
            self._append_locked(msg)
            return msg

    def delete_surface(self) -> dict[str, Any]:
        with self._lock:
            msg = protocol.delete_surface_message(self._surface_id)
            self._append_locked(msg)
            return msg

    # ── reads ──────────────────────────────────────────────────────────
    def latest_components(self) -> list[dict[str, Any]] | None:
        with self._lock:
            for _seq, msg in reversed(self._stream):
                if "updateComponents" in msg:
                    return list(msg["updateComponents"]["components"])
            return None

    def latest_data_model(self) -> Any:
        with self._lock:
            for _seq, msg in reversed(self._stream):
                if "updateDataModel" in msg:
                    return msg["updateDataModel"].get("value")
            return None

    def bootstrap_messages(self) -> list[dict[str, Any]]:
        """createSurface + latest updateComponents + latest
        updateDataModel — exactly what GET /a2ui/bootstrap serves."""
        with self._lock:
            if not self._created:
                raise SurfaceStateError("surface not created yet")
            create = self._stream[0][1]
            components = next(
                (m for _s, m in reversed(self._stream)
                 if "updateComponents" in m), None)
            data = next(
                (m for _s, m in reversed(self._stream)
                 if "updateDataModel" in m), None)
            out = [create]
            if components is not None:
                out.append(components)
            if data is not None:
                out.append(data)
            return out

    def events_after(self, seq: int) -> list[dict[str, Any]]:
        """Ordered messages strictly after ``seq`` (SSE reconnect tail)."""
        with self._lock:
            return [msg for s, msg in self._stream if s > seq]

    # ── internals ──────────────────────────────────────────────────────
    def _append_locked(self, msg: dict[str, Any]) -> None:
        self._seq += 1
        self._stream.append((self._seq, msg))


class SurfaceStoreRegistry:
    """session_id → SurfaceStore. The composition root owns one; the
    stores hold no kernel references whatsoever."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stores: dict[str, SurfaceStore] = {}

    def get_or_create(self, session_id: str) -> SurfaceStore:
        with self._lock:
            store = self._stores.get(session_id)
            if store is None:
                store = SurfaceStore(session_id)
                self._stores[session_id] = store
            return store

    def get(self, session_id: str) -> SurfaceStore | None:
        with self._lock:
            return self._stores.get(session_id)
