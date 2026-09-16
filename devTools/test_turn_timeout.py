"""Focused checks for activity-aware agent turn timeouts."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _turn_timeout_reason


def check(label, actual, expected):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"[ok] {label}")


check(
    "active turn may exceed old 180-second wall cap",
    _turn_timeout_reason(0, 180, 181, 180, 900),
    None,
)
check(
    "idle turn stops after 180 seconds without progress",
    _turn_timeout_reason(0, 1, 181, 180, 900),
    "idle",
)
check(
    "active turn still honors absolute safety cap",
    _turn_timeout_reason(0, 899, 900, 180, 900),
    "absolute",
)