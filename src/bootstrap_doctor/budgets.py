"""Standalone defaults matching current OpenClaw bootstrap budgets.

OpenClaw owns its runtime injection policy. These constants mirror its
defaults so bootstrap-doctor gives useful results when OpenClaw is not
importable and does not change behavior based on whether Brigade is installed.
"""
from __future__ import annotations

DEFAULT_SOFT_LIMIT = 17_000
DEFAULT_HARD_LIMIT = 20_000
DEFAULT_TOTAL_LIMIT = 60_000

__all__ = [
    "DEFAULT_HARD_LIMIT",
    "DEFAULT_SOFT_LIMIT",
    "DEFAULT_TOTAL_LIMIT",
]
