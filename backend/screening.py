"""The screening report for a switch ladder (docs/plan/15 sections 7 and 8, rule in docs/plan/16 section 3).

The main stage of round 2 runs 18 configurations on the same (problem, repeat) pairs: B2 (the base), B2 plus one switch for each
of eight switches ("one on"), ALL (every switch), and ALL minus one switch ("one off"). This module turns the recorded runs into
what the rule needs, with nothing hand-computed:

  * per configuration: cells with a clean result, passes, pass@1 with a Wilson interval, pass^R (all R repeats of a problem pass),
    missing cells and the worst case they could hide;
  * 16 paired contrasts (8 one-on: B2+f against B2, 8 one-off: ALL against ALL-f), each an exact two-sided sign test on the pairs
    where the outcome flipped, with a Holm correction over the 16;
  * a verdict per switch, and the candidate configuration doc 15 section 7 would build from the effects.

It is a screening on dev data, not a claim: gains are reported only from the frozen 30. A run without a clean result (the cell is
incomplete, or a person tagged it external-fault) is set aside and reported as missing, never counted as a failure.

Pure standard library, like stats.py."""

from __future__ import annotations

import csv
import io
from collections import defaultdict

from stats import METRICS, _known, mcnemar_exact, wilson

ALPHA = 0.05
DEFAULT_BASE = "R2 B2"
DEFAULT_ALL = "R2 ALL"


