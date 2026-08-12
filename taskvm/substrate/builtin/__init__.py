"""taskvm.substrate.builtin — the builtin desktop-app substrate (E17-C).

The builtin app adapters (CalendarAdapter, TaskBoardAdapter, DriveAdapter,
MailAdapter, OutlookCalAdapter) live in ``taskvm.substrate.base`` (co-located
with StateAdapter because they share the contract). This package exists for
future split-out (handoff §3.2 lists substrate/builtin/adapters.py as a
future home) — for now it re-exports from base for ergonomic imports.
"""
from taskvm.substrate.base import (
    CalendarAdapter, TaskBoardAdapter, DriveAdapter,
    MailAdapter, OutlookCalAdapter,
)

__all__ = [
    "CalendarAdapter", "TaskBoardAdapter", "DriveAdapter",
    "MailAdapter", "OutlookCalAdapter",
]
