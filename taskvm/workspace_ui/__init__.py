"""taskvm.workspace_ui — static frontend for the TaskVM Projection UI.

The old W1/W2 structured-text renderer / editable_components / live_sync
modules are replaced by the projection contract (docs/contracts/projection.md).
This package now serves as the static asset root for ``create_app()``.

Public API:
    STATIC_DIR — absolute path to the static assets (index.html, css/, js/)
    serve() — convenience: build a Flask app with the static assets wired
"""
from __future__ import annotations

import pathlib

STATIC_DIR = pathlib.Path(__file__).parent / "static"


def serve(store, **kwargs):
    """Build a Flask app with the workspace UI static assets wired."""
    from taskvm.projection import create_app
    return create_app(
        store,
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
        **kwargs)
