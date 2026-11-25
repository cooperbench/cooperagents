"""Extract executed steps and detect structural signals from worker histories."""

from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
from collections import deque
from dataclasses import replace
from hashlib import sha256
from itertools import takewhile
from pathlib import PurePosixPath
from time import monotonic
from typing import Any, Literal

from cooperagents.agent_coordination_types import DiffProbe as ProbeRecord
from cooperagents.agent_coordination_types import Severity, Signal, SignalConfig, SignalKind, StepRecord
from cooperagents.env.base import Environment
from cooperagents.git_snapshots import GitEvidence, _overlapping_regions, _regions

_TRUNCATED = "[output truncated after "
_COMMAND_PREFIX = re.compile(
    r"""^(?:cd\s+{word}\s*&&\s*|[a-zA-Z_]\w*=(?:{word})?\s+)""".format(
        word=r"""(?:'[^']*'|"(?:[^"\\]|\\.)*"|\\.|[^\s;&|'"\\])+""",
    )
)
_TRAILING_OUTPUT = re.compile(r"\s*(?:\|\s*(?:tail|head)(?:\s[^|;&]*?)?|2>&1)\s*$")

# Specification order also breaks ties between signals of the same severity.
_KIND_ORDER: tuple[SignalKind, ...] = (
    "NO_PROGRESS_FAILING",
    "HUNK_CHURN",
    "TEST_REGRESSION",
    "PEER_OVERLAP",
    "SUBMIT_REJECTED",
    "SUBMIT_ATTEMPT",
    "PEER_MESSAGE",
    "WORKER_FINISHED",
    "PROBE_ERROR",
)
_SEVERITY_ORDER: tuple[Severity, ...] = ("hard", "warn", "info")
Region = tuple[int, int]
Hunks = dict[str, list[Region]]


def signal_priority(signal: Signal) -> tuple[int, int]:
    return _SEVERITY_ORDER.index(signal.severity), _KIND_ORDER.index(signal.kind)


def _is_test(command: str, config: SignalConfig) -> bool:
    command = command.lstrip()
    while match := _COMMAND_PREFIX.match(command):
        command = command[match.end() :]
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    try:
        words = list(takewhile(lambda word: word[0:1] not in (";", "&", "|", "<", ">"), lexer))
    except ValueError:
        return False
    prefixes = [prefix.split() for prefix in config.test_command_prefixes]
    if any(prefix and words[: len(prefix)] == prefix for prefix in prefixes):
        return True
    if not words:
        return False
    if len(words) >= 2 and words[0] in {"bash", "sh", "python", "python3", "node", "ruby", "perl"}:
        script = words[1]
    else:
        script = words[0]
    return ".cb_checks" in PurePosixPath(script).parts[:-1]


def _actions(message: dict) -> list:
    extra = message.get("extra") or {}
    if "actions" in extra:
        return extra["actions"] if isinstance(extra["actions"], list) else []
    actions = []
    for call in message.get("tool_calls") or []:
        try:
            fn = call["function"]
            args = json.loads(fn["arguments"])
            actions.append({**args, "tool_name": fn.get("name", "bash"), "tool_call_id": call.get("id")})
        except (KeyError, TypeError, ValueError):
            continue
    return actions


