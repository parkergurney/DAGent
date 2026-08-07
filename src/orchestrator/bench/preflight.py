"""Benchmark trust-boundary checks performed before any worker is launched."""

from __future__ import annotations

import fnmatch
import shlex
import subprocess
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


def _protected_patterns(suite) -> tuple[str, ...]:
    patterns = list(suite.protected_paths)
    for task in suite.tasks:
        patterns.extend(task.protected_paths)
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


def _reachable_material(repo: Path, patterns: tuple[str, ...]) -> list[str]:
    result = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=repo, check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise ValueError(
            "benchmark isolation preflight failed: could not inspect reachable Git history "
            f"for {repo}: {result.stderr.strip()}"
        )
    matches = []
    for line in result.stdout.splitlines():
        _, separator, path = line.partition(" ")
        if separator and any(fnmatch.fnmatch(path, pattern) for pattern in patterns):
            matches.append(path)
    return matches


def _git_common_dir(repo: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo, check=False, capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(
            "benchmark isolation preflight failed: could not resolve Git common directory "
            f"for {repo}: {result.stderr.strip()}"
        )
    value = Path(result.stdout.strip())
    return (value if value.is_absolute() else repo / value).resolve(strict=False)


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
    patterns = _protected_patterns(suite)
    roots = [repo]
    if worktrees.is_dir():
        roots.extend(child.resolve(strict=False) for child in worktrees.iterdir()
                     if child.is_dir())

    contaminated = [path for root in roots for path in _matching_material(root, patterns)]
    contaminated_history = _reachable_material(repo, patterns) if patterns else []
    if contaminated or contaminated_history:
        shown = ", ".join(str(path) for path in contaminated[:8])
        if contaminated_history:
            history_shown = ", ".join(contaminated_history[:8])
            shown = ", ".join(filter(None, (shown, history_shown)))
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
    git_common = _git_common_dir(repo)
    for slot in range(max(1, worker_slots)):
        worker = (worktrees / f"slot-{slot}").resolve(strict=False)
        allowlist = (worker, git_common)
        for source in sources:
            if path_is_worker_visible(source, worker, allowlist=allowlist):
                raise ValueError(
                    "benchmark isolation preflight failed: hidden verifier source "
                    f"{source} is inside the worker sandbox allowlist for {worker}"
                )