def holm(pvals: list[float]) -> list[float]:
    """Holm step-down adjusted p-values, in the order given. Each adjusted value is at most 1 and never smaller than an earlier one."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    adj = [1.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * pvals[i])
        adj[i] = min(1.0, running)
    return adj


def detect_switches(labels: set[str], base: str, all_label: str) -> list[str]:
    """Switch names that have both a one-on configuration ("<base> +<f>") and a one-off one ("<all> -<f>")."""
    on = {l[len(base) + 2:] for l in labels if l.startswith(base + " +")}
    off = {l[len(all_label) + 2:] for l in labels if l.startswith(all_label + " -")}
    return sorted(on | off)


def _pairs(runs: list[dict], metric: str) -> tuple[dict, dict]:
    """(problem, repeat) -> outcome for the newest run of each pair that has one, and -> True for every pair attempted (any outcome)."""
    best: dict[tuple, tuple[str, bool]] = {}
    attempted: dict[tuple, bool] = {}
    for r in runs:
        pair = (r.get("problem_id") or "?", int(r.get("repeat_index") or 1))
        attempted[pair] = True
        v = _known(r, metric)
        if v is None:
            continue
        ts = r.get("timestamp_utc") or ""
        if pair not in best or ts >= best[pair][0]:
            best[pair] = (ts, v)
    return {p: v for p, (_, v) in best.items()}, attempted


def _contrast(cid: str, switch: str, kind: str, treated: str, control: str, pairs: dict[str, dict]) -> dict:
    t, c = pairs.get(treated, {}), pairs.get(control, {})
    shared = sorted(set(t) & set(c))
    only_t = sum(1 for p in shared if t[p] and not c[p])
    only_c = sum(1 for p in shared if c[p] and not t[p])
    both = sum(1 for p in shared if t[p] and c[p])
    p = mcnemar_exact(only_t, only_c) if shared else None
    diff = only_t - only_c
    return {"id": cid, "switch": switch, "kind": kind, "treated": treated, "control": control, "pairs": len(shared),
            "treated_only": only_t, "control_only": only_c, "both": both, "neither": len(shared) - only_t - only_c - both,
            "diff": diff, "direction": "up" if diff > 0 else "down" if diff < 0 else "none", "p": p,
            "p_holm": None, "passes_raw": bool(p is not None and p < ALPHA), "passes_holm": False}


def _verdict(on: dict, off: dict) -> tuple[str, str]:
    """The rule: a switch helps only if a contrast passes the exact test and the one-on and one-off effects point the same way.
    Passing without the Holm correction is a candidate, not a finding."""
    if not on["pairs"] and not off["pairs"]:
        return "no data", "neither contrast has a pair in common yet"
    significant = on["passes_raw"] or off["passes_raw"]
    if not significant:
        return "no evidence", "no contrast passes the exact sign test"
    holm_ok = on["passes_holm"] or off["passes_holm"]
    sign = lambda c: (c["diff"] > 0) - (c["diff"] < 0)     # noqa: E731
    s_on, s_off = sign(on), sign(off)
    if s_on > 0 and s_off > 0:
        return ("finding: helps" if holm_ok else "candidate: helps"), "one-on and one-off both point up" + ("; passes with Holm" if holm_ok else "; passes without correction only")
    if s_on < 0 and s_off < 0:
        return ("finding: hurts" if holm_ok else "candidate: hurts"), "one-on and one-off both point down" + ("; passes with Holm" if holm_ok else "; passes without correction only")
    return "mixed", "a contrast passes but one-on and one-off do not point the same way, so the rule does not count it"


def _candidate(base: str, all_label: str, switches: list[str], by_switch: dict[str, dict], all_vs_base: dict) -> dict:
    """The configuration doc 15 section 7 would run next, from the signs of the effects (counts of flipped pairs)."""
    if not all_vs_base["pairs"]:
        return {"switches": None, "note": "ALL and B2 have no pairs in common yet."}
    effects = {f: (by_switch[f]["on"]["diff"], by_switch[f]["off"]["diff"]) for f in switches}
    if all_vs_base["diff"] > 0:
        keep = [f for f in switches if not (effects[f][1] <= 0 and not (effects[f][0] > 0 and effects[f][1] == 0))]
        dropped = [f for f in switches if f not in keep]
        if len(keep) == len(switches):
            note = "ALL beats B2 and no switch can be dropped: the candidate is ALL, which has already run."
        elif len(keep) == 1:
            note = f"The candidate is B2 + {keep[0]}, which has already run."
        else:
            note = "ALL beats B2: the candidate is ALL without " + ", ".join(dropped) + "; stage 3 would run it."
        return {"switches": keep, "dropped": dropped, "note": note, "rule": "ALL beats B2"}
    keep = [f for f in switches if effects[f][0] > 0]
    return {"switches": keep, "dropped": [f for f in switches if f not in keep],
            "note": "ALL does not beat B2: the candidate would be B2 plus the switches with a positive one-on effect; "
                    "doc 15 says the team discusses before the frozen run.", "rule": "ALL does not beat B2"}


def screen(runs: list[dict], base: str = DEFAULT_BASE, all_label: str = DEFAULT_ALL, metric: str = "e2e_pass",
           switches: list[str] | None = None, expected: set[tuple] | None = None, excluded_problems: set[str] | None = None,
           exclude_commits: set[str] | None = None) -> dict:
    """The report for `runs` (already limited to one experiment round and with any share reset applied).

    expected          the (problem, repeat) pairs every configuration should have (from the queue); the pairs seen when omitted.
    excluded_problems problems left out of every number (the ones the null agent passes: their check passes without a fix).
    exclude_commits   agent/Lab commits whose runs are set aside (older agent code under the same experiment name)."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    excluded_problems = set(excluded_problems or ())
    exclude_commits = set(exclude_commits or ())
    warnings: list[str] = []
    kept, set_aside_commit = [], 0
    for r in runs:
        if (r.get("problem_id") or "") in excluded_problems:
            continue
        if exclude_commits and (r.get("git_commit") or "") in exclude_commits:
            set_aside_commit += 1
            continue
        kept.append(r)
    if set_aside_commit:
        warnings.append(f"{set_aside_commit} runs from commits {', '.join(sorted(exclude_commits))} were set aside.")

    by_label: dict[str, list[dict]] = defaultdict(list)
    for r in kept:
        by_label[r.get("config_label") or "-"].append(r)
    labels = set(by_label)
    if switches is None:
        switches = detect_switches(labels, base, all_label)
    pairs, attempted = {}, {}
    for lab, rs in by_label.items():
        pairs[lab], attempted[lab] = _pairs(rs, metric)

    universe = set(expected) if expected else set().union(*[set(a) for a in attempted.values()]) if attempted else set()
    if excluded_problems:
        universe = {p for p in universe if p[0] not in excluded_problems}
    repeats = max([p[1] for p in universe] or [1])
    problems_all = sorted({p[0] for p in universe})

    design_labels = [base] + [f"{base} +{f}" for f in switches] + [all_label] + [f"{all_label} -{f}" for f in switches]
    shown = [l for l in design_labels if l in by_label] + sorted(l for l in by_label if l not in design_labels)
    configs = []
    mixed_commits: list[str] = []
    mixed_versions: list[str] = []
    for lab in shown:
        known = pairs[lab]
        n = len(known)
        k = sum(1 for v in known.values() if v)
        missing = len([p for p in universe if p not in known]) if universe else 0
        per_problem: dict[str, list] = defaultdict(list)
        for (prob, rep), v in known.items():
            per_problem[prob].append(v)
        full = [prob for prob, vs in per_problem.items() if len(vs) >= repeats]
        pass_all = [prob for prob in full if all(per_problem[prob])]
        total = n + missing
        who = sorted({(r.get("run_by") or "?") for r in by_label[lab]})
        commits = sorted({(r.get("git_commit") or "?")[:8] for r in by_label[lab]})
        versions = sorted({(r.get("machine_versions") or "") for r in by_label[lab]} - {""})
        keys = {(r.get("agent_id"), r.get("model_id"), r.get("action_space"), r.get("judge_model_id")) for r in by_label[lab]}
        if len(keys) > 1:
            warnings.append(f"{lab}: runs with more than one agent/model/action-space/judge combination ({len(keys)}); they are pooled here.")
        w = wilson(k, n)
        configs.append({"label": lab, "in_design": lab in design_labels, "cells": n, "passes": k,
                        "rate": (k / n) if n else None, "wilson": list(w) if w else None,
                        "missing": missing, "worst_low": (k / total) if total else None, "worst_high": ((k + missing) / total) if total else None,
                        "problems_full": len(full), "pass_all": len(pass_all), "repeats": repeats,
                        "people": who, "commits": commits, "versions": versions, "attempts": len(by_label[lab])})
        if len(commits) > 1:
            mixed_commits.append(lab)
        if len(versions) > 1:
            mixed_versions.append(lab)

    all_commits = sorted({(r.get("git_commit") or "?")[:8] for r in kept})
    if mixed_commits:
        warnings.append(f"{len(mixed_commits)} configurations have runs from more than one commit (all commits: {', '.join(all_commits)}). "
                        "Check that the agent code was the same, or set the older commits aside.")
    all_versions = sorted({(r.get("machine_versions") or "") for r in kept} - {""})
    if mixed_versions:
        warnings.append(f"{len(mixed_versions)} configurations have runs made with different tool versions ({' | '.join(all_versions)}).")
    contrasts = []
    for f in switches:
        contrasts.append(_contrast(f"{f}:on", f, "on", f"{base} +{f}", base, pairs))
        contrasts.append(_contrast(f"{f}:off", f, "off", all_label, f"{all_label} -{f}", pairs))
    # The family is fixed at all the contrasts of the design (16 for eight switches), as written before the data: a contrast that
    # cannot be tested yet counts as p = 1 instead of shrinking the family and making the correction milder.
    adjusted = holm([c["p"] if c["p"] is not None else 1.0 for c in contrasts])
    for c, a in zip(contrasts, adjusted):
        if c["p"] is not None:
            c["p_holm"] = a
            c["passes_holm"] = a < ALPHA
    all_vs_base = _contrast("ALL:base", "ALL", "all", all_label, base, pairs)     # outside the family of 16: reported, not corrected
    by_switch = {f: {"on": next(c for c in contrasts if c["id"] == f"{f}:on"), "off": next(c for c in contrasts if c["id"] == f"{f}:off")} for f in switches}
    verdicts = []
    for f in switches:
        v, why = _verdict(by_switch[f]["on"], by_switch[f]["off"])
        verdicts.append({"switch": f, "verdict": v, "why": why})
    discordant = sum(c["treated_only"] + c["control_only"] for c in contrasts)
    if contrasts and discordant == 0:
        warnings.append("No contrast has a flipped pair yet: the contrasts need configurations run on the same (problem, repeat) pairs.")
    n_pairs = len(universe)
    in_design_cfgs = [c for c in configs if c["in_design"]]
    if n_pairs and in_design_cfgs:
        have = sum(c["cells"] for c in in_design_cfgs)
        want = n_pairs * len(in_design_cfgs)
        if have < 0.8 * want:
            warnings.append(f"Only {have} of {want} cells ({100 * have // want}%) have a clean result so far. A contrast uses only the pairs both of its "
                            "configurations have, so most contrasts rest on few pairs and cannot pass yet.")
    candidate = _candidate(base, all_label, switches, by_switch, all_vs_base) if switches else None
    if candidate is not None:
        candidate["evidence"] = any(v["verdict"].startswith(("finding", "candidate")) for v in verdicts)
        if not candidate["evidence"]:
            candidate["note"] += " No switch has passed the sign test, so this only describes the current signs; it is not evidence."
    return {"metric": metric, "base": base, "all": all_label, "switches": switches, "pairs_expected": n_pairs, "repeats": repeats,
            "problems": problems_all, "excluded_problems": sorted(excluded_problems), "configs": configs, "contrasts": contrasts,
            "all_vs_base": all_vs_base, "verdicts": verdicts, "candidate": candidate,
            "warnings": warnings,
            "rule": ("A switch helps only if one of its two contrasts passes the exact two-sided sign test on the pairs that flipped and its one-on "
                     "and one-off effects point the same way. Without correction that is p < 0.05 (the smallest case is 6 flipped pairs, all one way); "
                     "with Holm over the 16 contrasts it is the adjusted p < 0.05 (the smallest case is 10, all one way). Passing without correction only "
                     "is a candidate, not a finding. Cells without a clean result are missing, never failures. This is a screening on dev data; "
                     "gains are reported only from the frozen 30.")}


