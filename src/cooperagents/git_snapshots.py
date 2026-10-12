"""Advisory region evidence from the existing shared Git snapshots."""

from __future__ import annotations

import copy
import re
import shlex
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import combinations

from cooperagents.env.base import Environment


@dataclass(frozen=True)
class Snapshot:
    sha: str
    captured_at: str
    published_at: str


@dataclass(frozen=True)
class RegionComparison:
    covered: frozenset[str] = frozenset()
    overlaps: dict[str, str] = field(default_factory=dict)
    unknown: frozenset[str] = frozenset()
    error: str | None = None


@dataclass(frozen=True)
class GitEvidence:
    base_sha: str | None = None
    workers: dict[str, Snapshot] = field(default_factory=dict)
    collection_errors: dict[str, str] = field(default_factory=dict)
    comparisons: dict[tuple[str, str], RegionComparison] = field(default_factory=dict)

    def metadata(self) -> dict:
        return {
            "base_sha": self.base_sha,
            "workers": {aid: asdict(snapshot) for aid, snapshot in self.workers.items()},
            "collection_errors": dict(self.collection_errors),
            "comparison_errors": {f"{a}/{b}": c.error for (a, b), c in self.comparisons.items() if c.error},
        }


def _sha(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise ValueError("invalid object SHA")
    return value


def _raw_changes(output: str) -> dict[str, tuple[str, str, str, str, str]]:
    if output and not output.endswith("\0"):
        raise ValueError("incomplete raw diff")
    chunks = output.split("\0")[:-1]
    if len(chunks) % 2:
        raise ValueError("incomplete raw diff path")
    changes = {}
    for header, path in zip(chunks[::2], chunks[1::2], strict=True):
        fields = header.removeprefix(":").split()
        if not header.startswith(":") or len(fields) != 5 or not path:
            raise ValueError("invalid raw diff")
        old_mode, new_mode, old_sha, new_sha, status = fields
        if not all(re.fullmatch(r"[0-7]{6}", m) for m in (old_mode, new_mode)):
            raise ValueError("invalid mode")
        _sha(old_sha)
        _sha(new_sha)
        if not path.startswith(".cb_") and path != "patch.txt":
            changes[path] = (old_mode, new_mode, old_sha, new_sha, status)
    return changes


def _regions(output: str) -> list[tuple[int, int]]:
    regions = []
    for line in output.splitlines():
        if line.startswith("@@ "):
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
            if match is None:
                raise ValueError("invalid hunk")
            regions.append((int(match[1]), int(match[2] or 1)))
    if not regions:
        raise ValueError("no textual regions")
    return regions


def _overlapping_regions(left: list[tuple[int, int]], right: list[tuple[int, int]]) -> str | None:
    i = j = 0
    while i < len(left) and j < len(right):
        a, n = left[i]
        b, m = right[j]
        if n and m:
            overlap = max(a, b) < min(a + n, b + m)
        elif not n and not m:
            overlap = a == b
        elif not n:
            overlap = b - 1 <= a <= b + m - 1
        else:
            overlap = a - 1 <= b <= a + n - 1
        if overlap:

            def label(start: int, count: int) -> str:
                return f"L{start}-{start + count - 1}" if count else f"insert after L{start}"

            return f"{label(a, n)} / {label(b, m)}"
        if a + max(n - 1, 0) <= b + max(m - 1, 0):
            i += 1
        else:
            j += 1
    return None


class GitShareSync:
    """Publish normal stash/HEAD snapshots; optional observation never alters worker edits."""

    def __init__(self, envs: dict[str, Environment], *, observe: bool = False, git_dir: str = "/cbshared/repo.git") -> None:
        self._envs = envs
        self._observe = observe
        self._git_dir = git_dir
        self._stop = threading.Event()
        self._locks = {aid: threading.Lock() for aid in envs}
        self._evidence_lock = threading.Lock()
        self._base_sha: str | None = None
        self._published: dict[str, Snapshot] = {}
        self._errors: dict[str, str] = {}
        self._cache: dict[tuple[str, str], tuple[tuple[str, str, str], RegionComparison]] = {}
        self._evidence = GitEvidence()

    def initialize(self, heads: dict[str, str], published: set[str]) -> None:
        self._base_sha = None
        self._published.clear()
        self._errors.clear()
        self._cache.clear()
        try:
            values = {_sha(heads[aid]) for aid in self._envs}
            if len(values) == 1:
                self._base_sha = next(iter(values))
        except (KeyError, ValueError):
            pass
        now = datetime.now(UTC).isoformat()
        for aid in self._envs:
            if aid in published:
                try:
                    self._published[aid] = Snapshot(_sha(heads[aid]), now, now)
                except (KeyError, ValueError):
                    self._errors[aid] = "initial snapshot unavailable"
            else:
                self._errors[aid] = "initial publication unavailable"
        pairs = {
            (a, b): RegionComparison()
            for a, b in combinations(sorted(self._envs), 2)
            if self._base_sha and a in self._published and b in self._published
        }
        with self._evidence_lock:
            self._evidence = GitEvidence(self._base_sha, dict(self._published), dict(self._errors), pairs)

    def evidence(self) -> GitEvidence:
        with self._evidence_lock:
            return copy.deepcopy(self._evidence)

    def stop(self) -> None:
        self._stop.set()

    def finish(self, thread: threading.Thread) -> None:
        self.stop()
        thread.join(timeout=180)
        if thread.is_alive():
            raise RuntimeError("Git sync still running; episode is incomplete")

    def _query(self, env: Environment, args: list[str], deadline: float) -> str:
        remaining = deadline - time.monotonic()
        if self._stop.is_set() or remaining < 1:
            raise TimeoutError("snapshot comparison budget unavailable")
        command = shlex.join(["git", "--literal-pathspecs", f"--git-dir={self._git_dir}", *args])
        result = env.execute(command, timeout=int(remaining))
        if result.exit_code != 0 or "[output truncated after " in result.stdout:
            raise ValueError("snapshot query unavailable")
        return result.stdout

    def _compare(self, env: Environment, left: Snapshot, right: Snapshot) -> RegionComparison:
        if self._base_sha is None:
            return RegionComparison(error="common baseline unavailable")
        deadline = time.monotonic() + 5
        common: frozenset[str] = frozenset()
        try:
            options = ["--no-renames", "--no-ext-diff", "--no-textconv"]
            changes = [
                _raw_changes(self._query(env, ["diff", "--raw", "-z", "--abbrev=64", *options, self._base_sha, s.sha], deadline))
                for s in (left, right)
            ]
            common = frozenset(changes[0].keys() & changes[1].keys())
            overlaps, unknown = {}, set()
            for path in sorted(common):
                if self._stop.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("snapshot comparison budget unavailable")
                a, b = changes[0][path], changes[1][path]
                if (a[1], a[3]) == (b[1], b[3]):
                    continue
                if any(c[4] != "M" or c[0] != c[1] or c[1] not in {"100644", "100755"} for c in (a, b)):
                    unknown.add(path)
                    continue
                try:
                    regions = [
                        _regions(
                            self._query(env, ["diff", "--no-color", "--unified=0", *options, self._base_sha, s.sha, "--", path], deadline)
                        )
                        for s in (left, right)
                    ]
                    overlap = _overlapping_regions(regions[0], regions[1])
                    if overlap:
                        overlaps[path] = overlap
                except ValueError:
                    unknown.add(path)
            return RegionComparison(common, overlaps, frozenset(unknown))
        except Exception:
            return RegionComparison(common, unknown=common, error="snapshot comparison unavailable")

    def _refresh_comparisons(self) -> None:
        pairs = {}
        env = next(iter(self._envs.values()), None)
        for a, b in combinations(sorted(self._envs), 2):
            pair = (a, b)
            if a in self._errors or b in self._errors or a not in self._published or b not in self._published:
                pairs[pair] = RegionComparison(error="latest publication unavailable")
                continue
            if self._base_sha is None or env is None:
                pairs[pair] = RegionComparison(error="common baseline unavailable")
                continue
            left, right = self._published[a], self._published[b]
            key = (self._base_sha, left.sha, right.sha)
            cached = self._cache.get(pair)
            if cached and cached[0] == key:
                comparison = cached[1]
            else:
                with self._locks[next(iter(self._envs))]:
                    comparison = self._compare(env, left, right)
                if comparison.error is None:
                    self._cache[pair] = (key, comparison)
            pairs[pair] = comparison
        with self._evidence_lock:
            self._evidence = GitEvidence(self._base_sha, dict(self._published), dict(self._errors), pairs)

    def publish_cycle(self) -> None:
        for aid, env in list(self._envs.items()):
            if self._stop.is_set():
                break
            try:
                with self._locks[aid]:
                    if self._stop.is_set():
                        break
                    if not self._observe:
                        env.execute(
                            f"C=$(git stash create 2>/dev/null); git push -q -f shared ${{C:-HEAD}}:refs/heads/{aid} 2>/dev/null || true",
                            timeout=60,
                        )
                        continue
                    captured_at = datetime.now(UTC).isoformat()
                    ref = shlex.quote(f"refs/heads/{aid}")
                    result = env.execute(
                        "C=$(git stash create 2>/dev/null) || exit 1; "
                        'if [ -z "$C" ]; then C=$(git rev-parse HEAD) || exit 1; fi; '
                        f'git push -q -f shared "$C":{ref} 2>/dev/null || exit 2; '
                        'printf "%s\\n" "$C"',
                        timeout=60,
                    )
                    if result.exit_code != 0:
                        self._errors[aid] = "snapshot capture failed" if result.exit_code == 1 else "snapshot publication failed"
                        continue
                    self._published[aid] = Snapshot(_sha(result.stdout), captured_at, datetime.now(UTC).isoformat())
                    self._errors.pop(aid, None)
            except Exception:
                self._errors[aid] = "snapshot publication unavailable"
        if self._observe and not self._stop.is_set():
            self._refresh_comparisons()

    def run(self) -> None:
        while not self._stop.wait(45):
            self.publish_cycle()
