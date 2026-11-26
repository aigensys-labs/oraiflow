"""Check documentation coverage for production Python modules and interfaces."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (ROOT / "services", ROOT / "shared", ROOT / "scripts")


def _public_definitions(module: ast.Module) -> list[ast.AST]:
    """Collect public module definitions and methods on public classes."""
    definitions: list[ast.AST] = []
    for node in module.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            definitions.append(node)
            if isinstance(node, ast.ClassDef):
                definitions.extend(
                    member
                    for member in node.body
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not member.name.startswith("_")
                )
    return definitions


def find_missing_docstrings() -> list[str]:
    """Return source locations missing required production docstrings."""
    missing: list[str] = []
    for source_root in PRODUCTION_ROOTS:
        for path in sorted(source_root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            module = ast.parse(source, filename=str(path))
            relative = path.relative_to(ROOT)
            if not ast.get_docstring(module):
                missing.append(f"{relative}: module docstring")
            for definition in _public_definitions(module):
                if not ast.get_docstring(definition):
                    missing.append(f"{relative}:{definition.lineno}: {definition.name} docstring")
    return missing


def main() -> int:
    """Run the documentation-coverage check and return a process status."""
    missing = find_missing_docstrings()
    if missing:
        print("Missing production docstrings:", file=sys.stderr)
        for item in missing:
            print(f" - {item}", file=sys.stderr)
        return 1
    print("Production module, public class and public function docstrings: complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
