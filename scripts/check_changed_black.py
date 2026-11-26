#!/usr/bin/env python3
"""Check Black formatting for changed Python lines in the current CI event."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ZERO_SHA = "0" * 40
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(*arguments: str) -> str:
    """Run Git and return its standard output, raising on command failure."""
    result = subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _revision_range() -> str | None:
    """Resolve the base/head range for the current supported GitHub event."""
    event_name = os.getenv("CI_EVENT_NAME", "")
    head_sha = os.getenv("CI_HEAD_SHA", "HEAD")
    if event_name == "pull_request":
        base_sha = os.getenv("CI_BASE_SHA", "")
        if not base_sha:
            raise RuntimeError("CI_BASE_SHA is required for pull_request events")
        return f"{base_sha}...{head_sha}"
    if event_name == "push":
        before_sha = os.getenv("CI_BEFORE_SHA", "")
        if not before_sha or before_sha == ZERO_SHA:
            print(
                "::notice::Initial branch push has no prior revision; "
                "Black will be enforced on subsequent pushes and pull requests."
            )
            return None
        return f"{before_sha}..{head_sha}"
    print(f"::notice::Black changed-line check is not configured for {event_name!r} events")
    return None


def _changed_line_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Extract inclusive, context-expanded new-file ranges from a zero-context diff."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current_path: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            continue
        match = _HUNK_RE.match(line)
        if match and current_path:
            start = int(match.group(1))
            count = int(match.group(2) or "1")
            if count == 0:
                continue
            # Include one surrounding line so Black can format a statement whose
            # syntax spans a hunk boundary. Black may expand farther when needed.
            ranges.setdefault(current_path, []).append((max(1, start - 1), start + count))

    merged: dict[str, list[tuple[int, int]]] = {}
    for path, file_ranges in ranges.items():
        ordered = sorted(file_ranges)
        combined: list[list[int]] = []
        for start, end in ordered:
            if combined and start <= combined[-1][1] + 1:
                combined[-1][1] = max(combined[-1][1], end)
            else:
                combined.append([start, end])
        merged[path] = [(start, end) for start, end in combined]
    return merged


def main() -> int:
    """Run Black in check-only mode for each changed Python file/range."""
    try:
        revision_range = _revision_range()
        if revision_range is None:
            return 0
        diff_text = _git(
            "diff",
            "--no-ext-diff",
            "--unified=0",
            "--diff-filter=ACMRT",
            revision_range,
            "--",
            "*.py",
        )
        ranges_by_file = _changed_line_ranges(diff_text)
        added_files = set(
            _git(
                "diff",
                "--name-only",
                "--diff-filter=A",
                revision_range,
                "--",
                "*.py",
            ).splitlines()
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"::error::Unable to determine changed Python lines: {exc}")
        return 2

    changed_files = sorted(path for path in ranges_by_file if Path(path).is_file())
    if not changed_files:
        print("No added or modified Python lines; Black check is not required for this change.")
        return 0

    print(f"Checking Black formatting in changed lines across {len(changed_files)} Python file(s).")
    for path in changed_files:
        command = ["uv", "run", "--locked", "black", "--check", "--diff"]
        if path not in added_files:
            for start, end in ranges_by_file[path]:
                command.extend(["--line-ranges", f"{start}-{end}"])
        command.append(path)
        result = subprocess.run(command, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
