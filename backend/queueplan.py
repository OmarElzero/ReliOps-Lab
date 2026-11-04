"""Run queues: an ordered list of steps, each one a configuration (agent, model, judge, label, class)
applied to a set of problems with a repeat count. A step is run cell by cell, a cell being one
(problem, repeat). Running a queue again only fills the cells that have no known outcome, so a queue
that was stopped, crashed or interrupted resumes where it left off."""

from __future__ import annotations

import re
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
    purpose = (raw.get("purpose") or "").strip() or None
    if purpose not in (None, "experiment", "dev", "final"):
        raise ValueError("purpose must be experiment, dev, final or empty (automatic)")
    manifest_id = (raw.get("manifest_id") or "").strip() or None
    if purpose == "final" and run_class != "frozen":
        raise ValueError("purpose ‘final’ is for frozen-campaign steps; an exploration run is an experiment or dev")
    if run_class == "frozen" and not manifest_id:
        raise ValueError("a frozen step needs a frozen campaign (manifest): freeze one in the Campaigns tab")
    import runner  # local import: runner does not import this module, but keep the dependency one-way
    switches = runner.clean_switches(raw.get("agent_switches"))
    if switches and agent_id != runner.SWITCH_AGENT:
        raise ValueError(f"agent switches only exist in {runner.SWITCH_AGENT}; pick that agent or untick the switches")
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
        "agent_switches": switches,
        "run_class": run_class, "manifest_id": manifest_id, "purpose": purpose,
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
    try:
        machines = int(raw.get("machines") or 1)
    except (TypeError, ValueError):
        raise ValueError("machines must be a whole number") from None
    if not 1 <= machines <= MAX_MACHINES:
        raise ValueError(f"machines must be between 1 and {MAX_MACHINES}")
    shares = None
    if machines > 1 and raw.get("shares"):
        given = raw["shares"]
        if not isinstance(given, list) or len(given) != machines or not all(isinstance(x, list) for x in given):
            raise ValueError(f"shares must be {machines} lists of problems, one per machine")
        shares = [[str(p).strip() for p in x if str(p).strip()] for x in given]
        seen: set[str] = set()
        for x in shares:
            for p in x:
                if p in seen:
                    raise ValueError(f"problem {p} is in two machine shares")
                seen.add(p)
    split = raw.get("split") or "problem"
    if split not in SPLITS:
        raise ValueError("split must be 'problem' or 'repeat'")
    order = raw.get("order") or "step"
    if order not in ORDERS:
        raise ValueError("order must be 'step' or 'cell'")
    return {"queue_id": raw.get("queue_id") or uuid.uuid4().hex[:10], "name": name, "steps": steps,
            "created_by": raw.get("created_by") or "unknown", "machines": machines, "shares": shares, "split": split,
            "order": order}


# ---- Machine shares -------------------------------------------------------------------------------------------
# A queue can be split across several machines (members' laptops). Everyone sees the whole queue and its whole grid;
# each machine picks its share (k of N) and runs only the cells of the problems in that share. The split is by
# problem, not by cell, so every configuration of a problem runs on the same machine (paired comparisons, and each
# machine pulls an app's images once). Shares are fixed from the queue itself, never from what is left, so two
# machines never pick the same cell however far each has got.
MAX_MACHINES = 20
# "problem": a share is a set of problems (all their repeats). "repeat": a share is a set of (problem, repeat) pairs,
# written "problem #r", which balances the machines better when the problems do not divide evenly (12 problems x 3
# repeats on 5 machines: 8/7/7/7/7 instead of 3/3/2/2/2 problems). Every configuration of one (problem, repeat) still
# runs on the same machine.
SPLITS = ("problem", "repeat")
# "step": the steps run one after another (every cell of step 1, then step 2, ...). "cell": for each (problem, repeat),
# every step back to back, so the configurations being compared run close together in time on the same machine.
ORDERS = ("step", "cell")


def unit(problem: str, repeat: int) -> str:
    return f"{problem} #{int(repeat)}"


def in_share(only: set[str] | None, problem: str, repeat: int) -> bool:
    return only is None or problem in only or unit(problem, repeat) in only


def queue_units(queue: dict) -> list[str]:
    """What the shares are made of: problems, or "problem #r" pairs (repeat-major, so round-robin mixes problems)."""
    probs = queue_problems(queue)
    if (queue.get("split") or "problem") != "repeat":
        return probs
    reps: dict[str, int] = {}
    for s in queue.get("steps") or []:
        for p in s.get("problems") or []:
            reps[p] = max(reps.get(p, 0), int(s.get("repeat_n") or 1))
    top = max(reps.values(), default=1)
    return [unit(p, r) for r in range(1, top + 1) for p in probs if r <= reps[p]]


