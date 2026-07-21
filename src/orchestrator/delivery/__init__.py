"""Delivery dispatch: pr | local | scout (design.md section 9). "Delivered"
means artifact handed off; merge is always the captain's call, even for the
local ff-merge path below (which merges into the repo's default branch, not
a remote).
"""
import subprocess
from pathlib import Path

DATA_DIR = Path("data")


class DeliveryError(Exception):
    pass


def deliver(task: dict) -> tuple:
    """Returns (event_type, payload) for the delivery.* event. Raises
    DeliveryError on failure -- caller routes that to delivery.failed -> triage.
    """
    mode = task["delivery_mode"]
    if mode == "scout":
        return _scout(task)
    if mode == "local":
        return _local(task)
    if mode == "pr":
        return _pr(task)
    raise DeliveryError(f"unknown delivery_mode {mode!r}")


def _scout(task: dict) -> tuple:
    out_dir = DATA_DIR / task["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / "report.md"
    report.write_text(f"# {task['title']}\n\nscout task, no changes pushed.\n")
    return "delivery.report_written", {"path": str(report)}


def _local(task: dict) -> tuple:
    branch = f"task/{task['id']}"
    repo_root = task["repo"]
    current = subprocess.run(["git", "branch", "--show-current"], cwd=repo_root,
                             capture_output=True, text=True).stdout.strip()
    if current != "main":
        raise DeliveryError(f"repo not on main (on {current!r}); refusing local merge")
    proc = subprocess.run(["git", "merge", "--ff-only", branch], cwd=repo_root,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise DeliveryError(proc.stderr)
    return "delivery.merged_local", {"branch": branch}


def _pr(task: dict) -> tuple:
    branch = f"task/{task['id']}"
    wt = task["worktree"]
    proc = subprocess.run(["git", "push", "-u", "origin", branch], cwd=wt,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise DeliveryError(proc.stderr)
    proc = subprocess.run(["gh", "pr", "create", "--fill", "--head", branch], cwd=wt,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise DeliveryError(proc.stderr)
    return "delivery.pr_opened", {"url": proc.stdout.strip()}
