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
    if shares is None and split == "repeat" and machines > 1 and not raw.get("queue_id"):
        # A new queue split by repeat gets its shares written down once, dealt so that one problem's repeats land on
        # different machines (see deal_repeat_shares). Existing queues keep the plain round-robin they started with.
        shares = deal_repeat_shares({"steps": steps, "split": split}, machines)
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


def deal_repeat_shares(queue: dict, machines: int) -> list[list[str]]:
    """Shares of "problem #r" units for a queue split by repeat. Plain round-robin over the repeat-major unit list puts
    every repeat of a problem on the same machine whenever the problem count is a multiple of the machine count (12
    problems on 4 machines: machine 1 gets problems 1, 5 and 9 three times), which is a split by problem in disguise:
    one broken machine then loses whole problems. Here repeat r is shifted by r - 1 machines in that case, so the
    repeats of one problem go to different machines and the shares stay the same size. When the problem count is not
    a multiple, round-robin already mixes them and is kept as it is."""
    probs = queue_problems(queue)
    units = queue_units(queue)
    n = max(1, int(machines))
    out: list[list[str]] = [[] for _ in range(n)]
    rotate = bool(probs) and len(probs) % n == 0
    for k, u in enumerate(units):
        r = int(u.rsplit("#", 1)[1]) if "#" in u else 1
        out[(k + (r - 1 if rotate else 0)) % n].append(u)
    return out


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


def share_of(queue: dict, problem: str, repeat: int, lists: list[list[str]] | None = None) -> int:
    """Which machine share (1-based) a cell belongs to."""
    lists = lists or share_lists(queue)
    owner = {u: k for k, x in enumerate(lists, 1) for u in x}
    return owner.get(problem, owner.get(unit(problem, repeat), 1))


def apply_resets(queue: dict, runs: list[dict], resets: list[dict], label: str | None) -> list[dict]:
    """"Restart my share" does not delete anything. It records a reset (experiment label, share, time). For that label, runs
    of that share recorded before the reset time are left out here, so those cells count as not done and run again, while the
    other shares keep their progress. Runs of other labels are never touched."""
    if not label or not resets or int(queue.get("machines") or 1) <= 1:
        return runs
    mine = {int(r.get("share") or 0): (r.get("at") or "") for r in resets if r.get("experiment") == label and r.get("at")}
    if not mine:
        return runs
    lists = share_lists(queue)
    owner = {u: k for k, x in enumerate(lists, 1) for u in x}
    out = []
    for r in runs:
        if (r.get("experiment") or "") == label:
            at = mine.get(owner.get(r.get("problem_id") or "?", owner.get(unit(r.get("problem_id") or "?", int(r.get("repeat_index") or 1)), 1)))
            if at and (r.get("timestamp_utc") or "") < at:
                continue
        out.append(r)
    return out


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


