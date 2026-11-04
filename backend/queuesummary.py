"""One summary per experiment (queue): which problems were solved, which were not, and how long everything took.

Built only from recorded runs, so it works for any machine's runs and for experiments that were started from a queue or by hand.
Rules, the same as the queue's own progress count:
  - runs are grouped by the experiment name (a "(restart 2)" round belongs to the same experiment);
  - inside an experiment, runs are grouped by configuration (agent, model, action space, judge, config label, switches);
  - the latest run of a (problem, repeat) cell is the one that counts, and the number of attempts on the cell is shown;
  - a run with no known outcome is "incomplete", never counted as a failure.
Time: `duration_s` (the whole run, deploy to result) when the run has it; older runs only have SREGym's diagnosis and
mitigation timers, which are added and reported as "agent time" so nobody reads them as the whole run.
"""
from __future__ import annotations

import queueplan


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def run_seconds(r: dict) -> tuple[float | None, str]:
    """(seconds, kind): kind is "run" for the whole run, "agent" for diagnosis plus mitigation time only, "" when unknown."""
    d = _num(r.get("duration_s"))
    if d is not None and d > 0:
        return d, "run"
    parts = [x for x in (_num(r.get("ttd_s")), _num(r.get("ttm_s"))) if x is not None]
    return (sum(parts), "agent") if parts else (None, "")


def run_status(r: dict) -> str:
    v = r.get("e2e_pass")
    return "incomplete" if not isinstance(v, bool) else ("pass" if v else "fail")


def _ts(r: dict) -> str:
    return r.get("timestamp_utc") or ""


def _verdict(passed: int, failed: int, incomplete: int) -> str:
    if passed and not failed and not incomplete:
        return "solved"
    if passed:
        return "partly"
    if failed:
        return "not solved"
    return "incomplete"


def _problem_rows(cell_runs: dict[tuple[str, int], list[dict]]) -> list[dict]:
    by_problem: dict[str, list[tuple[int, list[dict]]]] = {}
    for (problem, rep), rs in cell_runs.items():
        by_problem.setdefault(problem, []).append((rep, rs))
    rows = []
    for problem, reps in by_problem.items():
        repeats, passed, failed, incomplete, seconds, tokens = [], 0, 0, 0, 0.0, 0
        for rep, rs in sorted(reps):
            latest = max(rs, key=_ts)
            status = run_status(latest)
            passed += status == "pass"
            failed += status == "fail"
            incomplete += status == "incomplete"
            secs, kind = run_seconds(latest)
            seconds += secs or 0
            tokens += int(_num(latest.get("total_tokens")) or 0)
            repeats.append({"repeat": rep, "status": status, "seconds": secs, "time_kind": kind, "attempts": len(rs),
                            "by": latest.get("run_by") or "unknown", "at": _ts(latest), "run_id": latest.get("run_id"),
                            "diagnosis_pass": latest.get("diagnosis_pass"), "mitigation_pass": latest.get("mitigation_pass"),
                            "tokens": latest.get("total_tokens"), "reason": latest.get("incomplete_reason") or ""})
        rows.append({"problem": problem, "verdict": _verdict(passed, failed, incomplete), "passed": passed, "failed": failed,
                     "incomplete": incomplete, "seconds": seconds, "tokens": tokens, "repeats": repeats})
    order = {"not solved": 0, "partly": 1, "incomplete": 2, "solved": 3}   # the problems that need a look come first
    return sorted(rows, key=lambda p: (order[p["verdict"]], p["problem"]))


def _config(key: tuple, rs: list[dict]) -> dict:
    agent, action_space, model, judge, label, switches = key
    cell_runs: dict[tuple[str, int], list[dict]] = {}
    for r in rs:
        cell_runs.setdefault((r.get("problem_id") or "?", int(r.get("repeat_index") or 1)), []).append(r)
    problems = _problem_rows(cell_runs)
    cells = [c for p in problems for c in p["repeats"]]
    counted = [c for c in cells if c["status"] != "incomplete"]
    secs = [c["seconds"] for c in cells if c["seconds"] is not None]
    kinds = {c["time_kind"] for c in cells if c["seconds"] is not None}
    diag = [c["diagnosis_pass"] for c in cells if isinstance(c["diagnosis_pass"], bool)]
    return {
        "agent_id": agent, "action_space": action_space, "model_id": model, "judge_model_id": judge,
        "config_label": label, "agent_switches": switches,
        "cells": len(cells),
        "passed": sum(c["status"] == "pass" for c in cells), "failed": sum(c["status"] == "fail" for c in cells),
        "incomplete": sum(c["status"] == "incomplete" for c in cells),
        "pass_rate": (sum(c["status"] == "pass" for c in cells) / len(counted)) if counted else None,
        "diagnosis_passed": sum(diag), "diagnosis_known": len(diag),
        "problems_solved": sum(p["verdict"] == "solved" for p in problems), "problems": len(problems),
        "seconds": sum(secs), "seconds_known_for": len(secs),
        "time_kind": "" if not kinds else ("run" if kinds == {"run"} else ("agent" if kinds == {"agent"} else "mixed")),
        "tokens": sum(p["tokens"] for p in problems),
        "attempts_extra": sum(c["attempts"] - 1 for c in cells),
        "problem_rows": problems,
    }


def summarize(runs: list[dict]) -> list[dict]:
    """[{name, runs, last, first, people, cells, passed, failed, incomplete, seconds, tokens, configs: [...]}] newest first."""
    by_exp: dict[str, list[dict]] = {}
    for r in runs:
        base, _ = queueplan.parse_label(r.get("experiment") or "")
        if base:
            by_exp.setdefault(base, []).append(r)
    out = []
    for name, rs in by_exp.items():
        groups: dict[tuple, list[dict]] = {}
        for r in rs:
            key = (r.get("agent_id") or "", r.get("action_space") or "", r.get("model_id") or "", r.get("judge_model_id") or "",
                   (r.get("config_label") or "").strip(), r.get("agent_switches") or "")
            groups.setdefault(key, []).append(r)
        configs = sorted((_config(k, g) for k, g in groups.items()), key=lambda c: (c["config_label"], c["agent_switches"]))
        stamps = sorted(_ts(r) for r in rs if _ts(r))
        out.append({
            "name": name, "runs": len(rs),
            "first": stamps[0] if stamps else "", "last": stamps[-1] if stamps else "",
            "people": sorted({r.get("run_by") or "unknown" for r in rs}),
            "cells": sum(c["cells"] for c in configs), "passed": sum(c["passed"] for c in configs),
            "failed": sum(c["failed"] for c in configs), "incomplete": sum(c["incomplete"] for c in configs),
            "seconds": sum(c["seconds"] for c in configs), "tokens": sum(c["tokens"] for c in configs),
            "time_kinds": sorted({c["time_kind"] for c in configs if c["time_kind"]}),
            "configs": configs,
        })
    return sorted(out, key=lambda e: e["last"], reverse=True)
