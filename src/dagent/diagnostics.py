"""Small, credential-free diagnostics for runs inside an outer harness.

The journal is written directly below Harbor's published artifact directory,
not the scheduler's private run directory. This matters when Harbor cancels
the agent process: the normal finalization path may never run, but the journal
and latest status still explain how far the runtime got.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


def _path() -> Path | None:
    value = os.environ.get("ORCH_LIVE_DIAGNOSTICS_PATH")
    return Path(value) if value else None


def live_diagnostic(event: str, **payload) -> None:
    """Append one safe phase record and update the latest status snapshot.

    Callers must pass phase metadata only; prompts, model responses, tokens,
    and arbitrary exception messages are intentionally not accepted here.
    Diagnostics are best-effort and never change task execution behavior.
    """
    path = _path()
    if path is None:
        return
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "event": str(event),
        **payload,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

        status_path = path.with_name("live_status.json")
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".live-status-", delete=False,
        ) as status_file:
            json.dump(record, status_file, sort_keys=True, indent=2)
            status_file.write("\n")
            temporary = Path(status_file.name)
        os.replace(temporary, status_path)
    except OSError:
        # Diagnostics are deliberately non-authoritative. The scheduler and
        # Harbor result artifacts remain the source of truth for task state.
        try:
            temporary.unlink(missing_ok=True)
        except (NameError, OSError):
            pass
