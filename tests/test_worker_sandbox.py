"""Seatbelt worker-boundary tests that do not require a Claude API call."""

import asyncio

import pytest

from orchestrator.worker import sandbox
from orchestrator.worker.sdk import spawn_sdk_worker
from tests.helpers import git, init_repo


def _git_worktree(repo, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-q", str(path), "HEAD", cwd=repo)
    return path


@pytest.mark.skipif(sandbox.platform.system() != "Darwin", reason="Seatbelt is macOS-only")
def test_profile_allows_public_worktree_but_not_its_parent_or_hidden_source(tmp_path):
    repo = init_repo(tmp_path)
    wt = _git_worktree(repo, tmp_path / "worker-slots" / "slot-0")
    worker = sandbox.prepare_worker_sandbox("task-1", wt)
    try:
        parent = str(wt.parent.resolve())
        hidden_source = str(tmp_path / "bench" / "hidden-tests")
        assert f'(allow file-read* (subpath "{wt.resolve()}"))' in worker.profile
        assert f'(allow file-write* (subpath "{wt.resolve()}"))' in worker.profile
        assert f'(allow file-read* (subpath "{parent}"))' not in worker.profile
        assert hidden_source not in worker.profile
        assert str(worker.private_dir) in worker.profile
        assert sandbox.path_is_worker_visible(wt / "public.py", wt, allowlist=worker.allowlist)
        assert not sandbox.path_is_worker_visible(tmp_path / "bench" / "hidden.py", wt,
                                                  allowlist=worker.allowlist)
    finally:
        worker.cleanup()
        git("worktree", "remove", "--force", str(wt), cwd=repo)


def test_real_worker_fails_closed_on_unsupported_host(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    with pytest.raises(sandbox.WorkerSandboxUnavailable, match="refusing to run unsandboxed"):
        asyncio.run(spawn_sdk_worker({"id": "task-1", "brief": "ignored"}, tmp_path))
