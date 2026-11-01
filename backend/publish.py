"""Shared, read-only results page.

The Lab runs on one machine and answers only on localhost. This module lets a team, supervisors or sponsors follow an
experiment without opening the Lab: it turns the recorded runs into a small *sanitized* snapshot and the queue's
progress into a small *live* file, and the static page in `site/` draws both. Nothing in the page can start, stop or
change anything.

What is published (and what is not)
  - Per run: problem, category, agent, config label, agent switches, experiment, outcome of each stage, judge scores,
    calls, tokens, seconds, failure tag, why a run is incomplete, who ran it (can be switched off).
  - Never: API keys, the `.env`, diagnosis text, judge reasoning, logs, stories, file paths, e-mail addresses.
  - Runs whose purpose is `final` (the frozen set) are left out unless `RELIOPS_PUBLISH_FINAL=on`, so a shared page
    cannot be used to read frozen-set failures before the freeze.

Where it goes
  - Always: `/share/` on the local Lab (preview, computed on request).
  - With `RELIOPS_PUBLISH=on`: files are written to `RELIOPS_PUBLISH_DIR` (default `results/site/`) whenever a run is
    recorded or the queue changes state, and, when `RELIOPS_PUBLISH_GIT` names a remote, pushed to its
    `RELIOPS_PUBLISH_BRANCH` (default `results-data`) at most every `RELIOPS_PUBLISH_EVERY_S` seconds (default 60), so the
    site host's build limits are respected. The push replaces the branch's single commit; it holds only the files above.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

SITE_SRC = Path(__file__).resolve().parent.parent / "site"

# The only run fields that leave the machine. Anything not listed here is dropped.
RUN_FIELDS = (
    "run_id", "timestamp_utc", "run_by", "problem_id", "category", "app", "agent_id", "model_id", "judge_model_id",
    "action_space", "config_label", "agent_switches", "experiment", "purpose", "run_class", "repeat_index", "repeat_n",
    "diagnosis_pass", "mitigation_pass", "e2e_pass", "run_status", "incomplete_reason",
    "diagnosis_d1", "diagnosis_d2", "diagnosis_d3", "diagnosis_composite",
    "total_tokens", "num_llm_calls", "num_tool_calls", "ttd_s", "ttm_s", "failure_mode_tag",
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PATH = re.compile(r"(?:[A-Za-z]:\\|/home/|/Users/|/root/)[^\s'\"]*")


def _clean_text(value):
    if not isinstance(value, str):
        return value
    return _PATH.sub("<path>", _EMAIL.sub("<email>", value))[:400]


def env_on(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "on", "true", "yes")


def sanitize_runs(runs: list[dict], include_final: bool = False, names: bool = True) -> list[dict]:
    out = []
    for r in runs:
        if r.get("purpose") == "final" and not include_final:
            continue
        row = {k: _clean_text(r.get(k)) for k in RUN_FIELDS}
        if not names:
            row["run_by"] = ""
        out.append(row)
    return out


def build_snapshot(runs: list[dict], source: str, include_final: bool = None, names: bool = None) -> dict:
    include_final = env_on("RELIOPS_PUBLISH_FINAL") if include_final is None else include_final
    names = env_on("RELIOPS_PUBLISH_NAMES", True) if names is None else names
    kept = sanitize_runs(runs, include_final, names)
    return {
        "schema": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": source,
        "n_runs": len(kept),
        "hidden_final_runs": sum(1 for r in runs if r.get("purpose") == "final") if not include_final else 0,
        "runs": kept,
    }


def build_live(job: dict | None, qstate: dict | None, left: int | None = None, now: float | None = None) -> dict:
    """What the queue is doing now, from `jobs.Job.describe()` and `queuestate.describe()`. Times are epoch seconds so
    the page can tick the elapsed time itself between updates."""
    now = time.time() if now is None else now
    p = (job or {}).get("progress") or {}
    running = bool(job) and job.get("state") == "running"
    status = "running" if running else ((qstate or {}).get("status") or "idle")
    if status == "running" and not running:
        status = "interrupted"
    recorded = p.get("recorded", 0)
    eta = None
    if running and recorded and left:
        eta = int((now - job["started_at"]) / recorded * left)
    return {
        "schema": 1,
        "generated_at": now,
        "status": status,                       # idle | running | paused | halted | finished | interrupted
        "reason": ((qstate or {}).get("reason") or "")[:300] if not running else "",
        "queue": (qstate or {}).get("queue_name") or (job or {}).get("name") or "",
        "experiment": (qstate or {}).get("experiment") or "",
        "step": p.get("step"), "steps": p.get("steps"), "step_name": p.get("step_name") or "",
        "problem": p.get("problem") if running else None,
        "repeat": p.get("repeat"), "repeat_n": p.get("repeat_n"),
        "run_k": p.get("run_k"), "run_total": p.get("run_total"),
        "run_started_at": p.get("run_started_at") if running else None,
        "recorded_this_start": recorded,
        "runs_left": left,
        "eta_s": eta,
        "started_at": (job or {}).get("started_at"),
    }


def write_files(out_dir: Path, snapshot: dict, live: dict) -> None:
    """Site files plus the two data files. Written to a temporary name first so a reader never sees half a file."""
    out_dir = Path(out_dir)
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    if SITE_SRC.is_dir():
        for f in SITE_SRC.iterdir():
            if f.is_file():
                shutil.copy2(f, out_dir / f.name)
    for name, data in (("snapshot.json", snapshot), ("live.json", live)):
        tmp = out_dir / "data" / (name + ".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(out_dir / "data" / name)


def _git(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)


def push(out_dir: Path, remote: str, branch: str) -> tuple[bool, str]:
    """Replaces the single commit on `branch` with the current contents of `out_dir`. `out_dir` is its own git
    repository, separate from the Lab's, and holds only the published files."""
    out_dir = Path(out_dir)
    try:
        if not (out_dir / ".git").exists():
            _git(["init", "-q", "-b", branch], out_dir)
        if _git(["remote", "get-url", "origin"], out_dir).returncode != 0:
            _git(["remote", "add", "origin", remote], out_dir)
        else:
            _git(["remote", "set-url", "origin", remote], out_dir)
        _git(["add", "-A"], out_dir)
        who = ["-c", "user.name=ReliOps Lab", "-c", "user.email=lab@users.noreply.github.com"]
        if _git(["rev-parse", "HEAD"], out_dir).returncode != 0:
            c = _git([*who, "commit", "-q", "-m", "Results page data"], out_dir)
        else:
            c = _git([*who, "commit", "-q", "--amend", "-m", "Results page data"], out_dir)
        if c.returncode != 0 and "nothing to commit" not in (c.stdout + c.stderr):
            return False, (c.stderr or c.stdout).strip()[:300]
        r = _git(["push", "-q", "--force", "origin", f"HEAD:{branch}"], out_dir, timeout=180)
        return r.returncode == 0, (r.stderr or "").strip()[:300]
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:300]


