"""taskvm.substrate.builtin_web — the built-in Web/Playwright substrate.

All Web/Playwright-specific code lives here (contract §1): browser driver,
port session, provider, launcher (app URLs), evaluation environment. No
other directory may import Playwright for substrate purposes.
"""
from taskvm.substrate.builtin_web.browser import (
    BrowserController, ensure_chromium_env, shutdown_browser,
)
from taskvm.substrate.builtin_web.evaluation import (
    WebEvaluationEnvironment, make_evaluation_environment,
    make_evaluation_environments,
)
from taskvm.substrate.builtin_web.launcher import app_url, BUILTIN_APPS
from taskvm.substrate.builtin_web.provider import (
    BuiltinWebProvider, BuiltinWebEvaluationProvider,
)
from taskvm.substrate.builtin_web.session import WebSubstrateSession

__all__ = [
    "BrowserController", "ensure_chromium_env", "shutdown_browser",
    "WebEvaluationEnvironment", "make_evaluation_environment",
    "make_evaluation_environments",
    "app_url", "BUILTIN_APPS",
    "BuiltinWebProvider", "BuiltinWebEvaluationProvider",
    "WebSubstrateSession",
]
