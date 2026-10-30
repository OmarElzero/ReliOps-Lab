"""Results dashboard: filter the recorded runs by anything, and compute from exactly that subset the
summary, the category and problem breakdowns, failure tags, a timeline, and the provenance (what the
numbers are based on). Also renders the same view as a Markdown report for a paper's results section."""

from __future__ import annotations

import time
from collections import Counter, defaultdict

import audit
import campaign
import stats

PURPOSES = ("experiment", "dev", "final")
PURPOSE_HELP = {
    "experiment": "trying things; read for learning, never reported",
    "dev": "development set; failures may be read to choose agent changes",
    "final": "frozen set of the current phase; reported, never used to choose changes",
}
# filter name -> run field. Values are compared as strings; empty or missing is "-".
FIELDS = {
    "agent_id": "agent_id", "model_id": "model_id", "action_space": "action_space", "config_label": "config_label",
    "judge_model_id": "judge_model_id", "run_class": "run_class", "purpose": "purpose", "category": "category",
    "app": "app", "problem_id": "problem_id", "run_by": "run_by", "manifest_id": "manifest_id",
}
RUN_LIMIT = 3000


def purpose_of(run: dict, mark: dict | None, roles: dict[str, str]) -> tuple[str, str]:
    """(purpose, source). A person's mark always wins. Otherwise: a problem in the dev set is dev; a frozen-set
    problem run as part of a frozen campaign is final; everything else is an experiment (a run of a frozen
    problem outside a campaign, like the pilot, is not reportable)."""
    if mark and mark.get("purpose") in PURPOSES:
        return mark["purpose"], "marked"
    role = roles.get(run.get("problem_id") or "")
    if role == "dev":
        return "dev", "auto"
    if role == "frozen" and run.get("run_class") == "frozen":
        return "final", "auto"
    return "experiment", "auto"


def _val(run: dict, field: str) -> str:
    v = run.get(field)
    return "-" if v is None or v == "" else str(v)


def outcome(run: dict, metric: str) -> str:
    v = run.get(metric)
    return "incomplete" if not isinstance(v, bool) else ("pass" if v else "fail")


def _tag_value(run: dict, metric: str) -> str:
    if outcome(run, metric) != "fail":
        return "-"
    return (run.get("failure_mode_tag") or "").strip() or "(untagged)"


def apply_filters(runs: list[dict], f: dict, metric: str = "e2e_pass") -> list[dict]:
    f = f or {}
    wanted = {k: set(map(str, f[k])) for k in list(FIELDS) + ["outcome", "failure_tag"] if f.get(k)}
    q = (f.get("q") or "").strip().lower()
    d0, d1 = (f.get("date_from") or ""), (f.get("date_to") or "")
    out = []
    for r in runs:
        ok = True
        for k, vals in wanted.items():
            if k == "outcome":
                got = outcome(r, metric)
            elif k == "failure_tag":
                got = _tag_value(r, metric)
            else:
                got = _val(r, FIELDS[k])
            if got not in vals:
                ok = False
                break
        if not ok:
            continue
        day = (r.get("timestamp_utc") or "")[:10]
        if (d0 and day < d0) or (d1 and day > d1):
            continue
        if q:
            hay = " ".join(str(r.get(x) or "") for x in ("problem_id", "config_label", "run_by", "review_note",
                                                        "diagnosis_text", "failure_mode_tag", "model_id")).lower()
            if q not in hay:
                continue
        out.append(r)
    return out


def options(runs: list[dict], metric: str) -> dict:
    """Distinct values with counts for every filter, over all runs (so the choices do not vanish as you filter)."""
    out = {}
    for name, field in FIELDS.items():
        c = Counter(_val(r, field) for r in runs)
        out[name] = [{"value": v, "n": n} for v, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]
    out["outcome"] = [{"value": v, "n": n} for v, n in Counter(outcome(r, metric) for r in runs).most_common()]
    tags = Counter(_tag_value(r, metric) for r in runs if _tag_value(r, metric) != "-")
    out["failure_tag"] = [{"value": v, "n": n} for v, n in tags.most_common()]
    return out


def _cell(rs: list[dict], metric: str) -> dict:
    known = [r.get(metric) for r in rs if isinstance(r.get(metric), bool)]
    return {"pass": sum(known), "n": len(known), "incomplete": len(rs) - len(known)}


