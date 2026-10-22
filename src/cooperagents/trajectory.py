"""Append-only agent I/O journal and container-free, event-boundary replay."""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path


def _json_default(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    raise TypeError(f"Unsupported journal value: {type(value).__name__}")


class Trajectory:
    """One journal per pair; a lock establishes ordering across its agents."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = gzip.open(path, "xt", encoding="utf-8", compresslevel=3) if path.suffix == ".gz" else path.open("x", encoding="utf-8")
        self._lock = threading.Lock()
        self._seq = 0
        self.error: Exception | None = None
        self._secrets = [
            v for k, v in os.environ.items() if any(s in k.upper() for s in ("API_KEY", "TOKEN", "PASSWORD", "SECRET")) and len(v) >= 8
        ]

    def emit(self, actor: str, event: str, **data: object) -> int:
        with self._lock:
            try:
                self._seq += 1
                record = dict(version=1, seq=self._seq, time=datetime.now(UTC).isoformat(), actor=actor, event=event, data=data)

                # Remove transport credentials, retaining model inputs/outputs and sampling.
                def clean(value):
                    if isinstance(value, dict):
                        return {
                            k: "[REDACTED]"
                            if k.lower() in {"api_key", "authorization", "x-api-key", "cookie", "password", "secret"}
                            else clean(v)
                            for k, v in value.items()
                        }
                    if isinstance(value, list):
                        return [clean(v) for v in value]
                    if isinstance(value, str):
                        for secret in self._secrets:
                            value = value.replace(secret, "[REDACTED]")
                    return value

                record = clean(json.loads(json.dumps(record, default=_json_default)))
                self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._file.flush()
                return self._seq
            except Exception as exc:
                self.error = exc
                raise

    def close(self) -> None:
        self._file.close()
        if self.error is not None:
            raise RuntimeError("Trajectory recording failed") from self.error


def record_call(trace, function, **request):
    """Capture SDK-level requests and raw responses, including failed attempts."""
    if trace is None:
        return function(**request)
    call_id = trace("request", request=request)
    try:
        result = function(**request)
    except BaseException as exc:
        trace("error", call_id=call_id, error_type=type(exc).__name__, error=str(exc))
        raise
    trace("response", call_id=call_id, response=result)
    return result


def open_events(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open(encoding="utf-8")


def replay(path: Path, *, at_seq: int | None = None, at_time: str | None = None) -> dict:
    """Restore observed conversation state at a recorded event boundary, without inference."""
    contexts, actors, pending = {}, {}, {}
    count = 0
    cutoff = datetime.fromisoformat(at_time) if at_time else None
    with open_events(path) as handle:
        for line in handle:
            row = json.loads(line)
            if row["version"] != 1 or row["seq"] != count + 1:
                raise ValueError("Unsupported version or non-contiguous journal sequence")
            if at_seq is not None and row["seq"] > at_seq:
                break
            if cutoff is not None and datetime.fromisoformat(row["time"]) > cutoff:
                break
            count = row["seq"]
            actor, event, data = row["actor"], row["event"], row["data"]
            state = actors.setdefault(actor, {})
            state["last_event"] = row
            if event == "context":
                contexts[actor] = data["messages"]
            elif event == "messages":
                contexts.setdefault(actor, []).extend(data["messages"])
            elif event == "request":
                pending[count] = row
                state["last_request"] = row
            elif event in ("response", "error"):
                if data["call_id"] not in pending:
                    raise ValueError("Response has no matching request")
                request = pending.pop(data["call_id"])
                if request["actor"] != actor:
                    raise ValueError("Response actor differs from request actor")
                state["last_completed_call"] = {"request": request, "result": row}
    return dict(seq=count, contexts=contexts, actors=actors, pending_calls=pending)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--at-seq", type=int)
    parser.add_argument("--at-time", help="ISO timestamp with timezone")
    parser.add_argument("--actor", help="agent1, agent2, integrator1, integrator2, or coordinator")
    args = parser.parse_args()
    state = replay(args.path, at_seq=args.at_seq, at_time=args.at_time)
    if args.actor:
        for key in ("contexts", "actors"):
            state[key] = {k: v for k, v in state[key].items() if k == args.actor}
        state["pending_calls"] = {k: v for k, v in state["pending_calls"].items() if v["actor"] == args.actor}
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
