#!/usr/bin/env python3
"""Materialize a reproducible, verifier-separated quality Harbor package.

The checked-in template contains no source fixture or hidden test.  This
builder copies pinned source commits into the worker image inputs and extracts
the selected historical hidden-test commit into the verifier-only inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
from io import BytesIO
from pathlib import Path


def _run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


def _archive(repo: Path, revision: str) -> tarfile.TarFile:
    data = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", revision],
        check=True, capture_output=True,
    ).stdout
    return tarfile.open(fileobj=BytesIO(data), mode="r:")


def _safe_extract(archive: tarfile.TarFile, destination: Path, *, strip: str = "") -> None:
    destination = destination.resolve()
    for member in archive.getmembers():
        name = member.name
        if strip:
            prefix = strip.rstrip("/") + "/"
            if name == strip:
                continue
            if not name.startswith(prefix):
                continue
            name = name[len(prefix):]
        if not name:
            continue
        target = (destination / name).resolve()
        if target != destination and destination not in target.parents:
            raise ValueError(f"archive path escapes destination: {member.name}")
        member.name = name
        archive.extract(member, destination, filter="data")


def _graph(tasks: list[dict], shape: str) -> list[dict]:
    if not tasks:
        raise ValueError("at least one quality task is required")
    by_id = {task["id"]: task for task in tasks}
    result = []
    for index, task in enumerate(tasks):
        depends = list(task.get("depends_on", []))
        if shape == "serial" and index:
            previous = tasks[index - 1]["id"]
            if previous not in depends:
                depends.append(previous)
        elif shape == "diamond" and len(tasks) >= 5:
            if index == 3:
                depends.extend(task["id"] for task in tasks[:3])
            elif index == 4:
                depends.append(tasks[3]["id"])
            elif index > 4:
                depends.append(tasks[index - 1]["id"])
        elif shape == "mixed" and len(tasks) >= 6:
            if index == 3:
                depends.extend(task["id"] for task in tasks[:2])
            elif index == 4:
                depends.extend((tasks[2]["id"], tasks[3]["id"]))
            elif index > 4:
                depends.append(tasks[index - 1]["id"])
        result.append({
            "id": task["id"],
            "title": task["title"],
            "brief": (
                f"Work on the {task['repository']} project in repos/{task['repository']}. "
                f"{task['brief']} Run the relevant public tests, inspect the result, "
                "then git add and commit only the requested fix. Do not inspect, "
                "recreate, or modify anything under /tests."
            ),
            "depends_on": list(dict.fromkeys(depends)),
            "delivery_mode": "local",
            "write_scopes": task["write_scopes"],
            "verify_cmd": "true",
            "max_retries": 1,
            "quality_task": True,
            "parallel_safe": bool(task.get("parallel_safe", False)),
        })
    for node in result:
        missing = [dependency for dependency in node["depends_on"] if dependency not in by_id]
        if missing:
            raise ValueError(f"{node['id']} depends on tasks outside the selection: {missing}")
    return result


def _selected(manifest: dict, suite: str, task_ids: list[str]) -> list[dict]:
    tasks = {task["id"]: task for task in manifest["tasks"]}
    allowed = set(manifest["suite_tasks"][suite])
    selected_ids = task_ids or list(manifest["suite_tasks"][suite])
    unknown = [task_id for task_id in selected_ids if task_id not in tasks or task_id not in allowed]
    if unknown:
        raise ValueError(f"unknown tasks for suite {suite}: {', '.join(unknown)}")
    return [tasks[task_id] for task_id in selected_ids]


def build(args: argparse.Namespace) -> dict:
    repo_root = Path(args.repo_root).resolve()
    template = repo_root / "harbor/tasks/orchestrator-quality-claude"
    source_lock = json.loads((repo_root / "bench/quality/source-lock.json").read_text())
    task_manifest = json.loads((repo_root / "bench/quality/task-manifest.json").read_text())
    source_root = Path(args.source_root).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, output)
    (output / "fixtures").mkdir()

    selected = _selected(task_manifest, args.suite, args.task_ids)
    graph = _graph(selected, args.graph_shape)
    repositories = source_lock["repositories"]
    source_digests = {}
    for name, lock in repositories.items():
        source_repo = source_root / lock["source_dir"]
        if not source_repo.is_dir():
            raise ValueError(f"missing source repository: {source_repo}")
        if _run("git", "rev-parse", "HEAD", cwd=source_repo) != lock["commit"]:
            raise ValueError(f"source commit mismatch for {name}: {source_repo}")
        if _run("git", "status", "--short", cwd=source_repo):
            raise ValueError(f"source repository is dirty: {source_repo}")
        with _archive(source_repo, lock["commit"]) as archive:
            _safe_extract(archive, output / "fixtures" / name)
        source_digests[name] = lock["commit"]

    hidden_lock = source_lock["hidden_suites"][args.suite]
    with _archive(repo_root, hidden_lock["commit"]) as archive:
        _safe_extract(archive, output / "tests" / "hidden", strip="bench/hidden-tests")

    selection = {
        "schema_version": 1,
        "suite": args.suite,
        "hidden_commit": hidden_lock["commit"],
        "graph_shape": args.graph_shape,
        "tasks": selected,
        "graph": graph,
    }
    (output / "quality_tasks.json").write_text(json.dumps(selection, indent=2, sort_keys=True) + "\n")
    (output / "graph.json").write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n")
    (output / "tests" / "selection.json").write_text(
        json.dumps(selection, indent=2, sort_keys=True) + "\n"
    )
    # Harbor builds each environment Dockerfile with that directory as its
    # build context. Keep the worker and verifier inputs in their respective
    # contexts rather than relying on Docker COPY paths outside the context.
    for context_name in ("environment", "tests"):
        context = output / context_name
        shutil.copytree(output / "fixtures", context / "fixtures")
        shutil.copy2(output / "quality_tasks.json", context / "quality_tasks.json")
    package_files = sorted(
        path for path in output.rglob("*") if path.is_file() and ".git" not in path.parts
    )
    digest = hashlib.sha256()
    for path in package_files:
        digest.update(path.relative_to(output).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    metadata = {
        "schema_version": 1,
        "suite": args.suite,
        "graph_shape": args.graph_shape,
        "task_ids": [task["id"] for task in selected],
        "source_commits": source_digests,
        "hidden_commit": hidden_lock["commit"],
        "task_package_sha256": digest.hexdigest(),
    }
    (output / "package-manifest.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--suite", choices=("original", "latest"), default="latest")
    parser.add_argument(
        "--graph-shape", choices=("task", "serial", "wide", "diamond", "mixed"), default="task"
    )
    parser.add_argument("--task", dest="task_ids", action="append", default=[])
    args = parser.parse_args()
    if args.graph_shape == "task" and len(args.task_ids) != 1:
        parser.error("--graph-shape task requires exactly one --task")
    print(json.dumps(build(args), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
