"""Verifier-only checks; this file is never copied into the agent image."""

from pathlib import Path


def main() -> int:
    output = Path("/app/output.txt").read_text()
    if output != "ready\n":
        return 1
    # A candidate must contain the intended tracked-file change, not merely
    # leave an unrelated artifact that happens to satisfy a shell check.
    if not (Path("/app/output.txt").is_file() and Path("/app/.git/index").is_file()):
        return 1
    return 0


raise SystemExit(main())