class Publisher:
    """Background thread. `get_runs()` returns (source, runs); `get_live()` returns the dict for `build_live`.
    Writes when something changed and pushes at most every `every_s` seconds (and once more after the last change)."""

    def __init__(self, get_runs: Callable[[], tuple[str, list[dict]]], get_live: Callable[[], dict],
                 out_dir: Path, remote: str = "", branch: str = "gh-pages", every_s: float = 300, poll_s: float = 15):
        self.get_runs, self.get_live = get_runs, get_live
        self.out_dir, self.remote, self.branch = Path(out_dir), remote, branch
        self.every_s, self.poll_s = every_s, poll_s
        self.last_push = 0.0
        self.last_error = ""
        self.last_pushed_at: float | None = None
        self._sig = None
        self._dirty = False
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="publisher", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self, now: float | None = None) -> bool:
        """One pass; returns True when files were written. Separate from the loop so it can be tested."""
        now = time.time() if now is None else now
        live = self.get_live()
        source, runs = self.get_runs()
        # signature = what the page would show differently, not the clock
        sig = (len(runs), max((str(r.get("timestamp_utc") or "") for r in runs), default=""),
               live["status"], live.get("step"), live.get("problem"), live.get("repeat"), live.get("runs_left"))
        wrote = False
        if sig != self._sig:
            write_files(self.out_dir, build_snapshot(runs, source), live)
            self._sig, self._dirty, wrote = sig, True, True
        if self.remote and self._dirty and now - self.last_push >= self.every_s:
            ok, err = push(self.out_dir, self.remote, self.branch)
            self.last_push = now
            self.last_error = "" if ok else err
            if ok:
                self._dirty = False
                self.last_pushed_at = now
        return wrote

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.tick()
            except Exception as e:  # never let the page take the Lab down
                self.last_error = f"{type(e).__name__}: {e}"[:300]

    def describe(self) -> dict:
        return {"out_dir": str(self.out_dir), "remote": bool(self.remote), "branch": self.branch,
                "every_s": self.every_s, "last_pushed_at": self.last_pushed_at, "last_error": self.last_error}
