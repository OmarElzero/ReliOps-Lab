"""What the queue was doing the last time it ran, kept in one small file so Continue can pick up exactly there.

The finished runs are already kept (each run is recorded as it ends, and a queue started again under the same
experiment name runs only the cells that have no result yet), so Continue needs no list of cells. It only needs to
remember which queue, which experiment name and who started it, and whether the queue was paused on purpose, stopped
itself because the setup looked broken, finished, or was cut off (the Lab or the machine went down). The file lives
beside the other results on this machine; it is not shared, because only the machine that ran the queue can continue it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import runner

FILE = "gui_queue_state.json"
RESUMABLE = ("paused", "halted", "interrupted")


def _path() -> Path:
    return runner.reliops_dir() / "results" / FILE


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def read() -> dict[str, Any] | None:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("queue_id") else None


def begin(queue: dict, experiment: str, run_by: str | None) -> None:
    """The queue starts (or continues). Until `end` is called the state says 'running'."""
    _write({"status": "running", "queue_id": queue.get("queue_id"), "queue_name": queue.get("name"),
            "experiment": experiment, "run_by": run_by or "", "started_at": _now(), "updated_at": _now(),
            "step": None, "step_name": "", "reason": "", "left": None})


def end(status: str, step: int | None = None, step_name: str = "", reason: str = "", left: int | None = None) -> None:
    """How this start ended: paused | halted | finished | interrupted (nothing said how it ended)."""
    data = read()
    if data is None:
        return
    data.update({"status": status, "step": step, "step_name": step_name, "reason": reason, "left": left, "updated_at": _now()})
    _write(data)


def clear() -> None:
    try:
        _path().unlink()
    except OSError:
        pass


def describe(job_busy: bool) -> dict[str, Any] | None:
    """The state for the screen. A queue that says 'running' while no job is running was cut off: it is 'interrupted'."""
    data = read()
    if data is None:
        return None
    status = data.get("status")
    if status == "running" and not job_busy:
        status = "interrupted"
    return {**data, "status": status, "can_continue": status in RESUMABLE and not job_busy}
