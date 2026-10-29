"""Two audits the team asked for: (1) how far a judge can be trusted (agreement with hand labels,
Cohen's kappa, judge against judge), and (2) the standing shortcut check (does a configuration clearly
beat the dumb baseline, and what happened on no-fault controls)."""

from __future__ import annotations

import re
from collections import defaultdict

import stats

FLOOR_LABEL = "dumb-baseline"  # config_label that marks the near-zero-LLM baseline agent runs
CONTROL_RE = re.compile(r"(no[_-]?fault|noop|no_op|control)", re.I)


def is_control(problem_id: str | None) -> bool:
    return bool(problem_id and CONTROL_RE.search(problem_id))


def _confusion(judge: list[bool], human: list[bool]) -> dict:
    tp = sum(1 for j, h in zip(judge, human) if j and h)
    fp = sum(1 for j, h in zip(judge, human) if j and not h)
    tn = sum(1 for j, h in zip(judge, human) if not j and not h)
    fn = sum(1 for j, h in zip(judge, human) if not j and h)
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn}


def judge_audit(runs: list[dict], labels: list[dict], rejudges: list[dict]) -> dict:
    """Judge verdicts per run come from two places: the judge the run was graded with at the time
    (`diagnosis_pass`, named by `judge_model_id`) and any later re-judge records. Each judge is scored
    against the hand labels (human says the diagnosis is correct / not) on the runs both have, and
    against every other judge on the runs they share."""
    human = {l["run_id"]: bool(l["correct"]) for l in labels if l.get("run_id") and isinstance(l.get("correct"), bool)}
    verdicts: dict[str, dict[str, bool]] = defaultdict(dict)  # judge -> run_id -> passed
    for r in runs:
        v = r.get("diagnosis_pass")
        if isinstance(v, bool) and r.get("run_id") and r.get("judge_model_id"):
            verdicts[r["judge_model_id"]][r["run_id"]] = v
    for rj in rejudges:
        v = rj.get("success")
        if isinstance(v, bool) and rj.get("run_id") and rj.get("judge_model_id"):
            verdicts[rj["judge_model_id"]][rj["run_id"]] = v

    judges = []
    for judge, byrun in sorted(verdicts.items()):
        ids = sorted(set(byrun) & set(human))
        j = [byrun[i] for i in ids]
        h = [human[i] for i in ids]
        agree = sum(1 for a, b in zip(j, h) if a == b)
        judges.append({
            "judge": judge, "judged": len(byrun), "labeled_overlap": len(ids), "agree": agree,
            "agreement": (agree / len(ids)) if ids else None,
            "agreement_wilson": stats.wilson(agree, len(ids)) if ids else None,
            "kappa": stats.cohen_kappa(j, h), **_confusion(j, h),
        })
    pairs = []
    names = sorted(verdicts)
    for x in range(len(names)):
        for y in range(x + 1, len(names)):
            ids = sorted(set(verdicts[names[x]]) & set(verdicts[names[y]]))
            if ids:
                pairs.append({"a": names[x], "b": names[y], "n": len(ids),
                              "kappa": stats.cohen_kappa([verdicts[names[x]][i] for i in ids],
                                                         [verdicts[names[y]][i] for i in ids]),
                              "agree": sum(1 for i in ids if verdicts[names[x]][i] == verdicts[names[y]][i])})
    return {"labeled": len(human), "judges": judges, "pairs": pairs,
            "warnings": ([] if len(human) >= 30 else
                         [f"Only {len(human)} hand labels; aim for about 30 before trusting a kappa."])}


def label_queue(runs: list[dict], labels: list[dict], limit: int = 200) -> list[dict]:
    """Runs with a stored diagnosis that nobody has hand-labelled yet. The original verdict is NOT
    included so the labeller is not anchored on it."""
    done = {l["run_id"] for l in labels if l.get("run_id")}
    out = []
    for r in runs:
        if r.get("run_id") and r["run_id"] not in done and (r.get("diagnosis_text") or "").strip():
            out.append({"run_id": r["run_id"], "problem_id": r.get("problem_id"),
                        "diagnosis_text": r["diagnosis_text"], "timestamp_utc": r.get("timestamp_utc")})
    return out[:limit]


def standing_audit(runs: list[dict], metric: str = "e2e_pass", floor_label: str = FLOOR_LABEL) -> dict:
    """For each configuration: does it clearly beat the dumb baseline run on the same class of runs, and
    what happened on no-fault control problems. `clearly` = the Wilson lower bound is above the floor's
    point rate. No floor runs at all is reported as such, never as a pass."""
    groups = stats.summarize(runs, metric)
    floors = {g["run_class"]: g for g in groups if g["config_label"] == floor_label and g["n"]}
    controls = [r for r in runs if is_control(r.get("problem_id"))]
    rows = []
    for g in groups:
        if g["config_label"] == floor_label:
            continue
        floor = floors.get(g["run_class"])
        ctrl = [r for r in controls if stats.config_key(r) == g["key"]]
        ctrl_known = [r for r in ctrl if isinstance(r.get(metric), bool)]
        status = ("no floor runs yet" if floor is None else
                  ("clearly above floor" if g["wilson_lo"] is not None and g["wilson_lo"] > floor["rate"]
                   else "NOT clearly above floor"))
        rows.append({
            "key": g["key"], "config_label": g["config_label"], "agent_id": g["agent_id"], "model_id": g["model_id"],
            "run_class": g["run_class"], "rate": g["rate"], "wilson_lo": g["wilson_lo"], "n": g["n"],
            "floor_rate": floor["rate"] if floor else None, "floor_n": floor["n"] if floor else 0,
            "floor_status": status,
            "control_runs": len(ctrl), "control_passes": sum(1 for r in ctrl_known if r[metric]),
        })
    notes = ["On a no-fault control, what 'pass' means is defined by that problem's own oracle; read the stories "
             "for whether the agent invented a cause or took a write action."]
    if not controls:
        notes.append("No no-fault control runs recorded yet (problem ids matching no_fault / noop / control).")
    return {"floor_label": floor_label, "metric": metric, "rows": rows, "notes": notes}
