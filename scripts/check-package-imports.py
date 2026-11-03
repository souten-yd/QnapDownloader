"""Fail when a local module imported (transitively) by the entry point is missing from a package dir.

Usage: python3 -I scripts/check-package-imports.py <package-dir> <entry.py> [more entries...]
A "local module" is any top-level import whose <name>.py exists in the repository root. Only the
source text is parsed (ast); nothing is imported or executed.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def local_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return {n for n in names if (ROOT / (n + ".py")).is_file()}


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    package = Path(argv[0])
    todo = [Path(e).stem for e in argv[1:]]
    seen, missing = set(), []
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        if not (package / (name + ".py")).is_file():
            missing.append(name + ".py")
            continue
        todo.extend(local_imports(package / (name + ".py")) - seen)
    if missing:
        print("package is missing modules imported by the Hub: " + ", ".join(sorted(missing)), file=sys.stderr)
        return 1
    print("package imports complete: %d local modules" % len(seen))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