def build(all_runs: list[dict], f: dict, metric: str, manifests: list[dict], labels: list[dict],
          rejudges: list[dict], source: str) -> dict:
    runs = apply_filters(all_runs, f, metric)
    groups = stats.summarize(runs, metric)
    keys = [g["key"] for g in groups]

    # problem x configuration matrix, and the same rolled up by category
    by_problem: dict[str, dict] = {}
    for r in runs:
        p = r.get("problem_id") or "?"
        e = by_problem.setdefault(p, {"problem": p, "category": r.get("category") or "Uncategorized", "app": r.get("app") or "",
                                      "purposes": set(), "runs": defaultdict(list)})
        e["purposes"].add(r.get("purpose"))
        e["runs"][stats.config_key(r)].append(r)
    matrix = []
    for p, e in sorted(by_problem.items(), key=lambda kv: (kv[1]["category"], kv[0])):
        matrix.append({"problem": p, "category": e["category"], "app": e["app"], "purposes": sorted(e["purposes"]),
                       "cells": {k: _cell(e["runs"][k], metric) for k in keys if k in e["runs"]}})
    cats: dict[str, dict] = {}
    for row in matrix:
        c = cats.setdefault(row["category"], {"category": row["category"], "problems": 0, "cells": {}})
        c["problems"] += 1
        for k, cell in row["cells"].items():
            agg = c["cells"].setdefault(k, {"pass": 0, "n": 0, "incomplete": 0})
            for x in ("pass", "n", "incomplete"):
                agg[x] += cell[x]
    categories = sorted(cats.values(), key=lambda c: c["category"])

    # failure tags overall
    tags = Counter(_tag_value(r, metric) for r in runs if outcome(r, metric) == "fail")

    # timeline
    tl: dict[str, Counter] = defaultdict(Counter)
    for r in runs:
        tl[(r.get("timestamp_utc") or "")[:10] or "?"][outcome(r, metric)] += 1
    timeline = [{"day": d, **{k: c.get(k, 0) for k in ("pass", "fail", "incomplete")}} for d, c in sorted(tl.items())]

    # provenance: what the numbers in view are based on
    aud = audit.judge_audit(all_runs, labels, rejudges)
    judges_in_view = sorted({_val(r, "judge_model_id") for r in runs})
    label_ids = {l["run_id"] for l in labels if l.get("run_id")}
    man_by_id = {m.get("manifest_id"): m for m in manifests}
    man_rows = []
    for mid in sorted({r.get("manifest_id") for r in runs if r.get("manifest_id")}):
        m = man_by_id.get(mid)
        if not m:
            man_rows.append({"manifest_id": mid, "missing": True})
            continue
        g = campaign.grid(m, all_runs, metric)
        man_rows.append({"manifest_id": mid, "name": m.get("name"), "agent_id": m.get("agent_id"), "model_id": m.get("model_id"),
                         "action_space": m.get("action_space"), "judge_model_id": m.get("judge_model_id"),
                         "config_label": m.get("config_label"), "git_commit": m.get("git_commit"),
                         "problems": len(m.get("problems", [])), "problems_hash": m.get("problems_hash"),
                         "repeat_n": m.get("repeat_n"), "created_by": m.get("created_by"), "created_at": m.get("created_at"),
                         "cells_total": len(g["cells"]), "counts": g["counts"]})
    days = sorted(d for d in ((r.get("timestamp_utc") or "")[:10] for r in runs) if d)
    classes = Counter(_val(r, "run_class") for r in runs)
    purposes = Counter(_val(r, "purpose") for r in runs)
    prov = {
        "source": source, "runs": len(runs), "date_from": days[0] if days else None, "date_to": days[-1] if days else None,
        "commits": [{"commit": c, "n": n} for c, n in Counter(_val(r, "git_commit") for r in runs).most_common()],
        "judges": [{"judge": j, "audit": next((x for x in aud["judges"] if x["judge"] == j), None)} for j in judges_in_view],
        "models": sorted({_val(r, "model_id") for r in runs}), "run_by": sorted({_val(r, "run_by") for r in runs}),
        "manifests": man_rows, "hand_labelled_in_view": sum(1 for r in runs if r.get("run_id") in label_ids),
        "total_hand_labels": aud["labeled"], "purposes": dict(purposes), "run_classes": dict(classes),
    }

    notes = []
    if len([p for p in purposes if p != "-"]) > 1:
        notes.append("The runs in view mix purposes (" + ", ".join(sorted(purposes)) + "). Do not read one pass rate from them: "
                     "filter to a single purpose first.")
    if len(classes) > 1:
        notes.append("Exploration and frozen runs are mixed in this view.")
    if len(judges_in_view) > 1:
        notes.append("More than one judge graded these runs; their verdicts are not directly comparable.")
    odd = sum(1 for r in runs if r.get("purpose") == "final" and r.get("run_class") != "frozen")
    if odd:
        notes.append(f"{odd} run(s) are marked final but were not run in a frozen campaign, so they do not meet the frozen-set protocol.")
    inc = sum(1 for r in runs if outcome(r, metric) == "incomplete")
    if inc:
        notes.append(f"{inc} run(s) have no known outcome. They are shown as incomplete and are never counted as failures.")
    if any(not j["audit"] or not j["audit"]["labeled_overlap"] for j in prov["judges"]):
        notes.append("At least one judge in view has no hand-labelled runs, so its agreement with a person is unknown. "
                     "Treat diagnosis scores as provisional.")
    elif aud["labeled"] < 30:
        notes.append(f"Only {aud['labeled']} hand labels exist; about 30 are needed before the judge agreement means much.")
    if any(m.get("missing") for m in man_rows):
        notes.append("Some runs refer to a frozen campaign this machine cannot find.")
    if any(m.get("counts", {}).get("missing") or m.get("counts", {}).get("incomplete") for m in man_rows):
        notes.append("A frozen campaign in view is not complete: missing or incomplete cells remain.")

    # what each person did (the name they typed into "Your name")
    who: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        who[_val(r, "run_by")].append(r)
    people = []
    for name, rs in who.items():
        c = _cell(rs, metric)
        days = sorted(d for d in ((r.get("timestamp_utc") or "")[:10] for r in rs) if d)
        people.append({"name": name, "runs": len(rs), "pass": c["pass"], "n": c["n"], "incomplete": c["incomplete"],
                       "problems": len({r.get("problem_id") for r in rs}), "configs": len({stats.config_key(r) for r in rs}),
                       "purposes": dict(Counter(r.get("purpose") for r in rs)), "first": days[0] if days else None,
                       "last": days[-1] if days else None})
    people.sort(key=lambda p: (-p["runs"], p["name"]))

    trim = sorted(runs, key=lambda r: r.get("timestamp_utc") or "", reverse=True)[:RUN_LIMIT]
    cols = ("run_id", "timestamp_utc", "problem_id", "category", "app", "purpose", "purpose_source", "agent_id", "model_id",
            "action_space", "config_label", "judge_model_id", "run_class", "repeat_index", "e2e_pass", "diagnosis_pass",
            "mitigation_pass", "ttd_s", "ttm_s", "failure_mode_tag", "first_bad_step", "review_note", "run_by", "manifest_id",
            "git_commit", "key_slot", "has_story", "log_file", "total_tokens")
    rows = []
    for r in trim:
        row = {c: r.get(c) for c in cols}
        row["has_text"] = bool(r.get("diagnosis_text") or r.get("judge_reasoning"))
        row["hand_labelled"] = r.get("run_id") in label_ids
        rows.append(row)
    n_known = sum(1 for r in runs if isinstance(r.get(metric), bool))
    kpis = {"runs": len(runs), "known": n_known, "passes": sum(1 for r in runs if r.get(metric) is True),
            "incomplete": len(runs) - n_known, "problems": len(by_problem), "configs": len(groups),
            "tagged_failures": sum(1 for r in runs if outcome(r, metric) == "fail" and (r.get("failure_mode_tag") or "").strip()),
            "failures": sum(1 for r in runs if outcome(r, metric) == "fail")}
    return {"metric": metric, "filters": f or {}, "kpis": kpis, "groups": groups, "matrix": matrix, "categories": categories,
            "tags": [{"tag": t, "n": n} for t, n in tags.most_common()], "timeline": timeline, "people": people, "provenance": prov,
            "notes": notes, "runs": rows, "runs_truncated": len(runs) > RUN_LIMIT, "options": options(all_runs, metric)}


