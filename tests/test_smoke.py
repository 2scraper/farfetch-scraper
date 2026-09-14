"""
tests/test_smoke.py
--------------------
A pytest entry point over the project's own offline suite.

smoke_test.py (repo root) is deliberately a single self-contained runner with
inline HTML/JSON fixtures, not a pytest suite — see CONTRIBUTING.md for why.
This wraps it rather than reimplementing its checks as pytest asserts, so
`pytest` and `python3 smoke_test.py` exercise the exact same code path instead
of two suites that can silently drift apart.

It used to report ONE test. The 2026-09-11 audit's complaint about that is
fair and is fixed here without touching the invariant: the suite is run once,
and every `[PASS]`/`[FAIL]` line it prints becomes its own pytest result. So
`pytest -q` now reports ~290 tests, `-k` can select one, and a failure names
the check that failed instead of attaching 290 lines of output to a single
assertion — all from a single execution of a single implementation.

Splitting smoke_test.py itself into thematic pytest modules, the other half of
what the audit proposed, is deliberately NOT done: the single-file design is
an explicit invariant of this project family, and a second copy of the checks
is exactly what this wrapper exists to avoid.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_LINE = re.compile(r"^\[(PASS|FAIL)\] (.+)$")


def _run_suite():
    """Run the offline suite once and return (results, returncode, output).

    Once, at collection time, rather than once per check: the suite is a
    single function that builds its own fixtures, and running it ~290 times
    would take ~290x as long for no extra coverage.
    """
    proc = subprocess.run(
        [sys.executable, "smoke_test.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    output = proc.stdout + proc.stderr
    results = []
    for line in output.splitlines():
        m = _LINE.match(line)
        if m:
            results.append((m.group(2), m.group(1) == "PASS"))
    return results, proc.returncode, output


_RESULTS, _RETURNCODE, _OUTPUT = _run_suite()


@pytest.mark.parametrize("label,passed",
                         _RESULTS or [("smoke suite produced no checks", False)],
                         ids=[label[:90] for label, _ in _RESULTS] or ["no-checks"])
def test_check(label, passed):
    # The label is the whole message on purpose. Attaching the suite's full
    # output to every parametrized failure would put ~290 lines under each
    # one — which is the thing this wrapper was changed to stop doing.
    # test_suite_exit_code below carries the output once, for the cases where
    # the surrounding context is what you need.
    assert passed, label


def test_suite_exit_code():
    """The suite's own verdict, which is more than the sum of its checks.

    It also fails on an unhandled exception partway through — which would
    leave the checks that DID run looking green while most never ran at all.
    Asserted separately so that case cannot pass unnoticed.
    """
    assert _RETURNCODE == 0, _OUTPUT


def test_suite_actually_ran_its_checks():
    """A floor on the number of checks collected.

    Without it, a suite that died during its very first import would collect
    zero checks, report zero failures, and read as a green run.
    """
    assert len(_RESULTS) > 250, (
        f"only {len(_RESULTS)} checks were collected from smoke_test.py — it "
        f"probably exited early.\n{_OUTPUT}")
