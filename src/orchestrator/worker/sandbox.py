"""Fail-closed macOS Seatbelt boundary for real SDK workers.

The Claude SDK sandbox protects commands launched by Claude Code.  This
boundary protects the worker subprocess itself, including Python code and
commands which do not pass through Claude's Bash tool.  It is deliberately
small: a worker gets its public worktree, the Git metadata needed to commit,
the Python/SDK/orchestrator runtime, and one private temporary directory.

Fake workers do not use this module.  They are deterministic test fixtures,
not an untrusted execution boundary.
"""

from __future__ import annotations

import platform
import json
import shutil
import string
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import dataclass
from pathlib import Path


class WorkerSandboxUnavailable(RuntimeError):
    """The real-worker OS boundary cannot be constructed safely."""


# sdk.py -> worker -> orchestrator -> src
_ORCHESTRATOR_SRC = Path(__file__).resolve().parents[2]
_PRIVATE_DIRS: dict[int, Path] = {}
_ENV_EXACT = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TERM_PROGRAM", "COLORTERM",
    "LANG", "VIRTUAL_ENV", "SYSTEM_VERSION_COMPAT", "__CF_USER_TEXT_ENCODING",
}
_ENV_PREFIXES = ("LC_", "ANTHROPIC_", "CLAUDE_")


