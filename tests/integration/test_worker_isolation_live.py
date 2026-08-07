"""Adversarial Seatbelt probe.

This is opt-in because managed macOS runners commonly deny sandbox creation
even though ``sandbox-exec`` exists.  It exercises the OS boundary without an
API call; the SDK worker uses the same profile and launcher.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from orchestrator.worker.sandbox import prepare_worker_sandbox
from tests.helpers import git, init_repo

pytestmark = pytest.mark.skipif(
    os.environ.get("ORCH_LIVE_SANDBOX_TESTS") != "1",
    reason="Seatbelt integration requires an explicitly permitted macOS host",
)


def test_adversarial_worker_cannot_read_or_copy_hidden_material(tmp_path):
    repo = init_repo(tmp_path)
    wt = tmp_path / "worker-slots" / "slot-0"
    wt.parent.mkdir()
    git("worktree", "add", "-q", str(wt), "HEAD", cwd=repo)
    hidden = tmp_path / "hidden-verifier"
    hidden.mkdir()
    (hidden / "known_hidden.py").write_text("SECRET_HIDDEN_CONTENT = 1\n")
    (wt / "public.txt").write_text("public content\n")
    symlink = wt / "link-to-hidden.py"
    symlink.symlink_to(hidden / "known_hidden.py")
    sink = Path(tempfile.gettempdir()) / "orch-adversarial-hidden-copy"
    sink.unlink(missing_ok=True)

    script = f'''\
import json, os, shutil, subprocess, sys
from pathlib import Path

wt = Path({str(wt)!r})
hidden = Path({str(hidden)!r})
sink = Path({str(sink)!r})
attempts = {{}}
def attempt(name, fn):
    try:
        value = fn()
        text = repr(value)
        attempts[name] = {{"failed": False, "secret": "SECRET_HIDDEN_CONTENT" in text}}
    except Exception as exc:
        attempts[name] = {{"failed": True, "secret": "SECRET_HIDDEN_CONTENT" in str(exc)}}

def require_denied(value):
    if value:
        raise RuntimeError("hidden path was traversable")
    raise PermissionError("hidden path denied")

attempt("find", lambda: subprocess.run(["find", str(hidden)], capture_output=True, text=True, check=True).stdout)
attempt("grep", lambda: subprocess.run(["grep", "-R", "SECRET_HIDDEN_CONTENT", str(hidden)], capture_output=True, text=True, check=True).stdout)
attempt("os_walk", lambda: require_denied(list(os.walk(hidden))))
attempt("rglob", lambda: require_denied(list(hidden.rglob("*"))))
attempt("absolute_read", lambda: (hidden / "known_hidden.py").read_text())
attempt("parent_traversal", lambda: (wt / "../hidden-verifier/known_hidden.py").read_text())
attempt("known_filename", lambda: (hidden / "known_hidden.py").open().read())
attempt("symlink", lambda: (wt / "link-to-hidden.py").read_text())
attempt("copy_to_tmp", lambda: shutil.copy2(hidden / "known_hidden.py", sink))
attempt("direct_execution", lambda: subprocess.run([sys.executable, str(hidden / "known_hidden.py")], check=True))

public_read = public.read_text()
(wt / "worker_edit.txt").write_text("public change\\n")
subprocess.run(["git", "add", "worker_edit.txt"], cwd=wt, check=True)
subprocess.run(["git", "commit", "-qm", "worker public change"], cwd=wt, check=True)
private = Path(tempfile.gettempdir()) / "permitted.txt"
private.write_text("private")
print(json.dumps({{"attempts": attempts, "public": public_read, "committed": (wt / "worker_edit.txt").exists(), "private": private.exists()}}))
'''
    sandbox = prepare_worker_sandbox("adversarial", wt)
    try:
        result = subprocess.run(
            sandbox.command([sys.executable, "-c", script]), cwd=wt,
            env=sandbox.environment(os.environ), capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["public"] == "public content\n"
        assert report["committed"]
        assert report["private"]
        assert all(item["failed"] and not item["secret"]
                   for item in report["attempts"].values())
        assert not sink.exists()
    finally:
        sandbox.cleanup()
        sink.unlink(missing_ok=True)
        git("worktree", "remove", "--force", str(wt), cwd=repo)
