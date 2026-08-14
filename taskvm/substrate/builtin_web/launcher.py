"""builtin_web.launcher — where (and only where) builtin app URLs live.

Contract §5: app URL, port and launch strategy appear ONLY in the built-in
provider config. Upper layers pass a surface name (e.g. ``calendar``) and
get back a URL; they never learn ports, hosts, or how the app was started.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: builtin desktop-app demo services (host/port per app). Kept from the
#: legacy ``substrate/base.py`` DEFAULT_PORTS table so existing deployments
#: keep working; overridable via ``TASKVM_<APP>_PORT`` env vars and by the
#: provider config dict.
BUILTIN_APP_PORTS: dict[str, int] = {
    "calendar": 3013,
    "taskboard": 3014,
    "drive": 3015,
    "mail": 3017,
    "outlook_cal": 3018,
}

BUILTIN_APPS: tuple[str, ...] = tuple(BUILTIN_APP_PORTS)


def app_url(app: str, host: str = "localhost", port: int | None = None,
            base_url: str | None = None) -> str:
    """Resolve a builtin app surface to its URL.

    ``base_url`` (explicit) wins; then ``port``; then the ``TASKVM_<APP>_PORT``
    env var; then the builtin default table."""
    if base_url:
        return base_url.rstrip("/")
    p = port
    if p is None:
        env = os.environ.get(f"TASKVM_{app.upper()}_PORT")
        if env and env.isdigit():
            p = int(env)
    if p is None:
        p = BUILTIN_APP_PORTS.get(app)
    if p is None:
        raise ValueError(
            f"unknown builtin web app {app!r}; known: {BUILTIN_APPS}")
    return f"http://{host}:{p}"


@dataclass
class WebSessionConfig:
    """Config for ``WebSubstrateSession``. Only this dataclass (built by
    the provider / composition root) knows URLs."""
    app: str
    url: str
    sid: str = ""
    viewport: tuple[int, int] = (1100, 760)
    screenshot_dir: str | None = None