def _resolve_existing(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _home_dir() -> Path:
    return Path.home()


def _copy_private_file(source: Path, destination: Path) -> None:
    """Copy one operator-owned auth file without following a symlink."""
    if not source.is_file() or source.is_symlink():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    # Auth material must not inherit a permissive mode from the operator's
    # config tree.  The worker needs to read it, but no other user should.
    destination.chmod(0o600)


def _stage_claude_auth(private_dir: Path) -> None:
    """Stage only the Claude authentication inputs needed by a worker.

    Claude Code stores credentials in ``~/.claude/.credentials.json`` on
    Linux/Windows and in the macOS Keychain.  The latter is not a file we can
    safely copy; Claude Code can consult the Keychain directly.  ``~/.claude``
    is deliberately never copied.  On all platforms, retain only the
    account metadata Claude Code uses to associate OAuth credentials with the
    logged-in account; do not copy the host's project history or settings.
    """
    config_dir = private_dir / "claude-config"
    config_dir.mkdir(mode=0o700)

    credentials = _home_dir() / ".claude" / ".credentials.json"
    _copy_private_file(credentials, config_dir / ".credentials.json")

    # Claude Code's global config lives beside ~/.claude.  Recreate only its
    # OAuth account metadata under the isolated HOME; the original file also
    # contains project history, cached usage, and other operator state.
    global_config = _home_dir() / ".claude.json"
    if global_config.is_file() and not global_config.is_symlink():
        try:
            payload = json.loads(global_config.read_text())
        except (OSError, ValueError):
            payload = {}
        account = payload.get("oauthAccount")
        if isinstance(account, dict):
            isolated_config = private_dir / ".claude.json"
            isolated_config.write_text(json.dumps({"oauthAccount": account}, separators=(",", ":")))
            isolated_config.chmod(0o600)


def _git_path(worktree: Path, *args: str) -> Path:
    result = subprocess.run(
        ["git", *args], cwd=worktree, check=False,
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise WorkerSandboxUnavailable(
            f"could not resolve Git metadata for worker worktree {worktree}: "
            f"{result.stderr.strip()}"
        )
    value = Path(result.stdout.strip())
    return _resolve_existing(value if value.is_absolute() else worktree / value)


def _runtime_paths() -> set[Path]:
    """Return directories/files required before the SDK can start Claude."""
    paths: set[Path] = {_ORCHESTRATOR_SRC, Path(sys.executable), _resolve_existing(sys.executable)}
    python_bin = Path(sys.executable).resolve().parent
    paths.add(python_bin)

    # Do not allowlist arbitrary import roots from the caller.  In particular,
    # pytest and programmatic users commonly put the repository root on
    # ``sys.path``/``PYTHONPATH``; doing so here would expose the benchmark
    # checkout, including its hidden-test source, to the worker.  The
    # interpreter's standard/runtime locations are covered explicitly below,
    # and the orchestrator source is covered by ``_ORCHESTRATOR_SRC``.
    for value in sysconfig.get_paths().values():
        if value:
            paths.add(_resolve_existing(value))

    framework_prefix = sysconfig.get_config_var("PYTHONFRAMEWORKINSTALLNAMEPREFIX")
    framework_name = sysconfig.get_config_var("PYTHONFRAMEWORK")
    if framework_prefix and framework_name:
        # On macOS, the interpreter is dynamically linked against this
        # framework binary, which is not covered by sys.path or sysconfig
        # install directories.
        paths.add(_resolve_existing(Path(framework_prefix) / framework_name))

    try:
        import claude_agent_sdk

        paths.add(_resolve_existing(claude_agent_sdk.__file__))
        paths.add(_resolve_existing(Path(claude_agent_sdk.__file__).parent))
    except (ImportError, TypeError):
        # The parent has already imported the SDK in the normal path.  Keep
        # this helper independently testable; an absent SDK is reported by
        # the worker process itself when it starts.
        pass

    for command in ("git", "sh", "bash", "env"):
        resolved = shutil.which(command)
        if resolved:
            paths.update((Path(resolved), _resolve_existing(resolved)))
    claude = shutil.which("claude")
    if claude:
        paths.update((Path(claude), _resolve_existing(claude)))
    _add_linked_runtime_paths(paths)
    return paths


def _add_linked_runtime_paths(paths: set[Path]) -> None:
    """Include non-system dynamic libraries needed by runtime executables."""
    if platform.system() != "Darwin":
        return
    otool = shutil.which("otool")
    if not otool:
        return

    pending = [path for path in paths if path.is_file()]
    inspected: set[Path] = set()
    while pending:
        path = _resolve_existing(pending.pop())
        if path in inspected:
            continue
        inspected.add(path)
        result = subprocess.run(
            [otool, "-L", str(path)], check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines()[1:]:
            dependency = line.strip().split(" (", 1)[0]
            if not dependency.startswith("/"):
                continue
            library = _resolve_existing(dependency)
            if not library.is_file() or library in paths:
                continue
            paths.add(library)
            pending.append(library)


def _sb_string(path: Path) -> str:
    """Quote a path as a Seatbelt string literal."""
    value = str(path)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class WorkerSandbox:
    worktree: Path
    private_dir: Path
    sandbox_exec: Path
    allowlist: tuple[Path, ...]
    profile: str

    @classmethod
    def create(cls, task_id: str, worktree: str | Path) -> "WorkerSandbox":
        if platform.system() != "Darwin":
            raise WorkerSandboxUnavailable(
                "real SDK workers require macOS Seatbelt; refusing to run unsandboxed "
                f"on {platform.system() or sys.platform}"
            )
        sandbox_exec = shutil.which("sandbox-exec")
        if not sandbox_exec:
            raise WorkerSandboxUnavailable(
                "macOS sandbox-exec is unavailable; refusing to run an unsandboxed worker"
            )

        wt = _resolve_existing(worktree)
        if not wt.is_dir():
            raise WorkerSandboxUnavailable(f"worker worktree does not exist: {wt}")
        try:
            git_dir = _git_path(wt, "rev-parse", "--git-dir")
            git_common_dir = _git_path(wt, "rev-parse", "--git-common-dir")
            safe_task_id = "".join(
                char if char in string.ascii_letters + string.digits + "-_" else "_"
                for char in str(task_id)
            )
            private_dir = Path(tempfile.mkdtemp(
                prefix=f".orch-worker-{safe_task_id}-", dir=wt.parent,
            )).resolve()
            _stage_claude_auth(private_dir)
            allowed = {wt, git_dir, git_common_dir, private_dir, *_runtime_paths()}
            allowlist = tuple(sorted(allowed, key=str))
            profile = _profile(allowlist, private_dir)
            return cls(wt, private_dir, _resolve_existing(sandbox_exec), allowlist, profile)
        except Exception as exc:
            # Do not leave a private directory behind when preflight itself
            # fails.  No worker has been launched at this point.
            if "private_dir" in locals():
                shutil.rmtree(private_dir, ignore_errors=True)
            if isinstance(exc, WorkerSandboxUnavailable):
                raise
            raise WorkerSandboxUnavailable("failed to construct worker Seatbelt profile") from exc

    def command(self, args: list[str]) -> list[str]:
        return [str(self.sandbox_exec), "-p", self.profile, *args]

    def environment(self, base: dict[str, str]) -> dict[str, str]:
        # Do not inherit arbitrary operator/benchmark variables.  The worker
        # does not run setup or verification, so it needs only normal process
        # runtime settings plus Claude authentication/configuration variables.
        # In particular, ORCH_DATA_DIR and ad-hoc hidden/verifier variables
        # must not become a worker-side discovery channel.
        env = {
            key: value for key, value in base.items()
            if key in _ENV_EXACT or any(key.startswith(prefix) for prefix in _ENV_PREFIXES)
        }
        # Keep Claude Code's global config and any credential file inside the
        # worker's private directory.  This prevents a child from reading the
        # operator's full ~/.claude tree through HOME-based lookups.
        env["HOME"] = str(self.private_dir)
        env["PYTHONPATH"] = str(_ORCHESTRATOR_SRC)
        # Claude and Python must not use a shared /tmp.  CLAUDE_CONFIG_DIR is
        # also private so a worker cannot read or alter the operator's CLI
        # state.  Git identity is supplied without consulting ~/.gitconfig.
        env["TMPDIR"] = str(self.private_dir)
        env["TMP"] = str(self.private_dir)
        env["TEMP"] = str(self.private_dir)
        env["CLAUDE_CONFIG_DIR"] = str(self.private_dir / "claude-config")
        env["XDG_CONFIG_HOME"] = str(self.private_dir / "xdg-config")
        env["GIT_CONFIG_COUNT"] = "2"
        env["GIT_CONFIG_KEY_0"] = "user.name"
        env["GIT_CONFIG_VALUE_0"] = "orchestrator worker"
        env["GIT_CONFIG_KEY_1"] = "user.email"
        env["GIT_CONFIG_VALUE_1"] = "worker@localhost"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        return env

    def cleanup(self) -> None:
        shutil.rmtree(self.private_dir, ignore_errors=True)


def _profile(allowlist: tuple[Path, ...], private_dir: Path) -> str:
    """Build a deny-by-default Seatbelt profile.

    ``system.sb`` supplies the OS facilities needed to start a normal
    process; it does not grant access to arbitrary user directories.  All
    project/runtime file access is then explicitly scoped to the allowlist.
    There is intentionally no network allow rule.
    """
    lines = [
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        "(allow process-fork)",
        "(allow process-exec)",
        # Keep every descendant in the worker's process group.  Without these
        # syscall denials, setsid()/setpgrp() lets a child escape killpg()
        # while retaining the worker's filesystem sandbox.
        "(deny syscall-unix (syscall-number SYS_setsid SYS_setpgid))",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow file-read-metadata (subpath \"/\"))",
    ]
    for path in allowlist:
        quoted = _sb_string(path)
        rule = "literal" if path.is_file() else "subpath"
        lines.append(f"(allow file-read* ({rule} {quoted}))")
    for path in (private_dir,):
        lines.append(f"(allow file-write* (subpath {_sb_string(path)}))")
    # Worktree and Git metadata write rules are appended by
    # prepare_worker_sandbox(), which knows which allowlist entries are
    # project metadata. Runtime files remain read-only.
    return "\n".join(lines) + "\n"


def prepare_worker_sandbox(task_id: str, worktree: str | Path) -> WorkerSandbox:
    """Public construction entry point used by every real SDK spawn."""
    sandbox = WorkerSandbox.create(task_id, worktree)
    # Add write capability only for the public worktree and Git metadata.
    writable = {sandbox.worktree}
    for candidate in sandbox.allowlist:
        if ".git" in candidate.parts or candidate.name == ".git":
            writable.add(candidate)
    suffix = "".join(f"(allow file-write* (subpath {_sb_string(p)}))\n" for p in sorted(writable, key=str))
    sandbox.profile += suffix
    return sandbox


def register_worker_sandbox(proc, sandbox: WorkerSandbox) -> None:
    _PRIVATE_DIRS[proc.pid] = sandbox.private_dir


def cleanup_worker_sandbox(proc) -> None:
    private_dir = _PRIVATE_DIRS.pop(getattr(proc, "pid", -1), None)
    if private_dir is not None:
        shutil.rmtree(private_dir, ignore_errors=True)


def path_is_worker_visible(path: str | Path, worktree: str | Path, *, allowlist=None) -> bool:
    """Whether ``path`` lies under a worker's file-read allowlist.

    This is a preflight assertion helper; it never expands an allowlist by
    following a hidden source path.
    """
    candidate = _resolve_existing(path)
    if allowlist is None:
        # For benchmark preflight we do not need Git metadata or a temp dir.
        roots = (_resolve_existing(worktree), *_runtime_paths())
    else:
        roots = tuple(_resolve_existing(root) for root in allowlist) + tuple(_runtime_paths())
    return any(candidate == root or root in candidate.parents for root in roots)
