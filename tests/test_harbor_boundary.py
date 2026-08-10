"""Small contract tests for the policy and Harbor-facing boundaries."""
import asyncio
import functools
import subprocess

import pytest

from orchestrator import harbor, policies
from orchestrator.worker import WorkerIsolationError, spawn_sdk_worker
from orchestrator.store import connect, create_task
from tests.helpers import init_repo


def _run(scheduler):
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=30))


@pytest.mark.parametrize("policy, expected_limit", [
    ("sequential", 1),
    ("naive-parallel", 2),
    ("orchestrator", 2),
])
def test_execution_policies_run_through_common_scheduler(tmp_path, policy, expected_limit):
    repo = init_repo(tmp_path)
    conn = connect()
    for index in range(2):
        create_task(conn, title=f"task-{index}", brief="clean", repo=str(repo),
                    delivery_mode="scout", verify_cmd="true")

    scheduler = policies.build_scheduler(
        conn, repo, tmp_path / "worktrees", policy=policy, max_concurrency=2,
        fake_worker=True, fake_supervisor=True,
    )
    assert scheduler.max_concurrency == expected_limit
    _run(scheduler)
    states = [row["state"] for row in conn.execute("SELECT state FROM tasks")]
    assert states == ["delivered", "delivered"]


def test_orchestrator_policy_keeps_supervisor_extension_point(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    conn = connect()

    async def fake_supervisor(packet, *, model):
        del packet, model
        return None

    monkeypatch.setattr(policies, "invoke_supervisor", fake_supervisor)
    scheduler = policies.build_scheduler(
        conn, repo, tmp_path / "worktrees", policy="orchestrator",
        fake_worker=True, supervisor_model="test-model",
    )
    assert isinstance(scheduler.supervisor, functools.partial)
    assert scheduler.supervisor.func is fake_supervisor
    assert scheduler.supervisor.keywords["model"] == "test-model"


def test_harbor_run_returns_candidate_and_exportable_patch(tmp_path):
    repo = init_repo(tmp_path)
    result = asyncio.run(harbor.run_instruction(
        instruction="clean", repo_root=repo, db_path=tmp_path / "run.db",
        worktree_root=tmp_path / "worktrees", policy="sequential",
        fake_worker=True, fake_supervisor=True,
    ))

    assert result.state == "delivered"
    assert result.candidate_sha
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    patch = harbor.export_patch(repo, base_sha=base_sha, candidate_sha=result.candidate_sha)
    assert "output.txt" in patch
    assert result.metrics["attempts"] == 1


def test_real_harbor_run_requires_an_explicit_outer_boundary(tmp_path):
    repo = init_repo(tmp_path)

    with pytest.raises(WorkerIsolationError, match="external isolation boundary"):
        asyncio.run(harbor.run_instruction(
            instruction="clean", repo_root=repo, db_path=tmp_path / "run.db",
            worktree_root=tmp_path / "worktrees", policy="sequential",
        ))


def test_explicit_outer_boundary_and_trusted_development_are_distinct_opt_ins(tmp_path):
    repo = init_repo(tmp_path)

    for label, kwargs in (
        ("external", {"external_isolation": True}),
        ("trusted", {"trusted_development": True}),
    ):
        scheduler = policies.build_scheduler(
            connect(), repo, tmp_path / label,
            policy="sequential", **kwargs,
        )
        assert scheduler.spawn_worker is spawn_sdk_worker
