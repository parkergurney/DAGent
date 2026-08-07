"""Deterministic verify gate (design.md section 7). No LLM anywhere in it;
its job is turning a worker's "done" claim into evidence. Cheapest checks
first: git preflight, then a cached baseline run, then the real run, then a
flake-detecting rerun.

Pure function over VerifyRequest -> VerifyResult; run_verify() never touches
the task DB or the state machine, so it's identically usable from the
scheduler (M2) and from the standalone CLI the benchmark harness (section 10)
grades every condition -- orchestrated or not -- through.
"""
import fnmatch
import hashlib
import json
import os
import signal
import shutil
import subprocess
import tempfile
import time
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PROTECTED = ()
DATA_DIR = Path(os.environ.get("ORCH_DATA_DIR", "data"))


@dataclass
class VerifyRequest:
    task_id: str
    worktree: str
    base_sha: str
    verify_cmd: str
    hidden_cmd: str | None = None
    setup_cmd: str | None = None
    timeout_s: int = 600
    protected_paths: tuple = DEFAULT_PROTECTED
    rerun_on_fail: bool = True
    repo: str | None = None  # baseline scratch checkout; defaults to worktree's repo
    candidate_sha: str | None = None  # durable attempt ref; worker worktree may be reused
    worker_dirty: str | None = None  # status captured before disposable worktree release
    artifact_root: str | None = None  # per-run task artifact directory


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
    """Run a shell command, killing its whole process group on timeout --
    test runners orphan children that outlive a plain terminate()."""
    proc = subprocess.Popen(cmd, cwd=cwd, shell=True, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=timeout_s)
        return proc.returncode, out, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out = proc.communicate()[0] or ""
        return None, out, True


def _tail(s: str, n: int = 2000) -> str:
    return s[-n:]


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")
_ABS_PATH = re.compile(r"(?:/[A-Za-z0-9_.-]+){2,}")
_LINE_NUMBER = re.compile(r"(?<=:)[0-9]+(?=(:|\b))")


def normalize_failure_signature(cause: str, output: str = "", exit_code: int | None = None) -> str:
    """Return a deterministic signature for a public verification failure.

    Only stable failure text participates: ANSI escapes, absolute/temp paths,
    addresses, line numbers, and surrounding whitespace are removed. Hidden
    verifier output is never passed here by ``run_verify``.
    """
    text = _ANSI.sub("", output or "")
    candidates = [line.strip() for line in text.splitlines() if line.strip()]
    interesting = [line for line in candidates if re.search(
        r"assert|assertion|error|failed|failure|fail|traceback|^E(?:\s|$)",
        line, re.IGNORECASE,
    )]
    selected = (interesting[-1] if interesting else (candidates[-1] if candidates else ""))
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


def _create_verifier_worktree(req: VerifyRequest) -> tuple[Path, Path] | None:
    """Check out the worker's exact committed HEAD in a detached verifier WT.

    The worker worktree is intentionally never used as the cwd for setup or
    any test command.  ``setup_cmd`` commonly materializes hidden tests, so
    this boundary must exist even when the worker has already exited.
    """
    repo = Path(req.repo or req.worktree).resolve()
    worker = Path(req.worktree).resolve()
    head = _git("rev-parse", req.candidate_sha or "HEAD", cwd=repo if req.candidate_sha else worker)
    if head.returncode != 0 or not head.stdout.strip():
        return None
    verifier = Path(tempfile.mkdtemp(prefix="orch_verifier_"))
    add = _git("worktree", "add", "-q", "--detach", str(verifier),
                head.stdout.strip(), cwd=repo)
    if add.returncode != 0:
        shutil.rmtree(verifier, ignore_errors=True)
        return None
    return verifier, repo


def _remove_verifier_worktree(verifier: Path, repo: Path) -> None:
    _git("worktree", "remove", "--force", str(verifier), cwd=repo)
    shutil.rmtree(verifier, ignore_errors=True)