def queue_problems(queue: dict) -> list[str]:
    """Every problem of the queue's steps, in order of first appearance."""
    out: list[str] = []
    for s in queue.get("steps") or []:
        for p in s.get("problems") or []:
            if p not in out:
                out.append(p)
    return out


def share_lists(queue: dict) -> list[list[str]]:
    """The problems of each machine share (index 0 = share 1). Hand-made shares (from the queue generator) are used as
    given; a problem of the queue that no hand-made share names goes to the share with the fewest problems. Without
    hand-made shares the problems are dealt round-robin, so the shares differ by at most one problem."""
    n = int(queue.get("machines") or 1)
    allp = queue_units(queue)
    if n <= 1:
        return [allp]
    given = queue.get("shares")
    if given and len(given) == n:
        known = set(allp)
        out = [[p for p in x if p in known] for x in given]
        placed = {p for x in out for p in x}
        for p in allp:
            if p not in placed:
                min(out, key=len).append(p)
        return out
    return [allp[i::n] for i in range(n)]


def share_set(queue: dict, share: int | None) -> set[str] | None:
    """The problems machine share `share` (1-based) runs; None means all of them (no share picked, or one machine)."""
    n = int(queue.get("machines") or 1)
    if not share or n <= 1:
        return None
    if not 1 <= int(share) <= n:
        raise ValueError(f"machine share must be between 1 and {n}")
    return set(share_lists(queue)[int(share) - 1])


_ROUND_RE = re.compile(r"^(?P<base>.*?)(?: \(restart (?P<n>\d+)\))?$")


def parse_label(label: str) -> tuple[str, int]:
    """'name' -> ('name', 1); 'name (restart 3)' -> ('name', 3)."""
    m = _ROUND_RE.match((label or "").strip())
    return (m.group("base").strip(), int(m.group("n")) if m.group("n") else 1) if m else ((label or "").strip(), 1)


def make_label(base: str, round_n: int) -> str:
    return base if round_n <= 1 else f"{base} (restart {round_n})"


def current_round(base: str, runs: list[dict]) -> int:
    """The newest round recorded for this experiment name (1 when it has none yet)."""
    best = 1
    for r in runs:
        b, n = parse_label(r.get("experiment") or "")
        if b and b == base:
            best = max(best, n)
    return best


def experiment_label(base: str, runs: list[dict], restart: bool = False) -> str:
    """The label runs are stamped with: the newest round of this name, or the next one when restarting."""
    base = (base or "").strip()
    n = current_round(base, runs)
    if restart and any(parse_label(r.get("experiment") or "")[0] == base for r in runs):
        n += 1
    return make_label(base, n)


def experiment_names(runs: list[dict]) -> list[dict]:
    """Experiment names seen on recorded runs, newest first: [{name, runs, last}]."""
    seen: dict[str, dict] = {}
    for r in runs:
        b, _ = parse_label(r.get("experiment") or "")
        if not b:
            continue
        e = seen.setdefault(b, {"name": b, "runs": 0, "last": ""})
        e["runs"] += 1
        e["last"] = max(e["last"], r.get("timestamp_utc") or "")
    return sorted(seen.values(), key=lambda e: e["last"], reverse=True)


def _matches(step: dict, run: dict) -> bool:
    return ((run.get("agent_id") or "") == step["agent_id"]
            and (run.get("model_id") or "") == step["model_id"]
            and (run.get("action_space") or "") == (step["action_space"] or "")
            and (run.get("judge_model_id") or "") == step["judge_model_id"]
            and (run.get("config_label") or "").strip() == step["config_label"]
            and (run.get("agent_switches") or "") == "+".join(step.get("agent_switches") or [])
            and (run.get("run_class") or "exploration") == step["run_class"]
            and (not step["manifest_id"] or run.get("manifest_id") == step["manifest_id"]))


def cells(step: dict, runs: list[dict], manifest: dict | None = None, experiment: str | None = None) -> list[dict]:
    """[{problem, repeat, status}] for every cell of the step: pass / fail / incomplete (ran, no
    known outcome) / missing. Latest matching run per cell wins."""
    if step["manifest_id"] and manifest:
        return [{"problem": c["problem"], "repeat": c["repeat"], "status": c["status"]}
                for c in campaign.grid(manifest, runs)["cells"]]
    latest: dict[tuple[str, int], dict] = {}
    for r in runs:
        if not _matches(step, r):
            continue
        if experiment is not None and (r.get("experiment") or "") != experiment:
            continue  # a step's progress is counted per experiment label (frozen campaigns are counted by manifest)
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
            cell = {"problem": p, "repeat": rep, "status": status}
            if r is not None:
                cell.update(by=r.get("run_by") or "unknown", at=r.get("timestamp_utc") or "")
            out.append(cell)
    return out


def _is_todo(step: dict, c: dict) -> bool:
    return not (step["only_missing"] or step["manifest_id"]) or c["status"] in ("missing", "incomplete")


