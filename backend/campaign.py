"""Frozen campaigns: a manifest pins what a measured run is allowed to be (agent code commit, judge,
model, action space, label, problem set), the grid shows which (problem, repeat) cells exist, and a
slice splits the missing cells across laptops. A run that does not match its manifest is refused, so a
number labelled `frozen` always means the same thing."""

from __future__ import annotations

import hashlib
import subprocess
import time
import uuid

import runner
import stats

AGENT_PATHS = ("agent", "scripts")  # uncommitted changes here would change agent behaviour unseen


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=runner.reliops_dir(), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def git_commit() -> str | None:
    return _git("rev-parse", "HEAD")


def agent_dirty() -> bool | None:
    """True when agent/ or scripts/ have uncommitted changes; None when git is unavailable."""
    out = _git("status", "--porcelain", "--", *AGENT_PATHS)
    return None if out is None else bool(out)


def problems_hash(problems: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(problems))).encode()).hexdigest()[:16]


def create_manifest(*, name: str, agent_id: str, model_id: str, action_space: str | None, judge_model_id: str,
                    problems: list[str], repeat_n: int, config_label: str, created_by: str | None,
                    notes: str = "") -> dict:
    problems = sorted(set(p.strip() for p in problems if p.strip()))
    if not problems:
        raise ValueError("pick at least one problem")
    if not config_label.strip():
        raise ValueError("a frozen campaign needs a config label")
    if agent_dirty():
        raise ValueError("agent/ or scripts/ have uncommitted changes in the ReliOps clone: commit them first, "
                         "otherwise the frozen run is not reproducible")
    return {
        "manifest_id": uuid.uuid4().hex[:10],
        "name": name.strip() or config_label.strip(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "created_by": created_by or "unknown",
        "agent_id": agent_id, "model_id": model_id, "action_space": action_space or "",
        "judge_model_id": judge_model_id, "config_label": config_label.strip(),
        "problems": problems, "problems_hash": problems_hash(problems),
        "repeat_n": max(1, int(repeat_n)),
        "git_commit": git_commit(),
        "notes": notes,
    }


def verify_run(manifest: dict, *, agent_id: str, model_id: str, action_space: str | None, judge_model_id: str,
               config_label: str, problems: list[str]) -> list[str]:
    """Reasons this run may not count towards the manifest; empty list means it matches."""
    bad = []
    for field, got in (("agent_id", agent_id), ("model_id", model_id), ("judge_model_id", judge_model_id),
                       ("config_label", (config_label or "").strip())):
        if (manifest.get(field) or "") != (got or ""):
            bad.append(f"{field}: manifest has {manifest.get(field)!r}, this run has {got!r}")
    if (manifest.get("action_space") or "") != (action_space or ""):
        bad.append(f"action_space: manifest has {manifest.get('action_space')!r}, this run has {action_space!r}")
    extra = sorted(set(problems) - set(manifest.get("problems", [])))
    if extra:
        bad.append("problems not in the manifest: " + ", ".join(extra))
    commit = git_commit()
    if manifest.get("git_commit") and commit != manifest["git_commit"]:
        bad.append(f"ReliOps clone is at {str(commit)[:8]}, manifest was frozen at {manifest['git_commit'][:8]}")
    if agent_dirty():
        bad.append("agent/ or scripts/ have uncommitted changes")
    return bad


def all_cells(manifest: dict) -> list[tuple[str, int]]:
    return [(p, r) for p in manifest["problems"] for r in range(1, manifest["repeat_n"] + 1)]


def grid(manifest: dict, runs: list[dict], metric: str = "e2e_pass") -> dict:
    """Status of every (problem, repeat) cell: pass / fail / incomplete (ran but no known outcome, will
    be re-run) / missing. Only runs tagged with this manifest count, latest per cell."""
    latest: dict[tuple[str, int], dict] = {}
    for r in runs:
        if r.get("manifest_id") != manifest["manifest_id"]:
            continue
        cell = (r.get("problem_id") or "?", int(r.get("repeat_index") or 1))
        if cell not in latest or (r.get("timestamp_utc") or "") >= (latest[cell].get("timestamp_utc") or ""):
            latest[cell] = r
    cells, counts = [], {"pass": 0, "fail": 0, "incomplete": 0, "missing": 0}
    for problem, rep in all_cells(manifest):
        r = latest.get((problem, rep))
        if r is None:
            status = "missing"
        else:
            v = r.get(metric)
            status = "incomplete" if not isinstance(v, bool) else ("pass" if v else "fail")
        counts[status] += 1
        cells.append({"problem": problem, "repeat": rep, "status": status, "run_id": r.get("run_id") if r else None})
    return {"manifest": manifest, "metric": metric, "cells": cells, "counts": counts,
            "todo": [(c["problem"], c["repeat"]) for c in cells if c["status"] in ("missing", "incomplete")]}


def slice_cells(cells: list[tuple[str, int]], k: int, n: int) -> list[tuple[str, int]]:
    """Machine k of n takes every n-th cell. Deterministic, so five laptops given the same manifest and
    their own k never overlap and never need to talk to each other."""
    if n < 1 or not 1 <= k <= n:
        raise ValueError("slice must be k of n with 1 <= k <= n")
    return [c for i, c in enumerate(sorted(cells)) if i % n == k - 1]


def manifest_runs_summary(manifest: dict, runs: list[dict], metric: str = "e2e_pass") -> dict | None:
    mine = [r for r in runs if r.get("manifest_id") == manifest["manifest_id"]]
    groups = stats.summarize(mine, metric)
    return groups[0] if groups else None
