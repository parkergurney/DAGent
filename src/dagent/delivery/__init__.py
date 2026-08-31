"""Delivery dispatch: pr | local | scout (see README.md). "Delivered"
means artifact handed off; merge is always the manager's call, even for the
local ff-merge path below (which merges into the repo's default branch, not
a remote).
"""
import subprocess
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path("data")


class DeliveryError(Exception):
    pass


def deliver(task: dict, *, open_pr=None, artifact_root=None) -> tuple:
    """Returns (event_type, payload) for the delivery.* event. Raises
    DeliveryError on failure -- caller routes that to delivery.failed -> triage.

    `open_pr` (worktree, branch) -> url is injectable so pr mode's git-push
    half (fully real, testable against a local bare-repo remote) can be
    exercised without needing gh/GitHub -- defaults to the real `gh pr create`.
    """
    mode = task["delivery_mode"]
    if mode == "scout":
        return _scout(task, artifact_root=artifact_root)
    if mode == "local":
        return _local(task)
    if mode == "pr":
        return _pr(task, open_pr=open_pr or _gh_pr_create)
    raise DeliveryError(f"unknown delivery_mode {mode!r}")


def _scout(task: dict, *, artifact_root=None) -> tuple:
    out_dir = Path(artifact_root) if artifact_root else DATA_DIR / task["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / "report.md"
    report.write_text(f"# {task['title']}\n\nscout task, no changes pushed.\n")
    return "delivery.report_written", {"path": str(report)}


def _rev_parse(ref: str, *, cwd) -> str:
    proc = subprocess.run(["git", "rev-parse", ref], cwd=cwd,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise DeliveryError(proc.stderr)
    return proc.stdout.strip()


def _local(task: dict) -> tuple:
    branch = task.get("candidate_branch") or f"task/{task['id']}"
    repo_root = task["repo"]

    current = subprocess.run(["git", "branch", "--show-current"], cwd=repo_root,
                             capture_output=True, text=True).stdout.strip()
    if current != "main":
        raise DeliveryError(f"repo not on main (on {current!r}); refusing local merge")

    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root,
                            capture_output=True, text=True)
    if status.stdout.strip():
        raise DeliveryError(f"dirty_tree: uncommitted changes in {repo_root}:\n{status.stdout}")

    before_sha = _rev_parse("main", cwd=repo_root)
    commit_sha = _rev_parse(branch, cwd=repo_root)
    merge = subprocess.run(["git", "merge", "--ff-only", branch], cwd=repo_root,
                           capture_output=True, text=True)
    if merge.returncode == 0:
        after_sha = _rev_parse("main", cwd=repo_root)
        return "delivery.merged_local", {
            "branch": branch,
            "before_sha": before_sha,
            "after_sha": after_sha,
            "commit_sha": commit_sha,
        }

    # main moved past the branch point: rebase onto main's current SHA and retry.
    main_sha = subprocess.run(["git", "rev-parse", "main"], cwd=repo_root,
                              capture_output=True, text=True).stdout.strip()
    # The scheduler's worker checkout is disposable and may already have been
    # reused. Rebase the durable branch in a private checkout unless the
    # caller supplied a checkout that is still on this branch.
    rebase_wt = task.get("worktree")
    owned_temp = False
    if not rebase_wt or not Path(rebase_wt).exists() or subprocess.run(
        ["git", "branch", "--show-current"], cwd=rebase_wt,
        capture_output=True, text=True,
    ).stdout.strip() != branch:
        rebase_wt = tempfile.mkdtemp(prefix="orch_delivery_")
        add = subprocess.run(["git", "worktree", "add", "-q", str(rebase_wt), branch],
                             cwd=repo_root, capture_output=True, text=True)
        if add.returncode != 0:
            shutil.rmtree(rebase_wt, ignore_errors=True)
            raise DeliveryError(add.stderr)
        owned_temp = True
    rebase = subprocess.run(
        ["git", "-c", "user.name=dagent", "-c", "user.email=dagent@localhost",
         "rebase", main_sha], cwd=rebase_wt, capture_output=True, text=True)
    if rebase.returncode != 0:
        subprocess.run(["git", "rebase", "--abort"], cwd=rebase_wt,
                       capture_output=True, text=True)
        if owned_temp:
            subprocess.run(["git", "worktree", "remove", "--force", rebase_wt],
                           cwd=repo_root, capture_output=True, text=True)
            shutil.rmtree(rebase_wt, ignore_errors=True)
        raise DeliveryError(f"rebase_conflict: {rebase.stderr}\noriginal: {merge.stderr}")

    if owned_temp:
        subprocess.run(["git", "worktree", "remove", "--force", rebase_wt],
                       cwd=repo_root, capture_output=True, text=True)
        shutil.rmtree(rebase_wt, ignore_errors=True)

    # NOTE: verification ran against the pre-rebase base. See devlog —
    # re-verification after rebase is required before this is sound.
    remerge = subprocess.run(["git", "merge", "--ff-only", branch], cwd=repo_root,
                             capture_output=True, text=True)
    if remerge.returncode != 0:
        raise DeliveryError(remerge.stderr)
    after_sha = _rev_parse("main", cwd=repo_root)
    return "delivery.merged_local", {
        "branch": branch,
        "before_sha": before_sha,
        "after_sha": after_sha,
        "commit_sha": _rev_parse(branch, cwd=repo_root),
        "rebased": True,
        "original_commit_sha": commit_sha,
    }


def _pr(task: dict, *, open_pr) -> tuple:
    branch = f"task/{task['id']}"
    source_ref = task.get("candidate_branch") or branch
    repo = task.get("repo") or task["worktree"]
    if source_ref != branch:
        materialize = subprocess.run(["git", "branch", "-f", branch, source_ref],
                                     cwd=repo, capture_output=True, text=True)
        if materialize.returncode != 0:
            raise DeliveryError(materialize.stderr)
    commit_sha = _rev_parse(branch, cwd=repo)
    proc = subprocess.run(
        ["git", "push", "-u", "origin", f"{branch}:refs/heads/{branch}"], cwd=repo,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise DeliveryError(proc.stderr)
    try:
        url = open_pr(repo, branch)
    except DeliveryError:
        raise
    except Exception as e:
        raise DeliveryError(str(e)) from e
    return "delivery.pr_opened", {"url": url, "branch": branch, "commit_sha": commit_sha}


def _gh_pr_create(wt, branch: str) -> str:
    proc = subprocess.run(["gh", "pr", "create", "--fill", "--head", _head_arg(wt, branch)], cwd=wt,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise DeliveryError(proc.stderr)
    return proc.stdout.strip()


def _head_arg(wt, branch: str) -> str:
    """For fork PRs, `gh pr create --head branch` may look in the upstream
    repo instead of the fork. Qualify with the origin owner when possible.
    """
    proc = subprocess.run(["git", "remote", "get-url", "origin"], cwd=wt,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return branch
    owner = _github_owner_from_remote(proc.stdout.strip())
    return f"{owner}:{branch}" if owner else branch


def _github_owner_from_remote(url: str) -> str | None:
    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
    else:
        parsed = urlparse(url)
        if parsed.netloc != "github.com":
            return None
        path = parsed.path.lstrip("/")
    parts = path.removesuffix(".git").split("/")
    return parts[0] if len(parts) >= 2 and parts[0] else None
