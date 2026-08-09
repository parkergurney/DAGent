"""Worker spawn/teardown: FakeWorker (deterministic, for tests) and real
Agent SDK sessions (M3), both spoken through the same subprocess wire
protocol so the scheduler is backend-agnostic. worktree.py is a one-off
create/remove pair for standalone use (e.g. the verify-gate CLI grading a
single task outside the orchestrator); worktree_pool.py is what the
scheduler itself uses for concurrent batches (M5)."""
from orchestrator.worker.fake import spawn_fake_worker
from orchestrator.worker.cli import spawn_cli_worker
from orchestrator.worker.sandbox import cleanup_worker_sandbox
from orchestrator.worker.sdk import spawn_sdk_worker
from orchestrator.worker.worktree import create_worktree, remove_worktree
from orchestrator.worker.worktree_pool import WorktreePool
from orchestrator.worker.contract import build_execution_contract

__all__ = ["spawn_fake_worker", "spawn_cli_worker", "spawn_sdk_worker", "cleanup_worker_sandbox",
          "create_worktree", "remove_worktree", "WorktreePool",
          "build_execution_contract"]