def configs_csv(report: dict) -> str:
    cols = ["label", "cells", "passes", "rate", "wilson_lo", "wilson_hi", "missing", "worst_low", "worst_high", "problems_full", "pass_all", "repeats", "people", "commits", "versions"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for c in report["configs"]:
        w.writerow({**{k: ("" if c.get(k) is None else c.get(k)) for k in cols if k not in ("wilson_lo", "wilson_hi", "people", "commits", "versions")},
                    "wilson_lo": round(c["wilson"][0], 4) if c.get("wilson") else "", "wilson_hi": round(c["wilson"][1], 4) if c.get("wilson") else "",
                    "people": "; ".join(c["people"]), "commits": "; ".join(c["commits"]), "versions": " | ".join(c["versions"])})
    return buf.getvalue()


def contrasts_csv(report: dict) -> str:
    cols = ["id", "switch", "kind", "treated", "control", "pairs", "treated_only", "control_only", "both", "neither", "diff", "direction", "p", "p_holm", "passes_raw", "passes_holm"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    for c in report["contrasts"] + [report["all_vs_base"]]:
        w.writerow({k: ("" if c.get(k) is None else (round(c[k], 6) if isinstance(c[k], float) else c[k])) for k in cols})
    return buf.getvalue()


def to_markdown(report: dict, title: str = "Screening report") -> str:
    pct = lambda x: "-" if x is None else f"{100 * x:.0f}%"      # noqa: E731
    out = [f"# {title}", "",
           f"Metric `{report['metric']}`; base `{report['base']}`; all `{report['all']}`; {len(report['switches'])} switches; "
           f"{report['pairs_expected']} (problem, repeat) pairs expected per configuration ({len(report['problems'])} problems x {report['repeats']} repeats).", "",
           "## Configurations", "", "| Configuration | Cells | Pass | Rate | 95% Wilson | Missing | Worst case | Problems all repeats pass |", "|---|---|---|---|---|---|---|---|"]
    for c in report["configs"]:
        w = f"{pct(c['wilson'][0])} to {pct(c['wilson'][1])}" if c["wilson"] else "-"
        worst = f"{pct(c['worst_low'])} to {pct(c['worst_high'])}" if c["missing"] else "-"
        out.append(f"| {c['label']} | {c['cells']} | {c['passes']} | {pct(c['rate'])} | {w} | {c['missing']} | {worst} | {c['pass_all']} of {c['problems_full']} |")
    out += ["", "## Contrasts (exact two-sided sign test on the pairs that flipped)", "",
            "| Contrast | Treated | Control | Pairs | Only treated | Only control | p | p Holm (16) |", "|---|---|---|---|---|---|---|---|"]
    fmt = lambda p: "-" if p is None else f"{p:.4f}"      # noqa: E731
    for c in report["contrasts"] + [report["all_vs_base"]]:
        out.append(f"| {c['id']} | {c['treated']} | {c['control']} | {c['pairs']} | {c['treated_only']} | {c['control_only']} | {fmt(c['p'])} | "
                   f"{fmt(c['p_holm']) if c['kind'] != 'all' else 'outside the 16'} |")
    out += ["", "## Verdict per switch", "", "| Switch | Verdict | Why |", "|---|---|---|"]
    out += [f"| {v['switch']} | {v['verdict']} | {v['why']} |" for v in report["verdicts"]]
    cand = report.get("candidate")
    if cand:
        out += ["", "## Candidate by the doc 15 rule", "", cand["note"] + (" Switches: " + ", ".join(cand["switches"]) + "." if cand.get("switches") else "")]
    if report["warnings"]:
        out += ["", "## Warnings", ""] + [f"- {w}" for w in report["warnings"]]
    out += ["", "## Rule", "", report["rule"], ""]
    return "\n".join(out)
