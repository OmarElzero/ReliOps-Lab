"""Assigned judge audit: a frozen, stratified, blind sample of judged diagnoses, split across named labelers.

The plan stores run ids, strata and who labels what. The labeler endpoints never return the run's
configuration, the judge's verdict or another labeler's answer; only the owner-gated score does."""

from __future__ import annotations

import itertools
import random
import re
from collections import defaultdict

import audit
import stats

FLOOR_RE = re.compile(r"(dumb|do-?nothing|floor)", re.I)
DEFAULT_GROUPS = "R3 | split | 12\nR2 | pooled | 12"


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", (name or "").strip()).strip("-").lower()[:60]


def eligible(runs: list[dict]) -> list[dict]:
    """Clean runs only: a stored diagnosis, a judge verdict, not the floor baseline or a no-fault control."""
    out = [r for r in runs
           if (r.get("diagnosis_text") or "").strip() and isinstance(r.get("diagnosis_pass"), bool)
           and r.get("judge_model_id") and r.get("run_id")
           and not FLOOR_RE.search(r.get("config_label") or "") and not audit.is_control(r.get("problem_id"))]
    return sorted(out, key=lambda r: r["run_id"])


def parse_groups(text: str) -> list[dict]:
    """One line per group: `prefix | split or pooled | runs`. `split` makes one stratum per configuration
    whose label starts with the prefix; `pooled` makes one stratum from all of them."""
    groups = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3 or parts[1] not in ("split", "pooled") or not parts[2].isdigit() or not parts[0]:
            raise ValueError(f"Bad line {line!r}: write `prefix | split or pooled | number of runs`.")
        groups.append({"prefix": parts[0], "split": parts[1] == "split", "n": int(parts[2])})
    if not groups:
        raise ValueError("Give at least one group.")
    return groups


def _pick(rows, k, rng, cap, used):
    pool = rows[:]
    rng.shuffle(pool)
    chosen = []
    for relax in (False, True):
        for r in pool:
            if len(chosen) == k:
                return chosen
            if any(r is c for c in chosen) or (not relax and used[r["problem_id"]] >= cap):
                continue
            chosen.append(r)
            used[r["problem_id"]] += 1
    return chosen


