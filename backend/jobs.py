"""Background jobs. A run or a queue of runs is driven by a server thread, not by the browser
connection: closing the tab, reloading the page or putting the laptop's screen to sleep does not
stop it, and reopening the Lab re-attaches to the live output. Only one job runs at a time, because
SREGym drives a single cluster."""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import runner

_MAX_LINES = 60000   # kept in memory for re-attaching; the full output is always in the log file
_TRIM = 10000

_STEP_RE = re.compile(r"\[QUEUE\] step (\d+)/(\d+) start: (.*?) \| runs (\d+) to do")
_RUN_RE = re.compile(r"===== \[(\d+)/(\d+)\] problem=(\S+)(?: repeat=(\d+)/(\d+))?")
_DONE_RE = re.compile(r"recorded result for (\S+): ")


class Job:
    def __init__(self, kind: str, name: str, meta: dict | None = None):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.name = name
        self.meta = dict(meta or {})
        self.state = "running"          # running | done | stopped | error
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.log_file: str | None = None
        self.progress: dict = {}
        self._lines: list[str] = []
        self._base = 0                  # absolute index of _lines[0]
        self._cond = threading.Condition()

    # -- writing --------------------------------------------------------------------------
    def add(self, chunk: str) -> None:
        with self._cond:
            self._lines.append(chunk)
            if len(self._lines) > _MAX_LINES:
                del self._lines[:_TRIM]
                self._base += _TRIM
            self._track(chunk)
            self._cond.notify_all()

    def _track(self, chunk: str) -> None:
        p = self.progress
        for m in _STEP_RE.finditer(chunk):
            p.update(step=int(m.group(1)), steps=int(m.group(2)), step_name=m.group(3), step_runs=int(m.group(4)),
                     step_recorded=0, problem=None, repeat=None)
        for m in _RUN_RE.finditer(chunk):
            p.update(run_k=int(m.group(1)), run_total=int(m.group(2)), problem=m.group(3),
                     repeat=int(m.group(4)) if m.group(4) else 1,
                     repeat_n=int(m.group(5)) if m.group(5) else 1, run_started_at=time.time())
        for _ in _DONE_RE.finditer(chunk):
            p["recorded"] = p.get("recorded", 0) + 1
            p["step_recorded"] = p.get("step_recorded", 0) + 1
            p["run_started_at"] = None

    def finish(self, state: str) -> None:
        with self._cond:
            self.state = state
            self.finished_at = time.time()
            self.progress["run_started_at"] = None
            self._cond.notify_all()

    # -- reading --------------------------------------------------------------------------
    @property
    def total(self) -> int:
        return self._base + len(self._lines)

    @property
    def running(self) -> bool:
        return self.state == "running"

    def read(self, offset: int, wait: float = 15.0) -> tuple[list[str], int, bool]:
        """Lines from absolute `offset` (negative = that many from the end). Blocks up to `wait`
        seconds when there is nothing new. Returns (lines, next_offset, finished_and_caught_up)."""
        with self._cond:
            if offset < 0:
                offset = max(self._base, self.total + offset)
            offset = max(offset, self._base)
            deadline = time.time() + wait
            while offset >= self.total and self.running:
                left = deadline - time.time()
                if left <= 0:
                    break
                self._cond.wait(left)
            lines = self._lines[offset - self._base:]
            nxt = self.total
            return lines, nxt, (not self.running and nxt <= self.total and offset + len(lines) >= self.total)

    def describe(self) -> dict:
        p = dict(self.progress)
        started = p.get("run_started_at")
        return {
            "id": self.id, "kind": self.kind, "name": self.name, "state": self.state, "meta": self.meta,
            "started_at": self.started_at, "finished_at": self.finished_at, "n_lines": self.total,
            "log_file": self.log_file, "progress": p,
            "run_elapsed_s": int(time.time() - started) if started else None,
        }


_current: Job | None = None
_lock = threading.Lock()


def current() -> Job | None:
    return _current


def busy() -> bool:
    return _current is not None and _current.running


def start(kind: str, name: str, make_lines: Callable[[], Iterator[str]], meta: dict | None = None) -> Job | None:
    """Starts `make_lines()` on a daemon thread and returns the Job, or None if another job is running."""
    global _current
    with _lock:
        if busy():
            return None
        job = Job(kind, name, meta)
        _current = job
    log_path = runner.new_log_file(kind)
    job.log_file = log_path.name

    def work() -> None:
        state = "done"
        try:
            with log_path.open("w", encoding="utf-8") as logf:
                head = f"[saving full output to {log_path}]\n"
                logf.write(head)
                job.add(head)
                for line in make_lines():
                    logf.write(line)
                    logf.flush()
                    job.add(line)
        except Exception as e:  # noqa: BLE001 - a crashed job must still be reported, not vanish
            job.add(f"\n[job failed: {type(e).__name__}: {e}]\n")
            state = "error"
        finally:
            if state == "done" and runner._stop_event.is_set():
                state = "stopped"
            job.finish(state)

    threading.Thread(target=work, name=f"job-{job.id}", daemon=True).start()
    return job
