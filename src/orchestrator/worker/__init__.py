"""Worker spawn/teardown: FakeWorker (deterministic, for tests) and real
Agent SDK sessions (M3), both spoken through the same subprocess wire
protocol so the scheduler is backend-agnostic."""
from orchestrator.worker.fake import spawn_fake_worker
from orchestrator.worker.sdk import spawn_sdk_worker
from orchestrator.worker.worktree import create_worktree, remove_worktree

__all__ = ["spawn_fake_worker", "spawn_sdk_worker", "create_worktree", "remove_worktree"]
