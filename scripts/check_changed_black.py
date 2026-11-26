#!/usr/bin/env python3
"""Check Black formatting for Python files changed by the current CI event."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ZERO_SHA = "0" * 40


def _git(*arguments: str) -> str:
    """Run Git and return its standard output, raising on command failure."""
    result = subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def changed_python_files() -> list[str]:
    """Return added or modified Python files for the current GitHub event."""
    event_name = os.getenv("CI_EVENT_NAME", "")
    head_sha = os.getenv("CI_HEAD_SHA", "HEAD")

    if event_name == "pull_request":
        base_sha = os.getenv("CI_BASE_SHA", "")
        if not base_sha:
            raise RuntimeError("CI_BASE_SHA is required for pull_request events")
        revision_range = f"{base_sha}...{head_sha}"
    elif event_name == "push":
        before_sha = os.getenv("CI_BEFORE_SHA", "")
        if not before_sha or before_sha == ZERO_SHA:
            print(
                "::notice::Initial branch push has no prior revision; "
                "Black will be enforced on subsequent pushes and pull requests."
            )
            return []
        revision_range = f"{before_sha}..{head_sha}"
    else:
        print(f"::notice::Black changed-file check is not configured for {event_name!r} events")
        return []

    output = _git(
        "diff",
        "--name-only",
        "--diff-filter=ACMRT",
        revision_range,
        "--",
        "*.py",
    )
    return sorted(path for path in output.splitlines() if path.endswith(".py"))


def main() -> int:
    """Run Black in check-only mode against changed Python files."""
    try:
        files = changed_python_files()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"::error::Unable to determine changed Python files: {exc}")
        return 2

    files = [path for path in files if Path(path).is_file()]
    if not files:
        print("No added or modified Python files; Black check is not required for this change.")
        return 0

    print(f"Checking Black formatting for {len(files)} changed Python file(s).")
    result = subprocess.run(
        ["uv", "run", "--locked", "black", "--check", "--diff", *files],
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