def todo(step: dict, runs: list[dict], manifest: dict | None = None, experiment: str | None = None,
         only: set[str] | None = None) -> list[tuple[str, int]]:
    """Cells this step still has to run now (only the problems in `only`, when given: this machine's share)."""
    cs = cells(step, runs, manifest, experiment)
    return [(c["problem"], c["repeat"]) for c in cs if _is_todo(step, c) and in_share(only, c["problem"], c["repeat"])]


def preview(queue: dict, runs: list[dict], manifests: list[dict], experiment: str | None = None,
            share: int | None = None, claims_for=None, now: float | None = None) -> dict:
    """`experiment` is the name typed in the run bar (or None). Progress is counted for its newest round.
    `share` is the machine share picked on this machine (None = all problems): total_runs and runs_left, and each
    step's total and todo, count only that share, while every cell is still returned (with `mine` set) so the screen
    shows the whole queue. `shares` gives the progress of every share and who has been running it."""
    base = (experiment or "").strip()
    label = experiment_label(base, runs) if base else None
    by_id = {m.get("manifest_id"): m for m in manifests}
    only = share_set(queue, share)
    lists = share_lists(queue)
    owner = {u: k for k, x in enumerate(lists, 1) for u in x}
    per_share = [{"share": k, "problems": x, "total": 0, "done": 0, "left": 0, "passed": 0, "failed": 0, "people": {}, "last": ""}
                 for k, x in enumerate(lists, 1)]
    out, total, left, all_total, all_left = [], 0, 0, 0, 0
    for s in queue["steps"]:
        manifest = by_id.get(s["manifest_id"]) if s["manifest_id"] else None
        cs = cells(s, runs, manifest, label)
        for c in cs:
            c["mine"] = in_share(only, c["problem"], c["repeat"])
        mine = [c for c in cs if c["mine"]]
        counts = {k: sum(1 for c in mine if c["status"] == k) for k in ("pass", "fail", "incomplete", "missing")}
        n_todo = sum(1 for c in mine if _is_todo(s, c)) if s["enabled"] else 0
        if s["enabled"]:
            total += len(mine)
            left += n_todo
            all_total += len(cs)
            all_left += sum(1 for c in cs if _is_todo(s, c))
            for c in cs:
                sh = per_share[owner.get(c["problem"], owner.get(unit(c["problem"], c["repeat"]), 1)) - 1]
                sh["total"] += 1
                sh["done"] += c["status"] in ("pass", "fail")
                sh["passed"] += c["status"] == "pass"
                sh["failed"] += c["status"] == "fail"
                sh["left"] += _is_todo(s, c)
                if c.get("by"):
                    sh["people"][c["by"]] = max(sh["people"].get(c["by"], ""), c.get("at") or "")
                    sh["last"] = max(sh["last"], c.get("at") or "")
        issue = None
        if s["manifest_id"] and manifest is None:
            issue = "frozen campaign not found (was it created on another machine that is not synced?)"
        out.append({"step_id": s["step_id"], "cells": cs, "counts": counts, "total": len(mine), "todo": n_todo,
                    "issue": issue})
    groups: dict[str, dict] = {}
    if label:
        for r in runs:
            v = (r.get("machine_versions") or "").strip()
            if v and (r.get("experiment") or "") == label:
                g = groups.setdefault(v, {"versions": v, "n": 0, "people": {}})
                g["n"] += 1
                g["people"][r.get("run_by") or "?"] = 1
    machine_versions = [{"versions": g["versions"], "n": g["n"], "people": sorted(g["people"])}
                        for g in sorted(groups.values(), key=lambda g: -g["n"])]
    for sh in per_share:
        sh["people"] = [{"name": k, "last": v} for k, v in sorted(sh["people"].items(), key=lambda kv: kv[1], reverse=True)]
    # Who took which share (claims.py): shown even before the first run of a share is recorded.
    import claims
    taken = claims_for(label) if (claims_for and label) else []
    for sh in per_share:
        c = claims.claim_for(taken, sh["share"]) if int(queue.get("machines") or 1) > 1 else None
        sh["claim"] = ({k: c.get(k) for k in ("run_by", "machine", "status", "reason", "started_at", "updated_at", "now_problem", "step_name")}
                       if c else None)
        sh["state"] = claims.share_state(sh, c, now)
    everything = claims.claim_for(taken, 0)
    return {"steps": out, "total_runs": total, "runs_left": left, "experiment_label": label,
            "experiments": experiment_names(runs), "machines": int(queue.get("machines") or 1),
            "share": share if only is not None else None, "shares": per_share,
            "all_total_runs": all_total, "all_runs_left": all_left,
            "machine_versions": machine_versions, "this_machine": claims.machine_name(),
            "whole_queue_claim": ({k: everything.get(k) for k in ("run_by", "machine", "status", "started_at", "updated_at", "now_problem")}
                                  if everything else None)}
