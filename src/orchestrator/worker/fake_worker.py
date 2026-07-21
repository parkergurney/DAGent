"""Scripted subprocess impersonating a Claude Code worker session
(design.md section 11, "FakeWorker (build first, in M2)").

Speaks the wire protocol real workers will speak once M3 wires up the SDK:
one JSON object per stdout line, {"type": "tool_used"|"messaged"|"asked"|
"done_claimed", "payload": {...}}. Exiting after done_claimed is a clean
finish; exiting (any code, any reason) without one is exactly what an
unclaimed worker.exited looks like on the real thing, so the scheduler needs
no scenario-specific handling -- one wire protocol, one reader.

Real workers get their instructions from `brief` as a natural-language
prompt. FakeWorker tasks use brief as a literal scenario name instead -- a
convention specific to this test harness, not part of the task schema.

Manipulates real files and git state in --worktree so the (real, unmocked)
verify gate exercises its actual git-diff logic end to end, rather than
FakeWorker asserting its own fake causes.

Run: python -m orchestrator.worker.fake_worker --scenario <name> --worktree <path>
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SCENARIOS = {}


def scenario(name):
    def deco(fn):
        SCENARIOS[name] = fn
        return fn
    return deco


def emit(type_, **payload):
    print(json.dumps({"type": type_, "payload": payload}), flush=True)


def _git(wt, *args):
    subprocess.run(
        ["git", "-C", str(wt), "-c", "user.email=fake@local", "-c", "user.name=fake", *args],
        check=True, capture_output=True, text=True,
    )


def _commit(wt, msg="work"):
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", msg)


@scenario("clean")
def _clean(wt):
    emit("tool_used", tool="Write", target="output.txt")
    (wt / "output.txt").write_text("done\n")
    _commit(wt)
    emit("done_claimed", result="DONE_CLAIM: ok")


@scenario("no_commit")
def _no_commit(wt):
    """Claims done but leaves the worktree dirty -- verify gate's cheapest
    preflight check (uncommitted_changes)."""
    emit("tool_used", tool="Write", target="output.txt")
    (wt / "output.txt").write_text("done\n")
    emit("done_claimed", result="DONE_CLAIM: ok")


@scenario("empty_diff")
def _empty_diff(wt):
    """Claims done having changed nothing -- a hallucinated completion."""
    emit("messaged", text="nothing to do here")
    emit("done_claimed", result="DONE_CLAIM: ok")


@scenario("protected_edit")
def _protected_edit(wt):
    """Commits a change under tests/ -- the anti-gaming check."""
    test_dir = wt / "tests"
    test_dir.mkdir(exist_ok=True)
    (test_dir / "test_fake.py").write_text("def test_x():\n    assert True\n")
    emit("tool_used", tool="Edit", target="tests/test_fake.py")
    _commit(wt)
    emit("done_claimed", result="DONE_CLAIM: ok")


@scenario("stall")
def _stall(wt):
    """Goes silent. The watchdog, not this script, ends the wait."""
    time.sleep(60)


@scenario("ask")
def _ask(wt):
    emit("asked", question="which logging library should I use?")
    sys.stdin.readline()  # a real reply would land here; M2 has no supervisor to send one
    (wt / "output.txt").write_text("done\n")
    _commit(wt)
    emit("done_claimed", result="DONE_CLAIM: ok")


@scenario("crash")
def _crash(wt):
    emit("tool_used", tool="Bash", target="run tests")
    sys.exit(1)


@scenario("wait")
def _wait(wt):
    """Declares an external wait, then goes silent like stall -- distinct
    event shape, same fate under M2's no-supervisor policy."""
    emit("messaged", text="waiting on external CI to finish")
    time.sleep(60)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    p.add_argument("--worktree", required=True)
    args = p.parse_args()
    SCENARIOS[args.scenario](Path(args.worktree))


if __name__ == "__main__":
    main()
