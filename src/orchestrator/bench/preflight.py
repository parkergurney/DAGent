"""Benchmark trust-boundary checks performed before any worker is launched."""

from __future__ import annotations

import fnmatch
import shlex
from pathlib import Path

from orchestrator.worker.sandbox import path_is_worker_visible


def _hidden_source_paths(suite) -> tuple[Path, ...]:
    paths = [Path(p).expanduser().resolve(strict=False) for p in suite.hidden_source_paths]
    paths.extend(
        Path(p).expanduser().resolve(strict=False)
        for task in suite.tasks for p in task.hidden_source_paths
    )
    # Existing suites predate the explicit field and put an absolute source
    # directory in setup_cmd.  Keep those suites safe while making the field
    # available for new suites whose source path is not named "hidden".
    commands = [suite.setup_cmd, *(task.setup_cmd for task in suite.tasks)]
    for command in commands:
        if not command:
            continue
        for token in shlex.split(command):
            candidate = Path(token).expanduser()
            if not candidate.is_absolute() or "hidden" not in str(candidate).lower():
                continue
            paths.append(candidate.resolve(strict=False))
    return tuple(dict.fromkeys(paths))


def _hidden_patterns(suite) -> tuple[str, ...]:
    patterns = list(p for p in suite.protected_paths if "hidden" in p.lower())
    for task in suite.tasks:
        patterns.extend(p for p in task.protected_paths if "hidden" in p.lower())
    return tuple(dict.fromkeys(patterns))


def _matching_material(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not root.is_dir():
        return []
    matches = []
    for candidate in root.rglob("*"):
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        if any(fnmatch.fnmatch(relative, pattern) for pattern in patterns):
            matches.append(candidate)
    return matches


def validate_benchmark_isolation(
    suite, repo: str | Path, worktrees: str | Path, *, worker_slots: int = 1,
) -> None:
    """Reject contaminated worker-visible state without modifying it.

    This intentionally runs before the benchmark output directory is
    created/overwritten.  A historical run is evidence, not scratch state to
    clean up automatically.
    """
    repo = Path(repo).expanduser().resolve()
    worktrees = Path(worktrees).expanduser().resolve()
    patterns = _hidden_patterns(suite)
    roots = [repo]
    if worktrees.is_dir():
        roots.extend(child for child in worktrees.iterdir() if child.is_dir())

    contaminated = [path for root in roots for path in _matching_material(root, patterns)]
    if contaminated:
        shown = ", ".join(str(path) for path in contaminated[:8])
        raise ValueError(
            "benchmark isolation preflight failed: protected hidden-test material "
            f"already exists in a worker-visible repository/worktree ({shown})"
        )

    sources = _hidden_source_paths(suite)
    if not sources:
        return

    # The common Git metadata directory is worker-visible for commits even
    # though it is not the public worktree.  Runtime paths are included by
    # path_is_worker_visible; a hidden source there would defeat the boundary.
    git_common = repo / ".git"
    for slot in range(max(1, worker_slots)):
        worker = worktrees / f"slot-{slot}"
        allowlist = (worker, git_common)
        for source in sources:
            if path_is_worker_visible(source, worker, allowlist=allowlist):
                raise ValueError(
                    "benchmark isolation preflight failed: hidden verifier source "
                    f"{source} is inside the worker sandbox allowlist for {worker}"
                )