def _pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def _ci(lo, hi):
    return "—" if lo is None or hi is None else f"{lo * 100:.0f}–{hi * 100:.0f}%"


def report_markdown(d: dict) -> str:
    """The view as Markdown for a results section. Everything in it comes from the Lab's records, and it
    states what the numbers are based on."""
    p = d["provenance"]
    out = [f"# ReliOps Lab results report", "",
           f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} from {p['source']} records. Metric: `{d['metric']}`.", ""]
    flt = {k: v for k, v in (d["filters"] or {}).items() if v}
    out += ["## Filters", "", ("; ".join(f"{k} = {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in flt.items())
                               if flt else "none (every recorded run)"), ""]
    out += ["## What these numbers are based on", "",
            f"- Runs: {p['runs']} ({d['kpis']['known']} with a known outcome, {d['kpis']['incomplete']} incomplete)",
            f"- Problems: {d['kpis']['problems']}; configurations: {d['kpis']['configs']}",
            f"- Dates: {p['date_from'] or '—'} to {p['date_to'] or '—'}",
            f"- Purposes: {', '.join(f'{k} ({v})' for k, v in p['purposes'].items()) or '—'}",
            f"- Run classes: {', '.join(f'{k} ({v})' for k, v in p['run_classes'].items()) or '—'}",
            f"- Models: {', '.join(p['models']) or '—'}",
            "- Agent code commits: " + (", ".join("%s (%d runs)" % ((c["commit"] or "?")[:8], c["n"]) for c in p["commits"]) or "—"), ""]
    for j in p["judges"]:
        a = j["audit"]
        if a and a["labeled_overlap"]:
            out.append(f"- Judge `{j['judge']}`: agrees with hand labels on {a['agree']} of {a['labeled_overlap']} "
                       f"({_pct(a['agreement'])}), Cohen's kappa {a['kappa'] if a['kappa'] is None else round(a['kappa'], 2)}")
        else:
            out.append(f"- Judge `{j['judge']}`: no hand-labelled overlap yet; diagnosis scores are provisional")
    out.append("")
    if p["manifests"]:
        out += ["### Frozen campaigns in view", "", "| Campaign | Config | Commit | Judge | Problems (hash) | Repeats | Cells done |",
                "|---|---|---|---|---|---|---|"]
        for m in p["manifests"]:
            if m.get("missing"):
                out.append(f"| {m['manifest_id']} | not found on this machine | | | | | |")
                continue
            c = m["counts"]
            done = c["pass"] + c["fail"]
            out.append(f"| {m['name']} ({m['manifest_id']}) | {m['agent_id']} / {m['model_id']} / {m['action_space'] or '-'} / {m['config_label']} "
                       f"| {(m['git_commit'] or '?')[:8]} | {m['judge_model_id']} | {m['problems']} ({m['problems_hash']}) | {m['repeat_n']} "
                       f"| {done} of {m['cells_total']} ({c['incomplete']} incomplete, {c['missing']} missing) |")
        out.append("")
    if d["notes"]:
        out += ["### Caveats", ""] + [f"- {n}" for n in d["notes"]] + [""]
    out += ["## Summary per configuration", "",
            "| Configuration | Pass / n | Rate | 95% Wilson | Problem bootstrap | Problems | Incomplete |", "|---|---|---|---|---|---|---|"]
    for g in d["groups"]:
        name = f"{g['config_label']} · {g['agent_id']} · {g['model_id']} · {g['action_space']} · judge {g['judge_model_id']} · {g['run_class']}"
        out.append(f"| {name} | {g['passes']} / {g['n']} | {_pct(g['rate'])} | {_ci(g['wilson_lo'], g['wilson_hi'])} | "
                   f"{_ci(g['boot_lo'], g['boot_hi'])} | {g['problems']} | {g['incomplete']} |")
    out.append("")
    keys = [g["key"] for g in d["groups"]]
    if d["categories"] and keys:
        out += ["## By fault category (pass / n)", "", "| Category | Problems | " + " | ".join(f"config {i + 1}" for i in range(len(keys))) + " |",
                "|---|---|" + "---|" * len(keys)]
        for c in d["categories"]:
            out.append(f"| {c['category']} | {c['problems']} | " + " | ".join(
                (f"{c['cells'][k]['pass']} / {c['cells'][k]['n']}" if k in c["cells"] else "—") for k in keys) + " |")
        out += ["", *[f"config {i + 1}: {g['config_label']} · {g['agent_id']} · {g['action_space']} · {g['model_id']}" for i, g in enumerate(d["groups"])], ""]
    if d["tags"]:
        out += ["## Failure tags", "", "| Tag | Runs |", "|---|---|"] + [f"| {t['tag']} | {t['n']} |" for t in d["tags"]] + [""]
    out.append("Numbers in this report come from the Lab's records. A number that is not in the Lab export does not go in the paper.")
    return "\n".join(out) + "\n"
