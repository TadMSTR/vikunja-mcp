"""Cross-module isolation of the hook registry (vikunja#473).

The defect: `test_hooks.py` and `test_contrib_audit.py` cleared the registry on teardown
without restoring the built-ins, so the *next* module to rely on them passed alone and
failed in the suite. The failure looked like a bug in the code under test.

This is deliberately a **subprocess** run rather than an in-process assertion. An
in-process check could not fail: `conftest._hook_registry` puts the registry into the
shipped state at the start of every test, including this one, so any assertion made here
about the registry would be asserting on state this test's own fixture just repaired —
a control that cannot fail, which is the whole bug class this build exists to close.
Spawning pytest is the only way to observe what one module leaves for the next.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# A module that empties the registry, followed by one whose tests fail without the
# built-ins. Ordered explicitly on the command line — this does not depend on pytest's
# collection order, and it is the exact pairing that produced the v0.8.0 misdiagnosis.
CLEARING_MODULE = "tests/test_hooks.py"
DEPENDENT_MODULE = "tests/test_task_refs.py"


@pytest.mark.slow
def test_builtin_hooks_survive_a_module_that_clears_the_registry():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            CLEARING_MODULE,
            DEPENDENT_MODULE,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{DEPENDENT_MODULE} fails when run after {CLEARING_MODULE}: the hook registry "
        f"is not being restored between modules (vikunja#473).\n\n"
        f"{result.stdout[-3000:]}\n{result.stderr[-2000:]}"
    )