# matches no run: used when no experiment name has been typed
_NO_EXPERIMENT = "\0no experiment name"


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
        if r.get("origin") == "quick":
            continue  # a quick run from Setup & quick run is never a cell of a queue, whatever its settings and name
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
            share: int | None = None, claims_for=None, now: float | None = None, resets: list[dict] | None = None,
            assigned: dict | None = None) -> dict:
    """`experiment` is the name typed in the run bar (or None). Progress is counted for its newest round.
    `share` is the machine share picked on this machine (None = all problems): total_runs and runs_left, and each
    step's total and todo, count only that share, while every cell is still returned (with `mine` set) so the screen
    shows the whole queue. `shares` gives the progress of every share and who has been running it."""
    base = (experiment or "").strip()
    label = experiment_label(base, runs) if base else None
    all_runs = runs
    runs = apply_resets(queue, runs, resets or [], label)
    # No name typed yet: nothing is recorded under it, so every cell is to do. It must NOT fall back to "any experiment":
    # a machine that had not typed the name showed other experiments' runs as this one's progress (a board full of "finished").
    scope = label if label else _NO_EXPERIMENT
    by_id = {m.get("manifest_id"): m for m in manifests}
    only = share_set(queue, share)
    lists = share_lists(queue)
    owner = {u: k for k, x in enumerate(lists, 1) for u in x}
    per_share = [{"share": k, "problems": x, "total": 0, "done": 0, "left": 0, "passed": 0, "failed": 0, "people": {}, "last": ""}
                 for k, x in enumerate(lists, 1)]
    out, total, left, all_total, all_left = [], 0, 0, 0, 0
    for s in queue["steps"]:
        manifest = by_id.get(s["manifest_id"]) if s["manifest_id"] else None
        cs = cells(s, runs, manifest, scope)
        for c in cs:
            c["mine"] = in_share(only, c["problem"], c["repeat"])
        mine = [c for c in cs if c["mine"]]
        counts = {k: sum(1 for c in mine if c["status"] == k) for k in ("pass", "fail", "incomplete", "missing")}
        # The whole queue (every machine's share), so a machine running one share also sees the team's progress.
        all_counts = {k: sum(1 for c in cs if c["status"] == k) for k in ("pass", "fail", "incomplete", "missing")}
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
                    "all_counts": all_counts, "all_total": len(cs),
                    "all_todo": sum(1 for c in cs if _is_todo(s, c)) if s["enabled"] else 0, "issue": issue})
    info = {"runs": 0, "people": [], "first": "", "last": "", "commits": []}
    if label:
        mine_runs = [r for r in runs if (r.get("experiment") or "") == label and r.get("origin") != "quick"]
        stamps = sorted(r.get("timestamp_utc") or "" for r in mine_runs if r.get("timestamp_utc"))
        info = {"runs": len(mine_runs), "people": sorted({r.get("run_by") or "unknown" for r in mine_runs}),
                "first": stamps[0] if stamps else "", "last": stamps[-1] if stamps else "",
                "commits": sorted({(r.get("git_commit") or "")[:8] for r in mine_runs if r.get("git_commit")})}
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
        sh["assigned"] = list((assigned or {}).get(sh["share"]) or [])
    everything = claims.claim_for(taken, 0)
    activity = _activity(all_runs, label, now)
    return {"steps": out, "total_runs": total, "runs_left": left, "experiment_label": label, "needs_name": not label,
            "experiments": experiment_names(runs), "machines": int(queue.get("machines") or 1),
            "share": share if only is not None else None, "shares": per_share,
            "all_total_runs": all_total, "all_runs_left": all_left,
            "machine_versions": machine_versions, "this_machine": claims.machine_name(), "label_info": info,
            "activity": activity,
            "whole_queue_claim": ({k: everything.get(k) for k in ("run_by", "machine", "status", "started_at", "updated_at", "now_problem")}
                                  if everything else None)}


