"""Scripted subprocess impersonating a Claude Code worker session
(see README.md)").

Speaks the same wire protocol real SDK workers speak:
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

Run: python -m dagent.worker.fake_worker --scenario <name> --worktree <path>
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


def _path_escapes_worktree(path_str, worktree: Path) -> bool:
    """Duplicated from sdk_worker.py's guard of the same name rather than
    imported -- the two workers deliberately don't share modules (see this
    file's docstring), so each speaks the wire protocol standalone."""
    candidate = Path(path_str)
    resolved = candidate if candidate.is_absolute() else worktree / candidate
    try:
        resolved.resolve().relative_to(worktree.resolve())
        return False
    except (ValueError, OSError):
        return True


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


@scenario("sdk_result_no_claim")
def _sdk_result_no_claim(wt):
    """Successful SDK-shaped result followed by a missing protocol marker."""
    (wt / "output.txt").write_text("done\n")
    _commit(wt, "sdk result candidate")
    emit("result", subtype="success", is_error=False, session_id="fake-sdk",
         result="completed the requested work", cost_usd=0.01,
         tokens_in=10, tokens_out=20)
    emit("unclaimed", reason="result_missing_terminal_marker")


@scenario("retry_candidate")
def _retry_candidate(wt):
    """First execution commits a failing candidate; the retry must see and
    repair that candidate instead of receiving a reset checkout."""
    marker = wt / "retry_marker.txt"
    solution = wt / "retry_solution.txt"
    if marker.exists():
        marker.unlink()
        solution.write_text("fixed\n")
        emit("tool_used", tool="Edit", target="retry_marker.txt")
        _commit(wt, "repair retained candidate")
    else:
        marker.write_text("candidate one\n")
        solution.write_text("draft\n")
        emit("tool_used", tool="Write", target="retry_marker.txt")
        _commit(wt, "candidate one")
    emit("done_claimed", result="DONE_CLAIM: candidate")


@scenario("no_commit")
def _no_commit(wt):
    """Claims done but leaves the worktree dirty -- verify gate's cheapest
    preflight check (uncommitted_changes)."""
    emit("tool_used", tool="Write", target="output.txt")
    (wt / "output.txt").write_text("done\n")
    emit("done_claimed", result="DONE_CLAIM: ok")


@scenario("verify_fail")
def _verify_fail(wt):
    """Commits a candidate that the task's visible check deliberately rejects."""
    emit("tool_used", tool="Write", target="verification_failure.txt")
    (wt / "verification_failure.txt").write_text("bad candidate\n")
    _commit(wt, "known failing candidate")
    emit("done_claimed", result="DONE_CLAIM: candidate")


@scenario("crash_dirty")
def _crash_dirty(wt):
    """Dies after an edit but before commit/normal teardown."""
    emit("tool_used", tool="Write", target="crash-draft.txt")
    (wt / "crash-draft.txt").write_text("unfinished\n")
    sys.exit(17)


@scenario("empty_diff")
def _empty_diff(wt):
    """Claims done having changed nothing -- a hallucinated completion."""
    emit("messaged", text="nothing to do here")
    emit("done_claimed", result="DONE_CLAIM: ok")


@scenario("escape_worktree")
def _escape_worktree(wt):
    """Attempts a write to an absolute path outside the worktree -- the
    batch01 regression (`sed -i` against an absolute path in the main
    checkout, dirtying it via Bash, a tool the PreToolUse hook never
    inspected). FakeWorker has no OS-level sandbox of its own, so this
    exercises the same escape-detection guard sdk_worker.py's hook applies,
    proving the check itself is correct. The hook is not a host security
    boundary; Harbor or another trusted outer environment must contain live
    workers and their visible verification."""
    target = wt.parent / "escaped.txt"
    if _path_escapes_worktree(str(target), wt):
        emit("tool_used", tool="Bash", target=str(target),
            error=f"path {str(target)!r} escapes the task worktree")
    else:
        target.write_text("escaped\n")  # only reached if the guard itself is broken
    (wt / "output.txt").write_text("done\n")
    emit("tool_used", tool="Write", target="output.txt")
    _commit(wt)
    emit("done_claimed", result="DONE_CLAIM: ok")


@scenario("stall")
def _stall(wt):
    """Goes silent. The watchdog, not this script, ends the wait."""
    time.sleep(60)


@scenario("ask")
def _ask(wt):
    emit("asked", question="which logging library should I use?")
    sys.stdin.readline()  # a real reply would land here; the fake harness sends none
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
    event shape, same fate when no supervisor is wired up."""
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
