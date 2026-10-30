"""Run queues: an ordered list of steps, each one a configuration (agent, model, judge, label, class)
applied to a set of problems with a repeat count. A step is run cell by cell, a cell being one
(problem, repeat). Running a queue again only fills the cells that have no known outcome, so a queue
that was stopped, crashed or interrupted resumes where it left off."""

from __future__ import annotations

import uuid

import campaign

RUN_CLASSES = ("exploration", "frozen")


def clean_step(raw: dict) -> dict:
    """Validated, normalised copy of a step. Raises ValueError with a message fit for the UI."""
    name = (raw.get("name") or "").strip()
    agent_id = (raw.get("agent_id") or "").strip()
    model_id = (raw.get("model_id") or "").strip()
    judge = (raw.get("judge_model_id") or "").strip()
    problems = []
    for p in raw.get("problems") or []:
        p = str(p).strip()
        if p and p not in problems:
            problems.append(p)
    if not agent_id:
        raise ValueError("pick an agent")
    if not model_id:
        raise ValueError("enter the agent model")
    if not judge:
        raise ValueError("enter the judge model")
    if not problems:
        raise ValueError("pick at least one problem")
    run_class = raw.get("run_class") or "exploration"
    if run_class not in RUN_CLASSES:
        raise ValueError("run class must be exploration or frozen")
    manifest_id = (raw.get("manifest_id") or "").strip() or None
    if run_class == "frozen" and not manifest_id:
        raise ValueError("a frozen step needs a frozen campaign (manifest): freeze one in the Campaigns tab")
    try:
        repeat_n = max(1, int(raw.get("repeat_n") or 1))
    except (TypeError, ValueError):
        raise ValueError("repeat count must be a whole number") from None
    return {
        "step_id": (raw.get("step_id") or uuid.uuid4().hex[:8]),
        "name": name or f"{raw.get('agent_label') or agent_id} · {len(problems)} problems × {repeat_n}",
        "auto_name": bool(raw.get("auto_name", False)),
        "agent_id": agent_id, "agent_label": raw.get("agent_label") or agent_id,
        "action_space": (raw.get("action_space") or None),
        "model_id": model_id, "judge_model_id": judge,
        "config_label": (raw.get("config_label") or "").strip(),
        "run_class": run_class, "manifest_id": manifest_id,
        "problems": problems, "set_label": raw.get("set_label") or "",
        "repeat_n": repeat_n,
        "only_missing": bool(raw.get("only_missing", True)),
        "force_build": bool(raw.get("force_build", False)),
        "record_walkthrough": bool(raw.get("record_walkthrough", True)),
        "enabled": bool(raw.get("enabled", True)),
    }


def clean_queue(raw: dict) -> dict:
    name = (raw.get("name") or "").strip()
    if not name:
        raise ValueError("give the queue a name")
    steps = []
    for i, s in enumerate(raw.get("steps") or [], start=1):
        try:
            steps.append(clean_step(s))
        except ValueError as e:
            raise ValueError(f"step {i}: {e}") from None
    return {"queue_id": raw.get("queue_id") or uuid.uuid4().hex[:10], "name": name, "steps": steps,
            "created_by": raw.get("created_by") or "unknown"}


def _matches(step: dict, run: dict) -> bool:
    return ((run.get("agent_id") or "") == step["agent_id"]
            and (run.get("model_id") or "") == step["model_id"]
            and (run.get("action_space") or "") == (step["action_space"] or "")
            and (run.get("judge_model_id") or "") == step["judge_model_id"]
            and (run.get("config_label") or "").strip() == step["config_label"]
            and (run.get("run_class") or "exploration") == step["run_class"]
            and (not step["manifest_id"] or run.get("manifest_id") == step["manifest_id"]))


def cells(step: dict, runs: list[dict], manifest: dict | None = None) -> list[dict]:
    """[{problem, repeat, status}] for every cell of the step: pass / fail / incomplete (ran, no
    known outcome) / missing. Latest matching run per cell wins."""
    if step["manifest_id"] and manifest:
        return [{"problem": c["problem"], "repeat": c["repeat"], "status": c["status"]}
                for c in campaign.grid(manifest, runs)["cells"]]
    latest: dict[tuple[str, int], dict] = {}
    for r in runs:
        if not _matches(step, r):
            continue
        cell = (r.get("problem_id") or "?", int(r.get("repeat_index") or 1))
        if cell not in latest or (r.get("timestamp_utc") or "") >= (latest[cell].get("timestamp_utc") or ""):
            latest[cell] = r
    out = []
    for p in step["problems"]:
        for rep in range(1, step["repeat_n"] + 1):
            r = latest.get((p, rep))
            if r is None:
                status = "missing"
            else:
                v = r.get("e2e_pass")
                status = "incomplete" if not isinstance(v, bool) else ("pass" if v else "fail")
            out.append({"problem": p, "repeat": rep, "status": status})
    return out


def todo(step: dict, runs: list[dict], manifest: dict | None = None) -> list[tuple[str, int]]:
    """Cells this step still has to run now."""
    cs = cells(step, runs, manifest)
    if step["only_missing"] or step["manifest_id"]:
        cs = [c for c in cs if c["status"] in ("missing", "incomplete")]
    return [(c["problem"], c["repeat"]) for c in cs]


def preview(queue: dict, runs: list[dict], manifests: list[dict]) -> dict:
    by_id = {m.get("manifest_id"): m for m in manifests}
    out, total, left = [], 0, 0
    for s in queue["steps"]:
        manifest = by_id.get(s["manifest_id"]) if s["manifest_id"] else None
        cs = cells(s, runs, manifest)
        counts = {k: sum(1 for c in cs if c["status"] == k) for k in ("pass", "fail", "incomplete", "missing")}
        n_todo = len(todo(s, runs, manifest)) if s["enabled"] else 0
        total += len(cs) if s["enabled"] else 0
        left += n_todo
        issue = None
        if s["manifest_id"] and manifest is None:
            issue = "frozen campaign not found (was it created on another machine that is not synced?)"
        out.append({"step_id": s["step_id"], "cells": cs, "counts": counts, "total": len(cs), "todo": n_todo,
                    "issue": issue})
    return {"steps": out, "total_runs": total, "runs_left": left}