def run_verify(req: VerifyRequest) -> VerifyResult:
    t0 = time.monotonic()
    wt = req.worktree
    git_cwd = Path(req.repo or wt).resolve() if req.candidate_sha else wt
    candidate_ref = req.candidate_sha or "HEAD"

    def done(passed, cause, exit_code, output, diff_stat="", tests_modified=None, flaky=False,
             patch_path=None, feedback=None, signature=True):
        failure_signature = (
            normalize_failure_signature(cause, output, exit_code) if not passed and signature else None
        )
        return VerifyResult(
            passed=passed, cause=cause, exit_code=exit_code,
            duration_s=round(time.monotonic() - t0, 3), flaky=flaky,
            output_tail=_tail(feedback if feedback is not None else output), diff_stat=diff_stat,
            tests_modified=tests_modified or [], output_path=_save_output(req, cause, output),
            patch_path=patch_path,
            failure_signature=failure_signature,
        )

    # 1. preflight (git only, ms)
    status = req.worker_dirty or (_git("status", "--porcelain", cwd=wt).stdout
                                  if not req.candidate_sha else "")
    if status.strip():
        return done(False, "uncommitted_changes", None, status)

    diff_stat = _git("diff", "--stat", req.base_sha, candidate_ref, cwd=git_cwd).stdout
    name_status = [line.split("\t") for line in
                   _git("diff", "--name-status", req.base_sha, candidate_ref,
                        cwd=git_cwd).stdout.splitlines()
                   if line]
    diff_names = [parts[-1] for parts in name_status]
    if not diff_names:
        return done(False, "empty_diff", None, "", diff_stat)

    tests_modified = [f for f in diff_names if "test" in Path(f).name.lower()]
    patch_path = _save_patch(req,
                             _git("diff", "--binary", req.base_sha, candidate_ref,
                                  cwd=git_cwd).stdout)
    # New files under protected_paths are exempt -- only edits/deletes/renames
    # of a file that already existed at base_sha count as gaming the gate.
    modified_protected = [parts[-1] for parts in name_status
                          if parts[0] != "A"
                          and any(fnmatch.fnmatch(parts[-1], pat) for pat in req.protected_paths)]
    if modified_protected:
        return done(False, "protected_path_modified", None,
                    "\n".join(modified_protected), diff_stat, modified_protected,
                    patch_path=patch_path)

    # 2. baseline: base_sha must itself pass verify_cmd, cached on (repo, base_sha, verify_cmd)
    repo = req.repo or wt
    baseline_ok = _cached_baseline(req, repo)
    if not baseline_ok:
        return done(False, "baseline_broken", None,
                    "baseline (base_sha) does not pass verify_cmd", diff_stat, tests_modified,
                    patch_path=patch_path)

    # 3. Create a detached verifier checkout from the exact worker HEAD.
    # setup_cmd may copy hidden tests here; the worker checkout remains the
    # committed delivery artifact throughout.
    verifier_info = _create_verifier_worktree(req)
    if verifier_info is None:
        return done(False, "setup_failed", None,
                    "could not create detached verifier worktree",
                    diff_stat, tests_modified, patch_path=patch_path)
    verifier, verifier_repo = verifier_info
    try:
        # 4. setup + the visible run, only in the verifier worktree.
        if req.setup_cmd:
            code, out, timed_out = _run(req.setup_cmd, verifier, req.timeout_s)
            if timed_out or code != 0:
                return done(False, "setup_failed", code, out, diff_stat, tests_modified,
                            patch_path=patch_path, feedback="verifier setup failed")

        code, out, timed_out = _run(req.verify_cmd, verifier, req.timeout_s)
        if timed_out:
            return done(False, "timeout", None, out, diff_stat, tests_modified,
                        patch_path=patch_path)

        # 5. flake protocol: fail once, rerun; fail-fail sticks, fail-pass is flaky
        flaky = False
        if code != 0 and req.rerun_on_fail:
            code2, out2, timed_out2 = _run(req.verify_cmd, verifier, req.timeout_s)
            if not timed_out2 and code2 == 0:
                code, out, flaky = code2, out2, True
            else:
                code, out = code2, out2

        if code != 0:
            return done(False, "tests_failed", code, out, diff_stat, tests_modified, flaky,
                        patch_path=patch_path)

        if req.hidden_cmd:
            hcode, hout, htimed_out = _run(req.hidden_cmd, verifier, req.timeout_s)
            if htimed_out or hcode != 0:
                # never leak hidden output into restart feedback -- otherwise the
                # hidden suite trains the worker to overfit it (design.md section 7).
                return done(False, "hidden_tests_failed", hcode,
                            "the change didn't hold up under additional checks",
                            diff_stat, tests_modified, patch_path=patch_path, signature=False)

        return done(True, "tests_passed", code, out, diff_stat, tests_modified, flaky,
                    patch_path=patch_path)
    finally:
        _remove_verifier_worktree(verifier, verifier_repo)


def _cached_baseline(req: VerifyRequest, repo) -> bool:
    # setup_cmd is part of the key: two tasks can share (repo, base_sha,
    # verify_cmd) but run different setup, and a baseline verdict computed
    # under one setup_cmd isn't valid evidence for the other.
    key = hashlib.sha256(
        f"{repo}|{req.base_sha}|{req.verify_cmd}|{req.setup_cmd}".encode()
    ).hexdigest()
    cache_file = DATA_DIR / ".baseline_cache" / f"{key}.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())["passed"]
    ok = _run_baseline(req, repo)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"passed": ok}))
    return ok


def _run_baseline(req: VerifyRequest, repo) -> bool:
    """Run setup+verify against base_sha in a scratch worktree, so a change
    already-broken at base never burns a retry on the worker's behalf."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="orch_baseline_") as tmp:
        add = _git("worktree", "add", "-q", "--detach", tmp, req.base_sha, cwd=repo)
        if add.returncode != 0:
            return False
        try:
            if req.setup_cmd:
                code, _, timed_out = _run(req.setup_cmd, tmp, req.timeout_s)
                if timed_out or code != 0:
                    return False
            code, _, timed_out = _run(req.verify_cmd, tmp, req.timeout_s)
            return not timed_out and code == 0
        finally:
            _git("worktree", "remove", "--force", tmp, cwd=repo)
