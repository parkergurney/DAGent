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

from orchestrator.bench.runner import _materialize_worker_repo
from orchestrator.worker.sandbox import prepare_worker_sandbox
from tests.helpers import git, init_repo

pytestmark = pytest.mark.skipif(
    os.environ.get("ORCH_LIVE_SANDBOX_TESTS") != "1",
    reason="Seatbelt integration requires an explicitly permitted macOS host",
)


def test_adversarial_worker_cannot_read_or_copy_hidden_material(tmp_path, monkeypatch):
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
    monkeypatch.setenv("ORCH_AUDIT_HIDDEN_SECRET", "SECRET_ENV_CONTENT")

    script = f'''\
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

wt = Path({str(wt)!r})
public = Path({str(wt / "public.txt")!r})
hidden = Path({str(hidden)!r})
sink = Path({str(sink)!r})
attempts = {{}}
def attempt(name, fn):
    try:
        value = fn()
        text = repr(value)
        attempts[name] = {{"failed": False, "secret": any(secret in text for secret in
                          ("SECRET_HIDDEN_CONTENT", "SECRET_ENV_CONTENT"))}}
    except Exception as exc:
        attempts[name] = {{"failed": True, "secret": any(secret in str(exc) for secret in
                          ("SECRET_HIDDEN_CONTENT", "SECRET_ENV_CONTENT"))}}

def require_denied(value):
    if value:
        raise RuntimeError("hidden path was traversable")
    raise PermissionError("hidden path denied")

def grep_hidden():
    result = subprocess.run(["grep", "-R", "SECRET_HIDDEN_CONTENT", str(hidden)],
                            capture_output=True, text=True)
    if result.returncode:
        raise PermissionError(result.stderr)
    return result.stdout

attempt("find", lambda: subprocess.run(["find", str(hidden)], capture_output=True, text=True, check=True).stdout)
attempt("grep", grep_hidden)
attempt("os_walk", lambda: require_denied(list(os.walk(hidden))))
attempt("rglob", lambda: require_denied(list(hidden.rglob("*"))))
attempt("absolute_read", lambda: (hidden / "known_hidden.py").read_text())
attempt("parent_traversal", lambda: (wt / "../hidden-verifier/known_hidden.py").read_text())
attempt("known_filename", lambda: (hidden / "known_hidden.py").open().read())
attempt("symlink", lambda: (wt / "link-to-hidden.py").read_text())
attempt("copy_to_tmp", lambda: shutil.copy2(hidden / "known_hidden.py", sink))
attempt("direct_execution", lambda: subprocess.run([sys.executable, str(hidden / "known_hidden.py")], check=True))
attempt("environment", lambda: os.environ["ORCH_AUDIT_HIDDEN_SECRET"])

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
                   for item in report["attempts"].values()), report["attempts"]
        assert not sink.exists()
    finally:
        sandbox.cleanup()
        sink.unlink(missing_ok=True)
        git("worktree", "remove", "--force", str(wt), cwd=repo)


def test_adversarial_worker_cannot_read_hidden_git_history(tmp_path):
    """A hidden file must not be recoverable from the worker's Git object DB."""
    repo = init_repo(tmp_path)
    (repo / "historical_hidden.py").write_text("SECRET_GIT_CONTENT = 1\n")
    git("add", "historical_hidden.py", cwd=repo)
    git("commit", "-qm", "temporary hidden material", cwd=repo)
    (repo / "historical_hidden.py").unlink()
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "remove temporary material", cwd=repo)

    worker_repo = _materialize_worker_repo(repo, "main", tmp_path / "worker-repo")
    wt = tmp_path / "worker-slots" / "slot-0"
    wt.parent.mkdir()
    git("worktree", "add", "-q", str(wt), "HEAD", cwd=worker_repo)
    script = f'''\
