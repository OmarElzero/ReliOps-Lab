"""Who took which share of a queue, and what each machine is doing now.

When a machine starts a queue it writes a claim for its share: who, which machine, since when. While it runs the claim is
refreshed (the problem being run, the time it was last seen), and when the queue ends the claim says how it ended. Claims
go through the shared store, so every machine sees them, including a share that someone took but has not finished a first
run in yet. A claim is information, not a lock: nothing stops a second machine from starting the same share, but the screen
and the start dialog say that someone else is on it.

A share is shown as one of:
  finished   every cell of the share has a result
  running    its machine reported within STALE_S
  silent     the claim says running, but nothing was heard for STALE_S (the Lab or the machine may have stopped)
  paused     paused on purpose; Continue picks it up
  stopped    the queue stopped itself (broken setup) or was cut off
  ended      the queue ended with cells still without a result
  worked     no claim, but runs of this share were recorded (a teammate whose Lab is older)
  not taken  nothing recorded
"""
from __future__ import annotations

import calendar
import re
import socket
import time
from typing import Any

import store

STALE_S = 40 * 60            # a run can take 30 minutes on a slow cluster; silence for longer than that is worth a look
BEAT_EVERY_S = 30            # at most one write this often while a problem is running

_STEP_RE = re.compile(r"\[QUEUE\] step (\d+)/(\d+) start: (.*?) \| runs")
_RUN_RE = re.compile(r"===== \[(\d+)/(\d+)\] problem=(\S+)(?: repeat=(\d+)/(\d+))?")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _epoch(iso: str | None) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(iso or "", "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def machine_name() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def for_label(label: str | None) -> list[dict]:
    """Every claim recorded for this experiment label (one per share)."""
    if not label:
        return []
    try:
        return [c for c in store.read("claims") if c.get("experiment") == label]
    except Exception:  # noqa: BLE001  the board is information; it must never break a preview or a run
        return []


def claim_for(claims: list[dict], share: int | None) -> dict | None:
    return next((c for c in claims if int(c.get("share") or 0) == int(share or 0)), None)


def share_state(sh: dict, claim: dict | None, now: float | None = None) -> str:
    now = time.time() if now is None else now
    if sh.get("total") and not sh.get("left"):
        return "finished"
    if claim:
        status = claim.get("status")
        if status == "running":
            seen = _epoch(claim.get("updated_at"))
            return "running" if seen is not None and now - seen < STALE_S else "silent"
        if status == "paused":
            return "paused"
        if status in ("halted", "interrupted"):
            return "stopped"
        if status == "finished":
            return "ended"
    return "worked" if sh.get("people") else "not taken"


class Tracker:
    """Keeps this machine's claim current while a queue runs. Every write is best effort: a failed one is ignored."""

    def __init__(self, queue: dict, run_by: str | None, share: int | None):
        self.queue, self.run_by, self.share = queue, run_by or "", int(share or 0)
        self.label = ""
        self.started_at = _now()
        self.problem = ""
        self.step_name = ""
        self._last = 0.0

    def _write(self, status: str, reason: str = "") -> None:
        try:
            store.append("claims", {
                "experiment": self.label, "share": self.share, "machines": int(self.queue.get("machines") or 1),
                "queue_id": self.queue.get("queue_id"), "queue_name": self.queue.get("name"),
                "run_by": self.run_by, "machine": machine_name(), "status": status, "reason": reason,
                "started_at": self.started_at, "now_problem": self.problem, "step_name": self.step_name})
            self._last = time.time()
        except Exception:  # noqa: BLE001
            pass

    def start(self, label: str) -> str:
        """Writes the claim. Returns a note when another machine looks busy on the same share, else an empty string."""
        self.label = label
        note = ""
        other = claim_for(for_label(label), self.share)
        if other and other.get("status") == "running":
            seen = _epoch(other.get("updated_at"))
            same = other.get("machine") == machine_name()
            if seen is not None and time.time() - seen < STALE_S and not same:
                note = (f"##### [QUEUE] note: {other.get('run_by') or 'someone'} ({other.get('machine') or '?'}) is already running "
                        f"{'share ' + str(self.share) if self.share else 'the whole queue'} and was last seen "
                        f"{int((time.time() - seen) // 60)} min ago. Two machines on one share run the same cells twice. #####\n")
        self._write("running")
        return note

    def beat(self, line: str) -> None:
        """Called with every log line. Refreshes the claim when a new problem starts, and at most every BEAT_EVERY_S otherwise."""
        m = _STEP_RE.search(line)
        if m:
            self.step_name = m.group(3)
        r = _RUN_RE.search(line)
        if r:
            self.problem = r.group(3)
            self._write("running")
            return
        if m or "recorded result for" in line:
            if time.time() - self._last >= BEAT_EVERY_S:
                self._write("running")

    def finish(self, outcome: dict) -> None:
        """How the queue ended: paused, halted, finished, or interrupted (nothing said how it ended)."""
        if not self.label:
            return
        self.problem = "" if outcome.get("status") in ("finished", "paused", "halted") else self.problem
        self._write(outcome.get("status") or "interrupted", outcome.get("reason") or "")