def _draw_stratum(rows, n, rng, cap):
    """n runs, half the judge passed and half it failed, so a lenient or a harsh judge cannot hide."""
    used = defaultdict(int)
    passed = [r for r in rows if r["diagnosis_pass"]]
    failed = [r for r in rows if not r["diagnosis_pass"]]
    chosen = _pick(passed, n // 2, rng, cap, used) + _pick(failed, n - n // 2, rng, cap, used)
    if len(chosen) < n:
        rest = [r for r in rows if not any(r is c for c in chosen)]
        chosen += _pick(rest, n - len(chosen), rng, cap, used)
    return chosen


def draw(runs: list[dict], groups: list[dict], labelers: list[str], seed: int, anchors_per_stratum: int = 3,
         max_per_problem: int = 2) -> tuple[list[dict], list[str]]:
    """Returns (items, notes). Each item: item_id, run_id, stratum, anchor, labelers."""
    labelers = [x for x in dict.fromkeys(l.strip() for l in labelers) if x]
    if not labelers:
        raise ValueError("Name at least one labeler.")
    rng = random.Random(seed)
    pool = eligible(runs)
    strata: list[tuple[str, list[dict], int]] = []
    for g in groups:
        sel = [r for r in pool if (r.get("config_label") or "").startswith(g["prefix"])]
        if g["split"]:
            for c in sorted({r["config_label"] for r in sel}):
                strata.append((c, [r for r in sel if r["config_label"] == c], g["n"]))
        else:
            strata.append((g["prefix"].strip() + " pooled", sel, g["n"]))
    notes, items = [], []
    for name, rows, n in strata:
        if not rows:
            notes.append(f"{name}: no eligible runs, skipped")
            continue
        got = _draw_stratum(rows, min(n, len(rows)), rng, max_per_problem)
        npass = sum(1 for r in got if r["diagnosis_pass"])
        notes.append(f"{name}: {len(got)} drawn from {len(rows)} eligible ({npass} judged pass, {len(got) - npass} judged fail)")
        rng.shuffle(got)
        for i, r in enumerate(got):
            items.append({"run_id": r["run_id"], "stratum": name, "anchor": i < anchors_per_stratum})
    ids = list(range(1, len(items) + 1))
    rng.shuffle(ids)
    rr = itertools.cycle(range(len(labelers)))
    for it, i in zip(items, ids):
        it["item_id"] = f"I{i:03d}"
        it["labelers"] = list(labelers) if it["anchor"] else [labelers[next(rr)]]
    items.sort(key=lambda x: x["item_id"])
    return items, notes


def validate_label(body: dict, item: dict) -> tuple[dict | None, str | None]:
    labeler = (body.get("labeler") or "").strip()
    if not any(labeler.casefold() == l.casefold() for l in item["labelers"]):
        return None, "This item is not assigned to you."
    canon = next(l for l in item["labelers"] if labeler.casefold() == l.casefold())
    for k in ("d1", "d2", "d3"):
        if not isinstance(body.get(k), bool):
            return None, f"Answer {k.upper()} with Yes or No."
    conf = body.get("confidence")
    if conf not in (1, 2, 3):
        return None, "Confidence must be 1, 2 or 3."
    overall = bool(body["d1"] and body["d2"])
    reason = (body.get("reason") or "").strip()
    if (not overall or conf == 1) and not reason:
        return None, "Write one sentence of reason when the answer is Incorrect or you are unsure."
    return {"labeler": canon, "d1": body["d1"], "d2": body["d2"], "d3": body["d3"], "overall_correct": overall,
            "confidence": conf, "reason": reason, "used_story": bool(body.get("used_story"))}, None


def progress(plan: dict, labels: list[dict]) -> list[dict]:
    done = {(l["item_id"], l["labeler"]) for l in labels}
    out = []
    for who in plan["labelers"]:
        mine = [i for i in plan["items"] if who in i["labelers"]]
        out.append({"labeler": who, "total": len(mine), "done": sum(1 for i in mine if (i["item_id"], who) in done)})
    return out


def score(plan: dict, runs: list[dict], labels: list[dict]) -> dict:
    by_run = {r["run_id"]: r for r in runs}
    byitem: dict[str, dict[str, bool]] = defaultdict(dict)
    for l in labels:
        byitem[l["item_id"]][l["labeler"]] = bool(l["overall_correct"])
    anchors = [i for i in plan["items"] if i["anchor"]]
    pairs = []
    for x, y in itertools.combinations(plan["labelers"], 2):
        ids = [i["item_id"] for i in anchors if x in byitem[i["item_id"]] and y in byitem[i["item_id"]]]
        if ids:
            A = [byitem[i][x] for i in ids]
            B = [byitem[i][y] for i in ids]
            pairs.append({"a": x, "b": y, "n": len(ids), "agree": sum(p == q for p, q in zip(A, B)), "kappa": stats.cohen_kappa(A, B)})
    groups: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    disagreements, ties = [], []
    for it in plan["items"]:
        votes = list(byitem[it["item_id"]].values())
        if not votes:
            continue
        if len(votes) % 2 == 0 and sum(votes) * 2 == len(votes):
            ties.append(it["item_id"])
            continue
        human = sum(votes) * 2 > len(votes)
        run = by_run.get(it["run_id"]) or {}
        judge = run.get("diagnosis_pass")
        if not isinstance(judge, bool):
            continue
        groups["ALL"].append((judge, human))
        groups[it["stratum"]].append((judge, human))
        if judge != human:
            disagreements.append({"item_id": it["item_id"], "run_id": it["run_id"], "stratum": it["stratum"],
                                  "problem_id": run.get("problem_id"), "judge": judge, "human": human})
    rows = []
    for g, ps in groups.items():
        J, H = [p[0] for p in ps], [p[1] for p in ps]
        n, ag = len(ps), sum(p == q for p, q in ps)
        rows.append({"group": g, "n": n, "agree": ag, "wilson": stats.wilson(ag, n), "kappa": stats.cohen_kappa(J, H),
                     "fp": sum(1 for p, q in ps if p and not q), "judge_pass": sum(J),
                     "fn": sum(1 for p, q in ps if not p and q), "judge_fail": n - sum(J)})
    rows.sort(key=lambda r: (r["group"] != "ALL", r["group"]))
    return {"progress": progress(plan, labels), "labeler_pairs": pairs, "judge_vs_human": rows,
            "disagreements": disagreements, "ties": ties,
            "story_used": sum(1 for l in labels if l.get("used_story")), "labels": len(labels),
            "note": "Half of every stratum is judge-pass and half judge-fail, so read the two error counts, not only the total."}