def parse_steps(worker: str, messages: list[dict], *, start: int, config: SignalConfig) -> list[StepRecord]:
    """Read results at message indices >= start in a stable message history.

    Step numbers count executed actions from the start of the supplied
    history. A caller using compacted context must keep its execution cursor
    separate from the model's message list.
    """
    records: deque[StepRecord] = deque(maxlen=config.history_steps)
    step = 0
    # Each assistant starts a batch. Outputs in a batch pair by ID, or by
    # position for legacy actions and outputs that both lack an ID.
    batches: list[tuple[list, list[tuple[int, dict]]]] = []
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            batches.append((_actions(message), []))
        elif batches and "raw_output" in (message.get("extra") or {}):
            batches[-1][1].append((index, message))
    for actions, outputs in batches:
        by_id = {output["tool_call_id"]: (index, output) for index, output in outputs if output.get("tool_call_id")}
        for position, action in enumerate(actions):
            if not isinstance(action, dict) or action.get("tool_name") != "bash":
                continue
            command = action.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
            call_id = action.get("tool_call_id")
            matched = (
                by_id.get(call_id)
                if call_id
                else outputs[position]
                if position < len(outputs) and not outputs[position][1].get("tool_call_id")
                else None
            )
            if matched is None:
                continue
            index, output = matched
            extra = output["extra"]
            code = extra.get("returncode")
            if type(code) is not int or extra.get("exception_info") == "action was not executed":
                continue
            step += 1
            if index >= start:
                raw = str(extra["raw_output"])
                records.append(StepRecord(worker, step, command, code, sha256(raw.encode()).hexdigest(), _is_test(command, config)))
    return list(records)


class DiffProbe(ProbeRecord):
    """Collect one read-only Git probe and retain only structural evidence."""

    @staticmethod
    def command() -> str:
        return r"""git -c core.quotepath=false diff --no-ext-diff --no-color --no-renames -U0 HEAD -- . \
  ':(exclude).cb_*' ':(exclude)patch.txt' | grep -E '^(diff --git|@@ )'
echo '--FP--'
{ git diff --no-ext-diff --no-color --binary HEAD -- . ':(exclude).cb_*' ':(exclude)patch.txt'
  git ls-files -o --exclude-standard -z -- . ':(exclude).cb_*' | xargs -0 -r git hash-object -- ; } \
  | sha256sum | cut -c1-64
echo '--NUM--'
git diff --no-ext-diff --numstat HEAD -- . ':(exclude).cb_*' ':(exclude)patch.txt' \
  | awk '{a+=$1; d+=$2} END {print a+0, d+0}'"""

    @classmethod
    def run(cls, env: Environment, *, config: SignalConfig = SignalConfig()) -> DiffProbe:
        started = monotonic()
        # The printed specification excludes patch.txt from tracked diffs.
        # Apply the same exclusion to the untracked file list.
        command = cls.command().replace("'\x3a(exclude).cb_*' | xargs", "'\x3a(exclude).cb_*' '\x3a(exclude)patch.txt' | xargs")
        try:
            result = env.execute(command, timeout=config.probe_timeout_s)  # type: ignore[arg-type]
        except (TimeoutError, subprocess.TimeoutExpired):
            return cls(None, {}, 0, 0, (monotonic() - started) * 1000, "timeout")
        except Exception:
            return cls(None, {}, 0, 0, (monotonic() - started) * 1000, "execution_failed")
        latency_ms = (monotonic() - started) * 1000
        if _TRUNCATED in result.stdout:
            return parse_probe(result.stdout, latency_ms=latency_ms)
        if result.exit_code:
            error = "timeout" if result.exit_code == 124 or result.stdout.startswith("[timed out after ") else "execution_failed"
            return cls(None, {}, 0, 0, latency_ms, error)
        return parse_probe(result.stdout, latency_ms=latency_ms)


def _diff_path(header: str) -> str:
    # With --no-renames the a/ and b/ paths have equal lengths and differ only
    # in the prefix. This also handles unquoted spaces and embedded " b/".
    paths = header.removeprefix("diff --git ")
    middle = len(paths) // 2
    if paths[middle : middle + 1] != " ":
        raise ValueError("invalid diff header")
    left, right = paths[:middle], paths[middle + 1 :]
    if left.startswith('"') and right.startswith('"'):
        left, right = ast.literal_eval(left), ast.literal_eval(right)
    if not left.startswith("a/") or not right.startswith("b/") or left[2:] != right[2:] or len(left) == 2:
        raise ValueError("invalid diff paths")
    return left[2:]


