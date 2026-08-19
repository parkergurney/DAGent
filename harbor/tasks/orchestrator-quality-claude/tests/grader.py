"""Verifier-only semantic quality scorer.

The task package copies this module only into Harbor's separate verifier image.
It runs the selected historical hidden tests against the patched source tree
and emits a fractional quality score plus credential-free per-task evidence.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def _run_task(task: dict, root: Path, hidden_root: Path) -> dict:
    repository = task["repository"]
    cwd = root / "repos" / repository
    test_path = hidden_root / task["hidden_test"]
    command = ["python3", "-m", "pytest", "-q", str(test_path)]
    if repository == "arrow":
        command[3:3] = ["-o", "addopts="]
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    return {
        "task_id": task["id"],
        "repository": repository,
        "hidden_test": task["hidden_test"],
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-2000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--selection", default="/tests/selection.json")
    parser.add_argument("--root", default="/app")
    args = parser.parse_args()
    selection = json.loads(Path(args.selection).read_text())
    root = Path(args.root)
    results = [_run_task(task, root, Path("/tests/hidden")) for task in selection["tasks"]]
    passed = sum(result["passed"] for result in results)
    total = len(results)
    score = passed / total if total else 0.0
    payload = {
        "schema_version": 1,
        "suite": selection["suite"],
        "hidden_commit": selection["hidden_commit"],
        "graph_shape": selection["graph_shape"],
        "tasks_total": total,
        "tasks_passed": passed,
        "quality_score": round(score, 6),
        "tasks": results,
    }
    Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
