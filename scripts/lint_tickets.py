"""Check the Status line of every spec and ticket under .scratch/.

Rules are in docs/agents/issue-tracker.md.
"""

import re
import sys
from pathlib import Path

STATUSES = (
    "needs-triage",
    "needs-info",
    "ready-for-agent",
    "ready-for-human",
    "wontfix",
    "claimed",
    "resolved",
)
STATUS_LINE = re.compile(r"^Status: (" + "|".join(STATUSES) + r")$")
TICKET_NAME = re.compile(r"^\d{2}-[a-z0-9-]+\.md$")


def problems(root: Path) -> list[str]:
    found = []
    scratch = root / ".scratch"
    files = sorted(scratch.glob("*/spec.md")) + sorted(scratch.glob("*/issues/*.md"))
    for path in files:
        name = path.relative_to(root)
        if path.parent.name == "issues" and not TICKET_NAME.match(path.name):
            found.append(f"{name}: name the file NN-<slug>.md")
        lines = path.read_text().splitlines()
        valid = [line for line in lines if STATUS_LINE.match(line)]
        if len(valid) != 1:
            bad = [line for line in lines if line.lstrip("*").lower().startswith("status")]
            hint = f" Found: {bad[0]!r}." if bad else ""
            found.append(
                f"{name}: write exactly one line 'Status: <value>' with a value from "
                f"{', '.join(STATUSES)}.{hint}"
            )
    return found


def main() -> int:
    found = problems(Path(__file__).resolve().parent.parent)
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
