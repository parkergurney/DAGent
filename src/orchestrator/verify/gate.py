"""Deterministic visible verification for a durable worker candidate.

Harbor owns hidden evaluation and scoring.  This gate only checks the public
verification command, records a normalized failure signature for retry policy,
and exports the committed candidate patch.  The public subprocess inherits the
agent environment and is not a host sandbox; it must run inside the same
trusted outer boundary as the worker when used for a benchmark.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DATA_DIR = Path(os.environ.get("ORCH_DATA_DIR", "data"))


@dataclass
class VerifyRequest:
    task_id: str
    worktree: str
    base_sha: str
    verify_cmd: str
    timeout_s: int = 600
    rerun_on_fail: bool = True
    repo: str | None = None
    candidate_sha: str | None = None
    worker_dirty: str | None = None
    artifact_root: str | None = None


@dataclass
class VerifyResult:
    passed: bool
    cause: str
    exit_code: int | None
    duration_s: float
    flaky: bool
    output_tail: str
    diff_stat: str
    tests_modified: list = field(default_factory=list)
    output_path: str | None = None
    patch_path: str | None = None
    failure_signature: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def _git(*args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _run(cmd, cwd, timeout_s) -> tuple:
    """Run a public check and clean up its entire process group on timeout."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, shell=True, start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        return proc.returncode, out, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        out = proc.communicate()[0] or ""
        return None, out, True


def _tail(value: str, limit: int = 2000) -> str:
    return value[-limit:]


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")
_ABS_PATH = re.compile(r"(?:/[A-Za-z0-9_.-]+){2,}")
_LINE_NUMBER = re.compile(r"(?<=:)[0-9]+(?=(:|\b))")


def normalize_failure_signature(cause: str, output: str = "", exit_code: int | None = None) -> str:
    """Return a stable signature suitable for retry and escalation decisions."""
    text = _ANSI.sub("", output or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    interesting = [line for line in lines if re.search(
        r"assert|assertion|error|failed|failure|fail|traceback|^E(?:\s|$)",
        line, re.IGNORECASE,
    )]
    selected = interesting[-1] if interesting else (lines[-1] if lines else "")
    selected = _ABS_PATH.sub("<path>", selected)
    selected = _HEX_ADDRESS.sub("<address>", selected)
    selected = _LINE_NUMBER.sub(":<line>", selected)
    selected = " ".join(selected.split())
    canonical = f"{cause}|{exit_code if exit_code is not None else ''}|{selected}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _artifact_dir(req: VerifyRequest) -> Path:
    return Path(req.artifact_root) if req.artifact_root else DATA_DIR / req.task_id


def _save_output(req: VerifyRequest, label: str, output: str) -> str:
    out_dir = _artifact_dir(req)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"verify_{label}_{int(time.time() * 1000)}.log"
    path.write_text(output)
    return str(path)


def _save_patch(req: VerifyRequest, text: str) -> str:
    out_dir = _artifact_dir(req)
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "review.patch"
    latest.write_text(text)
    path = out_dir / f"review_{int(time.time() * 1000)}.patch"
    path.write_text(text)
    return str(path)


def _candidate_checkout(req: VerifyRequest) -> tuple[Path, Path] | None:
    """Materialize only the committed candidate for a public check.

    This is an internal disposable checkout, not an external evaluator
    environment.  It lets the scheduler release a worker slot before
    verification while keeping verification pinned to the durable candidate.
    """
    if not req.candidate_sha:
        return None
    repo = Path(req.repo or req.worktree).resolve()
    checkout = Path(tempfile.mkdtemp(prefix="orch_candidate_"))
    head = _git("rev-parse", req.candidate_sha, cwd=repo)
    add = (_git("worktree", "add", "-q", "--detach", str(checkout), head.stdout.strip(), cwd=repo)
           if head.returncode == 0 and head.stdout.strip() else None)
    if add is None or add.returncode != 0:
        shutil.rmtree(checkout, ignore_errors=True)
        return None
    return checkout, repo


def _remove_candidate_checkout(checkout: Path, repo: Path) -> None:
    _git("worktree", "remove", "--force", str(checkout), cwd=repo)
    shutil.rmtree(checkout, ignore_errors=True)


def run_verify(req: VerifyRequest) -> VerifyResult:
    started = time.monotonic()
    worker = Path(req.worktree)
    repo = Path(req.repo or worker).resolve()
    git_cwd = repo if req.candidate_sha else worker
    candidate_ref = req.candidate_sha or "HEAD"

    def done(passed, cause, exit_code, output, diff_stat="", tests_modified=None,
             flaky=False, patch_path=None):
        signature = None if passed else normalize_failure_signature(cause, output, exit_code)
        return VerifyResult(
            passed=passed, cause=cause, exit_code=exit_code,
            duration_s=round(time.monotonic() - started, 3), flaky=flaky,
            output_tail=_tail(output), diff_stat=diff_stat,
            tests_modified=tests_modified or [], output_path=_save_output(req, cause, output),
            patch_path=patch_path, failure_signature=signature,
        )

    status = req.worker_dirty or (_git("status", "--porcelain", cwd=worker).stdout
                                  if not req.candidate_sha else "")
    if status.strip():
        return done(False, "uncommitted_changes", None, status)

    diff_stat = _git("diff", "--stat", req.base_sha, candidate_ref, cwd=git_cwd).stdout
    name_status = [line.split("\t") for line in
                   _git("diff", "--name-status", req.base_sha, candidate_ref,
                        cwd=git_cwd).stdout.splitlines() if line]
    diff_names = [parts[-1] for parts in name_status]
    if not diff_names:
        return done(False, "empty_diff", None, "", diff_stat)

    tests_modified = [name for name in diff_names if "test" in Path(name).name.lower()]
    patch_path = _save_patch(req, _git(
        "diff", "--binary", req.base_sha, candidate_ref, cwd=git_cwd).stdout
    )

    checkout_info = _candidate_checkout(req)
    if req.candidate_sha and checkout_info is None:
        return done(False, "candidate_checkout_failed", None,
                    "could not materialize the durable candidate", diff_stat,
                    tests_modified, patch_path=patch_path)
    check_cwd, check_repo = checkout_info if checkout_info else (worker, None)
    try:
        code, output, timed_out = _run(req.verify_cmd, check_cwd, req.timeout_s)
        if timed_out:
            return done(False, "timeout", None, output, diff_stat, tests_modified,
                        patch_path=patch_path)

        flaky = False
        if code != 0 and req.rerun_on_fail:
            code2, output2, timed_out2 = _run(req.verify_cmd, check_cwd, req.timeout_s)
            if not timed_out2 and code2 == 0:
                code, output, flaky = code2, output2, True
            else:
                code, output = code2, output2
        if code != 0:
            return done(False, "tests_failed", code, output, diff_stat, tests_modified,
                        flaky, patch_path=patch_path)
        return done(True, "tests_passed", code, output, diff_stat, tests_modified,
                    flaky, patch_path=patch_path)
    finally:
        if check_repo is not None:
            _remove_candidate_checkout(check_cwd, check_repo)
