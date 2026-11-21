#!/usr/bin/env python3
"""Check protected test changes against the PR base and reject disabled tests."""

import argparse
import ast
import subprocess
import sys
from pathlib import Path

DEFAULT_BASE = "origin/cameron-chen/coord-baselines"
PROTECTED = Path("tests/protected")
GUARD = Path("scripts/check_protected_tests.py")
GUARD_SUPPORT = {Path("tests/test_check_protected_tests.py"), Path("AGENTS.md"), Path("CLAUDE.md")}


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def changed_paths(root: Path, base: str) -> set[Path]:
    base_sha = git("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}", cwd=root).decode().strip()
    merge_base = git("merge-base", base_sha, "HEAD", cwd=root).decode().strip()
    paths = set()
    # Inspect each snapshot so an unstaged reversal cannot hide a staged change.
    for revision in ((merge_base, "HEAD"), ("--cached", "HEAD"), ()):
        fields = iter(git("diff", "--name-status", "-z", "-M", *revision, "--", cwd=root).split(b"\0")[:-1])
        for status in fields:
            paths.add(Path(next(fields).decode(errors="surrogateescape")))
            if status.startswith((b"R", b"C")):
                paths.add(Path(next(fields).decode(errors="surrogateescape")))
    untracked = git("ls-files", "--others", "--exclude-standard", "-z", cwd=root).split(b"\0")[:-1]
    paths.update(Path(path.decode(errors="surrogateescape")) for path in untracked)
    return paths


def protected_family(path: Path) -> str | None:
    if path == GUARD:
        return "guard"
    if not path.is_relative_to(PROTECTED) or path == PROTECTED:
        return None
    parts = path.relative_to(PROTECTED).parts
    if len(parts) > 1:
        return parts[0]
    return path.stem.removeprefix("test_").removesuffix("_coordinator_macro").removesuffix("_macro").removesuffix("_test")


def disabled_marks(path: Path) -> list[int]:
    tree = ast.parse(path.read_bytes(), filename=str(path))
    pytest_names = {"pytest"}
    mark_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            pytest_names.update(alias.asname or alias.name for alias in node.names if alias.name == "pytest")
        elif isinstance(node, ast.ImportFrom) and node.module == "pytest":
            mark_names.update(alias.asname or alias.name for alias in node.names if alias.name == "mark")

    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in {"skip", "skipif", "xfail"}:
            continue
        mark = node.value
        if (
            isinstance(mark, ast.Name)
            and mark.id in mark_names
            or (
                isinstance(mark, ast.Attribute)
                and mark.attr == "mark"
                and isinstance(mark.value, ast.Name)
                and mark.value.id in pytest_names
            )
        ):
            lines.append(node.lineno)
    return sorted(set(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument(
        "--allow-protected",
        action="append",
        default=[],
        metavar="FAMILY",
        help="Permit one protected test family and optionally 'guard' for guard changes",
    )
    args = parser.parse_args()
    allowed = set(args.allow_protected)
    if len(allowed - {"guard"}) > 1:
        parser.error("--allow-protected permits a single test family")

    violations = []
    try:
        root = Path(git("rev-parse", "--show-toplevel").decode().strip())
        for path in sorted(changed_paths(root, args.base)):
            family = protected_family(path)
            if family is not None and family not in allowed:
                violations.append(f"Protected change: {path} (family: {family})")
            elif family is None and allowed and ("guard" not in allowed or path not in GUARD_SUPPORT):
                violations.append(f"Protected test allowance requires a test-only change: {path}")
        for path in sorted((root / PROTECTED).rglob("*.py")):
            for line in disabled_marks(path):
                violations.append(f"Disabled protected test marker: {path.relative_to(root)}:{line}")
    except subprocess.CalledProcessError as exc:
        violations.append(f"Protected test guard failed: {exc.stderr.decode(errors='replace').strip()}")
    except (OSError, SyntaxError) as exc:
        violations.append(f"Protected test guard failed: {exc}")
    for violation in violations:
        print(violation, file=sys.stderr)
    return int(bool(violations))


if __name__ == "__main__":
    sys.exit(main())
