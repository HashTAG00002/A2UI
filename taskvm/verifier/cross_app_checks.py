"""cross_app_checks — cross-application consistency checks (W3 stub).

W1's ``round_trip_checks`` already verifies changed-happened per binding +
non-interference. This module adds deeper cross-app consistency (e.g. a date
moved in Calendar must be consistent with the deadline in TaskBoard that
claims to track it). Built in W3 with the full benchmark. Not built in W1.
"""
from __future__ import annotations


def check_cross_app_consistency(*args, **kwargs):
    raise NotImplementedError("cross_app_checks is W3.")
