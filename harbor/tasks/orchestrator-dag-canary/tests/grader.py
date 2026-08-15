"""Hidden verifier for the dependency-aware canary.

This file is copied only into Harbor's verifier image, never into the agent
image. The checks intentionally validate the complete final patch rather than
any intermediate worker result.
"""

from pathlib import Path


def main() -> int:
    root = Path("/app")
    if (root / "schema.json").read_text() != '{"version": 1, "items": []}\n':
        return 1
    if (root / "README.md").read_text() != "# Dependency-aware canary baseline\nDAG canary documentation\n":
        return 1
    lib = root / "lib.py"
    integration = root / "integration.py"
    if not lib.is_file() or not integration.is_file():
        return 1
    import sys
    sys.path.insert(0, str(root))
    from integration import build
    if build([4, 2, 4, 1]) != {"version": 1, "items": [1, 2, 4]}:
        return 1
    if (root / "release.txt").read_text() != "ready\n":
        return 1
    # The final artifact must be a real Git patch result, not just files copied
    # into the verifier checkout.
    if not (root / ".git" / "index").is_file():
        return 1
    return 0


raise SystemExit(main())