def parse_probe(stdout: str, *, latency_ms: float) -> DiffProbe:
    if _TRUNCATED in stdout:
        return DiffProbe(None, {}, 0, 0, latency_ms, "truncated")
    if stdout.startswith("[timed out after "):
        return DiffProbe(None, {}, 0, 0, latency_ms, "timeout")
    lines = stdout.removesuffix("\n").split("\n")
    try:
        if len(lines) < 4 or lines[-4] != "--FP--" or lines[-2] != "--NUM--":
            raise ValueError("missing probe sections")
        fingerprint = lines[-3]
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or not re.fullmatch(r"[0-9]+ [0-9]+", lines[-1]):
            raise ValueError("invalid fingerprint or numstat")
        added, deleted = map(int, lines[-1].split())
        headers: dict[str, list[str]] = {}
        path: str | None = None
        for line in lines[:-4]:
            if line.startswith("diff --git "):
                path = _diff_path(line)
                headers[path] = []
            elif line.startswith("@@ ") and path is not None:
                headers[path].append(line)
            else:
                raise ValueError("invalid probe diff")
        hunks = {path: _regions("\n".join(group)) if group else [] for path, group in headers.items()}
    except (SyntaxError, TypeError, ValueError):
        return DiffProbe(None, {}, 0, 0, latency_ms, "invalid_output")
    return DiffProbe(fingerprint, hunks, added, deleted, latency_ms)


def _normalized_test(command: str) -> str:
    command = " ".join(command.split())
    while stripped := _TRAILING_OUTPUT.search(command):
        command = command[: stripped.start()]
    return command


def _changed_hunks(before: Hunks, after: Hunks) -> Hunks:
    changed = {}
    for path in sorted(before.keys() | after.keys()):
        regions = set(before.get(path, ())) ^ set(after.get(path, ()))
        if regions:
            changed[path] = sorted(regions)
    return changed


def _matching_hunk(anchor: Region, regions: list[Region]) -> Region | None:
    for region in regions:
        # Text anchors represent actual base lines. Insertions can match
        # at their own anchors, including the edges of a textual hunk.
        if (region[1] or not anchor[1]) and _overlapping_regions([anchor], [region]):
            return region
    return None