import json, os, subprocess
from pathlib import Path
wt = Path({str(wt)!r})
attempts = {{}}
def attempt(name, fn):
    try:
        value = fn()
        attempts[name] = {{"failed": False, "secret": "SECRET_GIT_CONTENT" in repr(value)}}
    except Exception as exc:
        attempts[name] = {{"failed": True, "secret": "SECRET_GIT_CONTENT" in str(exc)}}
attempt("log", lambda: subprocess.run(["git", "log", "--all", "--", "historical_hidden.py"],
                                      cwd=wt, capture_output=True, text=True, check=True).stdout)
attempt("show", lambda: subprocess.run(["git", "show", "HEAD:historical_hidden.py"],
                                        cwd=wt, capture_output=True, text=True, check=True).stdout)
attempt("cat_file", lambda: subprocess.run(["git", "cat-file", "-p", "HEAD:historical_hidden.py"],
                                             cwd=wt, capture_output=True, text=True, check=True).stdout)
attempt("log_patch", lambda: subprocess.run(["git", "log", "--all", "-p"],
                                              cwd=wt, capture_output=True, text=True, check=True).stdout)
attempt("rev_list_objects", lambda: subprocess.run(["git", "rev-list", "--all", "--objects"],
                                                      cwd=wt, capture_output=True, text=True, check=True).stdout)
attempt("fsck", lambda: subprocess.run(["git", "fsck", "--full", "--no-reflogs", "--unreachable"],
                                        cwd=wt, capture_output=True, text=True).stdout)
attempt("refs", lambda: subprocess.run(["git", "show-ref"], cwd=wt,
                                        capture_output=True, text=True).stdout)
attempt("tags", lambda: subprocess.run(["git", "tag", "-l"], cwd=wt,
                                        capture_output=True, text=True).stdout)
attempt("remotes", lambda: subprocess.run(["git", "remote", "-v"], cwd=wt,
                                           capture_output=True, text=True).stdout)
attempt("reflogs", lambda: subprocess.run(["git", "reflog", "--all"], cwd=wt,
                                           capture_output=True, text=True).stdout)
attempt("git_metadata_scan", lambda: [
    path.read_bytes() for root, dirs, files in os.walk(wt / ".git")
    for path in [Path(root) / name for name in files]
])
attempt("object_store_scan", lambda: [
    path.read_bytes() for path in (wt / ".git").rglob("*") if path.is_file()
])
print(json.dumps(attempts))
'''
    sandbox = prepare_worker_sandbox("git-history", wt)
    try:
        result = subprocess.run(sandbox.command([sys.executable, "-c", script]), cwd=wt,
                                env=sandbox.environment(os.environ), capture_output=True,
                                text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        attempts = json.loads(result.stdout)
        assert all(not item["secret"] for item in attempts.values()), attempts
    finally:
        sandbox.cleanup()
        git("worktree", "remove", "--force", str(wt), cwd=worker_repo)


def test_sandbox_denies_detached_child_escape(tmp_path):
    """Seatbelt must deny setsid before a child can leave worker teardown."""
    repo = init_repo(tmp_path)
    wt = tmp_path / "worker-slots" / "slot-0"
    wt.parent.mkdir()
    git("worktree", "add", "-q", str(wt), "HEAD", cwd=repo)
    sandbox = prepare_worker_sandbox("setsid-probe", wt)
    try:
        marker = sandbox.private_dir / "detached.pid"
        status = sandbox.private_dir / "setsid.status"
        script = f'''\
import os, sys, time
marker = {str(marker)!r}
status = {str(status)!r}
child = os.fork()
if child == 0:
    try:
        os.setsid()
    except PermissionError:
        open(status, "w").write("denied")
        os._exit(0)
    open(marker, "w").write(str(os.getpid()))
    time.sleep(30)
    os._exit(0)
print("parent-exited", flush=True)
os._exit(0)
'''
        result = subprocess.run(
            sandbox.command([sys.executable, "-c", script]), cwd=wt,
            env=sandbox.environment(os.environ), capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert status.read_text() == "denied"
        assert not marker.exists()
    finally:
        sandbox.cleanup()
        git("worktree", "remove", "--force", str(wt), cwd=repo)
