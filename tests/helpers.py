"""Shared git-repo scaffolding for M2 tests (scheduler + verify gate), plus a
scripted supervisor double for M4 scheduler-dispatch tests."""
import subprocess
from pathlib import Path

from dagent.supervisor.llm import SupervisorResult


def git(*args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@local", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
    )


def init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("toy repo\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    return repo


class ScriptedSupervisor:
    """A deterministic supervisor double: returns one pre-built action per
    call, in order. Raises if called more times than scripted -- an
    unexpected extra triage entry is a test bug, not something to paper
    over with a default."""

    def __init__(self, actions):
        self._actions = list(actions)
        self.packets = []

    async def __call__(self, packet):
        self.packets.append(packet)
        action = self._actions.pop(0)
        return SupervisorResult(action=action, ok=True, tokens_in=1, tokens_out=1,
                                cost_usd=0.001, raw_text=None)
