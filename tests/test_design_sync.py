"""Guards against design.md drifting from the skill files that restate it.

docs/design.md is the source of truth (see CLAUDE.md); several
.claude/skills/*/SKILL.md files intentionally duplicate parts of it so a
session can load just the relevant topic instead of the whole doc. Both
copies are wrapped in matching `<!-- sync:NAME -->` / `<!-- /sync:NAME -->`
markers. This test extracts every marked block and asserts that all copies
of a given NAME are identical (modulo markdown heading depth, which varies
because design.md nests under numbered "## N." sections while skills start
fresh at "#").
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MARKER_RE = re.compile(r"<!--\s*sync:(\S+)\s*-->(.*?)<!--\s*/sync:\1\s*-->", re.DOTALL)


def _extract_blocks(path: Path) -> dict[str, str]:
    text = path.read_text()
    blocks = {}
    for name, body in MARKER_RE.findall(text):
        # Heading depth ("##" vs "#") is allowed to differ between design.md
        # and a skill; strip leading '#'s per line so only prose is compared.
        normalized = "\n".join(
            re.sub(r"^#+", "", line).rstrip() for line in body.strip("\n").splitlines()
        )
        blocks[name] = normalized
    return blocks


def test_design_and_skills_agree_on_shared_contracts():
    files = [REPO_ROOT / "docs" / "design.md", *(REPO_ROOT / ".claude" / "skills").glob("*/SKILL.md")]

    by_marker: dict[str, list[tuple[Path, str]]] = {}
    for path in files:
        for name, content in _extract_blocks(path).items():
            by_marker.setdefault(name, []).append((path, content))

    assert by_marker, "no sync markers found - did docs/design.md or the skills move?"

    for name, copies in by_marker.items():
        assert len(copies) >= 2, f"sync:{name} only appears in {copies[0][0]}, nothing to check it against"
        source_path, source_content = copies[0]
        for path, content in copies[1:]:
            assert content == source_content, (
                f"sync:{name} differs between {source_path.relative_to(REPO_ROOT)} "
                f"and {path.relative_to(REPO_ROOT)} - update both or neither"
            )
