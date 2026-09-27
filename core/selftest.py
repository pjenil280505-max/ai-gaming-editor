"""C7 (docs/PHASE0.md): run the unit tests on Colab's FFmpeg. DEC-032.

The tests run in a separate Python process, so nothing the notebook imported or
changed leaks in, and the slow F10 fixture stays off. Prints progress, then one
plain result line, then every failure in full for the owner to send.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from core.run import Steps, clock, log_default

REPO = Path(__file__).resolve().parent.parent
_DONE = re.compile(r" \.\.\. (ok|FAIL|ERROR|skipped\b.*|expected failure|unexpected success)$")
_RAN = re.compile(r"^Ran (\d+) tests? in ")


def count(repo: Path = REPO, tests: str = "tests") -> int:
    """How many tests discovery finds (imports the test modules in a child process)."""
    code = ("import sys, unittest; sys.path.insert(0, sys.argv[1]); "
            "print(unittest.TestLoader().discover(sys.argv[1]).countTestCases())")
    out = subprocess.run([sys.executable, "-c", code, tests], cwd=repo, capture_output=True,
                         text=True, timeout=300)
    return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip().isdigit() else 0


def run(log: Callable[[str], None] = log_default, repo: Path = REPO, tests: str = "tests") -> bool:
    """Run every unit test; True when none failed."""
    total = count(repo, tests)
    log(f"Self-test: running {total or 'the'} unit tests on this machine's FFmpeg "
        f"(about 5–10 minutes on Colab) ...")
    env = {k: v for k, v in os.environ.items() if k != "RUN_SLOW"}
    steps = Steps(log, 10, lambda pct, _: f"  {pct}%")
    started = time.monotonic()
    lines: list[str] = []
    done = 0
    with subprocess.Popen([sys.executable, "-m", "unittest", "discover", "-s", tests, "-v"],
                          cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True) as proc:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if total and _DONE.search(line):
                done += 1
                steps(min(done / total, 0.99))
        code = proc.wait()

    ran = next((int(m.group(1)) for m in map(_RAN.match, lines) if m), 0)
    verdict = next((l for l in reversed(lines) if l.startswith(("OK", "FAILED"))), "no result line")
    took = clock(time.monotonic() - started)
    if code == 0:
        log(f"Self-test passed: {ran} tests, {verdict} in {took}.")
        return True
    log(f"Self-test FAILED: {ran} tests, {verdict} in {took}. Send everything below:")
    first = next((i for i, l in enumerate(lines) if l.startswith(("=====", "Traceback"))), None)
    for line in lines[first:] if first is not None else lines[-40:]:
        log(line)
    return False