class SignalTracker:
    """Track a worker's executed steps, cooldowns, and re-arm conditions."""

    def __init__(self, worker: str, config: SignalConfig, *, initial_probe: ProbeRecord | None = None) -> None:
        self.worker = worker
        self.config = config
        self._initial_probe = initial_probe
        self._history: deque[StepRecord] = deque(maxlen=max(config.history_steps, config.no_progress_window + 1))
        self._changes: deque[tuple[int, Hunks]] = deque()
        self._churn_latches: dict[tuple[str, Region], int] = {}
        self._last_fired: dict[SignalKind, int] = {}
        self._passed: dict[str, int] = {}
        self._last_test_pass: int | None = None
        self._no_progress_armed = True
        self._last_no_progress_rearm = 0
        self._last_fingerprint = initial_probe.fingerprint if initial_probe else None
        self._rejections = 0
        self._finished = False

    @property
    def records(self) -> list[StepRecord]:
        return list(self._history)[-self.config.history_steps :] if self.config.history_steps else []

    @property
    def step(self) -> int:
        return self._history[-1].step if self._history else 0

    def _signal(self, kind: SignalKind, severity: Severity, at_step: int, **payload: Any) -> Signal:
        return Signal(f"{self.worker}:{kind}:{at_step}", kind, severity, payload)

    def _ready(self, kind: SignalKind, step: int) -> bool:
        last = self._last_fired.get(kind)
        return last is None or (step > last and step - last >= self.config.cooldown_steps)

    def _emit(self, signal: Signal, step: int) -> list[Signal]:
        if not self._ready(signal.kind, step):
            return []
        self._last_fired[signal.kind] = step
        return [signal]

    def _no_progress(self, record: StepRecord) -> Signal | None:
        size = self.config.no_progress_window
        if not self._no_progress_armed or not record.returncode or len(self._history) < size:
            return None
        history = list(self._history)
        window = history[-size:]
        baseline = history[-size - 1] if len(history) > size else self._initial_probe
        if record.fingerprint is None or baseline is None or record.fingerprint != baseline.fingerprint:
            return None
        if record.step - window[0].step != size - 1 or any(r.fingerprint is None for r in window):
            return None
        if isinstance(baseline, StepRecord) and window[0].step != baseline.step + 1:
            return None
        if baseline is self._initial_probe and window[0].step != 1:
            return None
        failures = [r for r in window if r.returncode]
        tests = [r.command[:160] for r in failures if r.is_test]
        if len(failures) < self.config.no_progress_min_failures:
            return None
        return self._signal(
            "NO_PROGRESS_FAILING",
            "hard" if tests else "warn",
            record.step,
            window_steps=size,
            failures=len(failures),
            failing_test_commands=tests[:3],
            fingerprint=record.fingerprint,
        )

    def _track_hunks(self, record: StepRecord, previous: StepRecord | ProbeRecord | None) -> None:
        while self._changes and record.step - self._changes[0][0] >= self.config.churn_window:
            self._changes.popleft()
        changes = {}
        if previous is not None and previous.fingerprint is not None and record.fingerprint is not None:
            changes = _changed_hunks(previous.hunks, record.hunks)
        for key, last_change in list(self._churn_latches.items()):
            path, region = key
            if record.step - last_change > self.config.churn_window:
                del self._churn_latches[key]
            elif _overlapping_regions([region], changes.get(path, [])):
                self._churn_latches[key] = record.step
        if changes:
            self._changes.append((record.step, changes))

    def _churn(self, step: int) -> tuple[Signal, tuple[str, Region]] | None:
        if len(self._changes) < self.config.churn_min_steps or self._changes[-1][0] != step:
            return None
        for path, latest in self._changes[-1][1].items():
            changed_sets = [changes[path] for _, changes in self._changes if path in changes]
            anchors = sorted({(start, int(count > 0)) for regions in changed_sets for start, count in regions})
            for anchor in anchors:
                if not _matching_hunk(anchor, latest):
                    continue
                matches = [hunk for regions in changed_sets if (hunk := _matching_hunk(anchor, regions)) is not None]
                if len(matches) < self.config.churn_min_steps:
                    continue
                common = anchor
                if anchor[1]:
                    start = max(start for start, _ in matches)
                    end = min(start + count for start, count in matches)
                    common = start, end - start
                if any(p == path and _overlapping_regions([region], [common]) for p, region in self._churn_latches):
                    continue
                label = _overlapping_regions([common], [matches[-1]])
                return self._signal(
                    "HUNK_CHURN",
                    "warn",
                    step,
                    path=path,
                    region=label,
                    steps=len(matches),
                    last_test_pass_step=self._last_test_pass,
                ), (path, common)
        return None

    def observe(self, records: list[StepRecord], *, probe: ProbeRecord | None = None) -> list[Signal]:
        """Record one poll and return at most one wake candidate plus probe telemetry.

        A poll-level probe describes the tree after the last action in the
        poll. Earlier actions with no probe retain unknown fingerprints.
        """
        new = [record for record in records if record.step > self.step]
        if probe is not None and new:
            new[-1] = replace(
                new[-1],
                fingerprint=probe.fingerprint,
                hunks=probe.hunks,
                diff_added=probe.added,
                diff_deleted=probe.deleted,
            )
        elif probe is not None and not self.step and self._initial_probe is None:
            self._initial_probe = probe
            self._last_fingerprint = probe.fingerprint
        candidates: list[tuple[Signal, int, tuple[str, Region] | None]] = []
        for record in new:
            previous = self._history[-1] if self._history else self._initial_probe
            adjacent = record.step == self.step + 1
            self._history.append(record)
            test_pass = record.is_test and record.returncode == 0
            fingerprint_changed = (
                record.fingerprint is not None and self._last_fingerprint is not None and record.fingerprint != self._last_fingerprint
            )
            if record.fingerprint is not None:
                self._last_fingerprint = record.fingerprint
            if test_pass or fingerprint_changed:
                self._no_progress_armed = True
                self._last_no_progress_rearm = record.step
            if test_pass:
                self._last_test_pass = record.step
                self._changes.clear()
                self._churn_latches.clear()
            else:
                self._track_hunks(record, previous if adjacent else None)
            if self._ready("NO_PROGRESS_FAILING", record.step) and (signal := self._no_progress(record)):
                candidates.append((signal, record.step, None))
            if self._ready("HUNK_CHURN", record.step) and (churn := self._churn(record.step)):
                signal, churn_key = churn
                candidates.append((signal, record.step, churn_key))
            if record.is_test:
                command = _normalized_test(record.command)
                if test_pass:
                    self._passed[command] = record.step
                elif command in self._passed and self._ready("TEST_REGRESSION", record.step):
                    signal = self._signal(
                        "TEST_REGRESSION",
                        "hard",
                        record.step,
                        command=command[:160],
                        passed_at_step=self._passed[command],
                        failed_at_step=record.step,
                    )
                    candidates.append((signal, record.step, None))

        emitted = []
        if candidates:
            signal, step, key = min(candidates, key=lambda candidate: (*signal_priority(candidate[0]), -candidate[1]))
            emitted.extend(self._emit(signal, step))
            if signal.kind == "NO_PROGRESS_FAILING":
                self._no_progress_armed = self._last_no_progress_rearm > step
            if key is not None and (self._last_test_pass is None or self._last_test_pass <= step):
                path, region = key
                self._churn_latches[key] = max(
                    (
                        changed_at
                        for changed_at, changes in self._changes
                        if changed_at >= step and _overlapping_regions([region], changes.get(path, []))
                    ),
                    default=step,
                )
        if probe is not None and probe.error is not None:
            emitted.extend(self._emit(self._signal("PROBE_ERROR", "info", self.step, error=probe.error), self.step))
        return emitted

    def submit_attempt(self, error: str | None) -> list[Signal]:
        signals = [self._signal("SUBMIT_ATTEMPT", "info", self.step, step=self.step)]
        if error is not None:
            self._rejections += 1
            signals.extend(
                self._emit(
                    self._signal(
                        "SUBMIT_REJECTED",
                        "warn" if self._rejections == 1 else "hard",
                        self.step,
                        rejections=self._rejections,
                        error_head=error[:500],
                    ),
                    self.step,
                )
            )
        return signals

    def peer_message(self, sender: str, content: str) -> list[Signal]:
        return self._emit(self._signal("PEER_MESSAGE", "info", self.step, sender=sender, bytes=len(content.encode())), self.step)

    def worker_finished(self, status: str) -> list[Signal]:
        if self._finished:
            return []
        self._finished = True
        return [self._signal("WORKER_FINISHED", "info", self.step, status=status)]


def overlap_signals(evidence: GitEvidence, seen: set[tuple[str, str, str, str]]) -> list[Signal]:
    signals = []
    for pair, comparison in sorted(evidence.comparisons.items()):
        regions: list[tuple[str, str, Literal["hard", "info"]]] = [
            (path, label, "hard") for path, label in sorted(comparison.overlaps.items())
        ]
        regions.extend((path, "unknown", "info") for path in sorted(comparison.unknown))
        for path, label, severity in regions:
            key = (*pair, path, label)
            if key in seen:
                continue
            seen.add(key)
            digest = sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:16]
            signals.append(
                Signal(
                    f"{pair[0]}:{pair[1]}:PEER_OVERLAP:{digest}",
                    "PEER_OVERLAP",
                    severity,
                    {"pair": list(pair), "path": path, "regions": label, "source": "snapshot"},
                )
            )
    return signals
