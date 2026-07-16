"""Tests for standalone OpenClaw-compatible bootstrap budgets."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import bootstrap_doctor.budgets as budgets_mod


def test_defaults_match_current_openclaw() -> None:
    assert budgets_mod.DEFAULT_SOFT_LIMIT == 17_000
    assert budgets_mod.DEFAULT_HARD_LIMIT == 20_000
    assert budgets_mod.DEFAULT_TOTAL_LIMIT == 60_000


def test_brigade_installation_does_not_override_runtime_defaults() -> None:
    """Bootstrap Doctor must remain stable when Brigade has different policy limits."""
    script = textwrap.dedent(
        """
        import sys
        import types

        brigade = types.ModuleType("brigade")
        brigade.__path__ = []
        brigade_budgets = types.ModuleType("brigade.budgets")
        brigade_budgets.DEFAULT_BOOTSTRAP_SOFT_LIMIT = 1
        brigade_budgets.DEFAULT_BOOTSTRAP_HARD_LIMIT = 2
        brigade_budgets.BOOTSTRAP_HARD_LIMIT_CEILING = 3
        sys.modules["brigade"] = brigade
        sys.modules["brigade.budgets"] = brigade_budgets

        import bootstrap_doctor.budgets as budgets

        assert budgets.DEFAULT_SOFT_LIMIT == 17_000
        assert budgets.DEFAULT_HARD_LIMIT == 20_000
        assert budgets.DEFAULT_TOTAL_LIMIT == 60_000
        print("OK")
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