def _activity(runs: list[dict], label: str | None, now: float | None = None, hours: int = 24) -> dict:
    """What the experiment recorded in the last `hours` hours: attempts per hour and per person, for the live chart and the
    rate behind the time estimate. Quick runs are left out, like everywhere else."""
    import calendar
    import time as _t
    now = _t.time() if now is None else now
    out = {"hours": hours, "buckets": [0] * hours, "by_person": {}, "attempts": 0, "per_hour": 0.0, "span_h": 0.0}
    if not label:
        return out
    stamps = []
    for r in runs:
        if (r.get("experiment") or "") != label or r.get("origin") == "quick":
            continue
        try:
            t = calendar.timegm(_t.strptime((r.get("timestamp_utc") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        age_h = (now - t) / 3600.0
        if 0 <= age_h < hours:
            i = hours - 1 - int(age_h)
            out["buckets"][i] += 1
            who = r.get("run_by") or "unknown"
            pp = out["by_person"].setdefault(who, [0] * hours)
            pp[i] += 1
            stamps.append(t)
    out["attempts"] = len(stamps)
    if len(stamps) >= 2:
        span = max(1.0, (now - min(stamps)) / 3600.0)
        out["span_h"] = round(span, 1)
        out["per_hour"] = round(len(stamps) / min(span, float(hours)), 2)
    return out


# ---- Moving work between shares ---------------------------------------------------------------------------------

def unit_of(queue: dict, problem: str, repeat: int) -> str:
    """The share unit a cell belongs to: the problem, or "problem #r" when the queue is split by problem and repeat."""
    return unit(problem, repeat) if (queue.get("split") or "problem") == "repeat" else problem


def undone_runs(queue: dict, runs: list[dict], manifests: list[dict], experiment: str | None,
                resets: list[dict] | None = None) -> dict[str, int]:
    """For every share unit that still has cells to run (in an enabled step) under this experiment name: how many runs
    are left. A unit is a problem, or a "problem #r" pair; one unit is one cell per enabled step, so it can be many runs."""
    base = (experiment or "").strip()
    label = experiment_label(base, runs) if base else None
    runs = apply_resets(queue, runs, resets or [], label)
    scope = label if label else _NO_EXPERIMENT
    by_id = {m.get("manifest_id"): m for m in manifests}
    out: dict[str, int] = {}
    for s in queue["steps"]:
        if not s["enabled"]:
            continue
        for c in cells(s, runs, by_id.get(s["manifest_id"]) if s["manifest_id"] else None, scope):
            if _is_todo(s, c):
                u = unit_of(queue, c["problem"], c["repeat"])
                out[u] = out.get(u, 0) + 1
    return out


def undone_units(queue: dict, runs: list[dict], manifests: list[dict], experiment: str | None,
                 resets: list[dict] | None = None) -> set[str]:
    """Share units that still have a cell to run (in an enabled step) under this experiment name."""
    return set(undone_runs(queue, runs, manifests, experiment, resets))


def _check_share(queue: dict, k: int) -> int:
    n = int(queue.get("machines") or 1)
    if n <= 1:
        raise ValueError("this queue is not split across machines")
    if not isinstance(k, int) or not 1 <= k <= n:
        raise ValueError(f"share must be between 1 and {n}")
    return k


def move_units(queue: dict, units: list[str], to_share: int) -> list[list[str]]:
    """The shares after moving `units` (problems, or "problem #r" pairs) to share `to_share`. Nothing else changes."""
    _check_share(queue, to_share)
    units = [str(u).strip() for u in units if str(u).strip()]
    if not units:
        raise ValueError("choose at least one problem to move")
    known = set(queue_units(queue))
    bad = [u for u in units if u not in known]
    if bad:
        raise ValueError(f"not part of this queue: {', '.join(bad[:3])}")
    gone = set(units)
    lists = [[u for u in x if u not in gone] for x in share_lists(queue)]
    lists[to_share - 1].extend(u for u in dict.fromkeys(units))
    return lists


def redistribute(queue: dict, from_share: int, to_shares: list[int] | None, movable: set[str] | dict[str, int]) -> tuple[list[list[str]], list[tuple[str, int]]]:
    """Moves the work of `from_share` that is not done yet (`movable`) to the other shares, to whichever target has the
    fewest runs left. `movable` is a set of units, or a dict unit -> runs left (better: the balance is then by runs, not by
    number of problems; a plain set weighs every unit the same). Biggest units are placed first, which levels the load
    best. Units already done stay where they were, so the record of who did what does not change.
    Returns (new shares, [(unit, target share)])."""
    _check_share(queue, from_share)
    n = int(queue.get("machines") or 1)
    targets = [t for t in (to_shares or [k for k in range(1, n + 1) if k != from_share]) if t != from_share]
    if not targets:
        raise ValueError("choose at least one other share to move the work to")
    for t in targets:
        _check_share(queue, t)
    weight = dict(movable) if isinstance(movable, dict) else {u: 1 for u in movable}
    lists = [list(x) for x in share_lists(queue)]
    moving = [u for u in lists[from_share - 1] if u in weight]
    if not moving:
        raise ValueError(f"share {from_share} has nothing left to move")
    gone = set(moving)
    lists[from_share - 1] = [u for u in lists[from_share - 1] if u not in gone]
    load = {t: sum(weight.get(u, 0) for u in lists[t - 1]) for t in targets}
    moved = []
    for u in sorted(moving, key=lambda x: (-weight[x], moving.index(x))):
        t = min(targets, key=lambda k: (load[k], k))
        lists[t - 1].append(u)
        load[t] += weight[u]
        moved.append((u, t))
    return lists, moved


def reassign(queue: dict, moves: list[tuple[str, int]], movable: set[str]) -> tuple[list[list[str]], list[tuple[str, int]]]:
    """The shares after sending each unit to the share you chose for it (any unit, from whichever share holds it now).
    Only units that still have runs to do can move: a finished unit already has its result under the share that ran it.
    A unit sent to the share that already holds it is left alone. Returns (new shares, the moves that changed something)."""
    if not moves:
        raise ValueError("choose at least one problem to move")
    seen: dict[str, int] = {}
    for u, to in moves:
        u = str(u).strip()
        _check_share(queue, to)
        if u in seen and seen[u] != to:
            raise ValueError(f"{u} is listed for two different shares")
        seen[u] = to
    known = set(queue_units(queue))
    bad = [u for u in seen if u not in known]
    if bad:
        raise ValueError(f"not part of this queue: {', '.join(bad[:3])}")
    done = [u for u in seen if u not in movable]
    if done:
        raise ValueError(f"already finished, cannot move: {', '.join(done[:3])}")
    lists = [list(x) for x in share_lists(queue)]
    where = {u: i for i, x in enumerate(lists) for u in x}
    changed = [(u, to) for u, to in seen.items() if where.get(u) != to - 1]
    if not changed:
        raise ValueError("nothing to change: every problem is already in the share you chose")
    gone = {u for u, _ in changed}
    lists = [[u for u in x if u not in gone] for x in lists]
    for u, to in changed:
        lists[to - 1].append(u)
    return lists, changed
