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


def _sample(runs, groups, rng, cap, exclude=frozenset()):
    """Items (run_id, problem_id, stratum) for every stratum of `groups`, half judge-pass and half judge-fail each."""
    pool = [r for r in eligible(runs) if r["run_id"] not in exclude]
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
        got = _draw_stratum(rows, min(n, len(rows)), rng, cap)
        npass = sum(1 for r in got if r["diagnosis_pass"])
        notes.append(f"{name}: {len(got)} drawn from {len(rows)} eligible ({npass} judged pass, {len(got) - npass} judged fail)")
        rng.shuffle(got)
        items += [{"run_id": r["run_id"], "problem_id": r["problem_id"], "stratum": name} for r in got]
    return items, notes


def assign_by_problem(items, labelers, overlap_problems=1, load=None, pref=None):
    """Whole problems go to one labeler, so nobody has to learn every problem: biggest problems first, each to the
    labeler with the fewest items so far (`load`), or to `pref[problem]` when that person already started it.
    Overlap: each labeler also labels `overlap_problems` of the previous labeler's problems, which is what lets the
    labelers' agreement be measured. Sets item['labelers'] and item['anchor'] (anchor = labelled by two people)."""
    ld = {l: (load or {}).get(l, 0) for l in labelers}
    byp = defaultdict(list)
    for it in items:
        byp[it["problem_id"]].append(it)
    owner = {}
    for pid in sorted(byp, key=lambda p: (-len(byp[p]), p)):
        who = pref[pid] if pref and pref.get(pid) in ld else min(labelers, key=lambda l: (ld[l], labelers.index(l)))
        owner[pid] = who
        ld[who] += len(byp[pid])
        for it in byp[pid]:
            it["labelers"], it["anchor"] = [who], False
    if len(labelers) > 1 and overlap_problems > 0:
        for i, l in enumerate(labelers):
            prev = labelers[i - 1]
            mine = sorted((p for p, w in owner.items() if w == prev), key=lambda p: (-len(byp[p]), p))[:overlap_problems]
            for pid in mine:
                for it in byp[pid]:
                    it["labelers"], it["anchor"] = [prev, l], True


def _spread(items, labelers, rng, anchors_per_stratum):
    """Older rule: the first runs of each stratum go to everyone, the rest round-robin."""
    rr = itertools.cycle(range(len(labelers)))
    seen = defaultdict(int)
    for it in items:
        seen[it["stratum"]] += 1
        it["anchor"] = seen[it["stratum"]] <= anchors_per_stratum
        it["labelers"] = list(labelers) if it["anchor"] else [labelers[next(rr)]]


def _clean_labelers(labelers):
    out = [x for x in dict.fromkeys(l.strip() for l in labelers) if x]
    if not out:
        raise ValueError("Name at least one labeler.")
    return out


def draw(runs: list[dict], groups: list[dict], labelers: list[str], seed: int, anchors_per_stratum: int = 3,
         max_per_problem: int = 2, assign: str = "by_problem", overlap_problems: int = 1) -> tuple[list[dict], list[str]]:
    """Returns (items, notes). Each item: item_id, run_id, problem_id, stratum, anchor, labelers."""
    labelers = _clean_labelers(labelers)
    rng = random.Random(seed)
    items, notes = _sample(runs, groups, rng, max_per_problem)
    if assign == "spread":
        _spread(items, labelers, rng, anchors_per_stratum)
    else:
        assign_by_problem(items, labelers, overlap_problems)
    ids = list(range(1, len(items) + 1))
    rng.shuffle(ids)
    for it, i in zip(items, ids):
        it["item_id"] = f"I{i:03d}"
    items.sort(key=lambda x: x["item_id"])
    return items, notes


def _done_map(labels):
    d = defaultdict(set)
    for l in labels:
        d[l["item_id"]].add(l["labeler"])
    return d


def _loads(items, labelers):
    return {l: sum(1 for i in items if l in i["labelers"]) for l in labelers}


def redistribute(plan: dict, labels: list[dict], labelers: list[str], overlap_problems: int = 1) -> dict:
    """Re-deals the items nobody has labelled yet among `labelers`, keeping whole problems together and keeping every label
    already given. Items with a label keep their labelers (minus anyone removed who has not labelled them)."""
    labelers = _clean_labelers(labelers)
    done = _done_map(labels)
    items = plan["items"]
    fixed = [i for i in items if done.get(i["item_id"])]
    free = [i for i in items if not done.get(i["item_id"])]
    for it in fixed:
        it["labelers"] = [l for l in it["labelers"] if l in labelers or l in done[it["item_id"]]]
        it["anchor"] = len(it["labelers"]) > 1
    pref: dict[str, str] = {}
    cnt: dict[tuple, int] = defaultdict(int)
    for it in fixed:
        for l in done[it["item_id"]]:
            if l in labelers:
                cnt[(it["problem_id"], l)] += 1
    for (pid, l), n in sorted(cnt.items(), key=lambda kv: -kv[1]):
        pref.setdefault(pid, l)
    assign_by_problem(free, labelers, overlap_problems, _loads(fixed, labelers), pref)
    plan["labelers"] = labelers
    return {"re_dealt": len(free), "kept": len(fixed)}


def extend(plan: dict, runs: list[dict], labels: list[dict], groups: list[dict], seed: int, max_per_problem: int = 2,
           overlap_problems: int = 0) -> list[str]:
    """Adds more runs by the same rules (none already in the sample); a problem that already has an owner stays with them."""
    rng = random.Random(seed)
    have = {i["run_id"] for i in plan["items"]}
    new, notes = _sample(runs, groups, rng, max_per_problem, exclude=have)
    labelers = plan["labelers"]
    pref: dict[str, str] = {}
    byp = defaultdict(lambda: defaultdict(int))
    for it in plan["items"]:
        if it["labelers"]:
            byp[it["problem_id"]][it["labelers"][0]] += 1
    for pid, c in byp.items():
        who = max(c, key=lambda l: c[l])
        if who in labelers:
            pref[pid] = who
    assign_by_problem(new, labelers, overlap_problems, _loads(plan["items"], labelers), pref)
    top = max((int(i["item_id"][1:]) for i in plan["items"]), default=0)
    ids = list(range(top + 1, top + len(new) + 1))
    rng.shuffle(ids)
    for it, i in zip(new, ids):
        it["item_id"] = f"I{i:03d}"
    plan["items"] = sorted(plan["items"] + new, key=lambda x: x["item_id"])
    return notes + [f"added {len(new)} runs"]


def remove_items(plan: dict, labels: list[dict], item_ids: list[str]) -> tuple[list[str], list[str]]:
    """Drops items nobody has labelled. Returns (removed, refused)."""
    done = _done_map(labels)
    want = set(item_ids)
    removed = [i["item_id"] for i in plan["items"] if i["item_id"] in want and not done.get(i["item_id"])]
    refused = sorted(want - set(removed))
    plan["items"] = [i for i in plan["items"] if i["item_id"] not in set(removed)]
    return removed, refused


def overview(plan: dict, labels: list[dict]) -> dict:
    """Owner's view: per labeler, the problems and items they hold and how many are done."""
    done = _done_map(labels)
    out = []
    for who in plan["labelers"]:
        mine = [i for i in plan["items"] if who in i["labelers"]]
        probs: dict[str, dict] = defaultdict(lambda: {"items": 0, "done": 0, "overlap": 0})
        for i in mine:
            p = probs[i["problem_id"]]
            p["items"] += 1
            p["done"] += 1 if who in done.get(i["item_id"], ()) else 0
            p["overlap"] += 1 if i["anchor"] else 0
        out.append({"labeler": who, "items": len(mine), "done": sum(p["done"] for p in probs.values()),
                    "problems": [{"problem_id": k, **v} for k, v in sorted(probs.items())]})
    unlabeled = [i["item_id"] for i in plan["items"] if not done.get(i["item_id"])]
    return {"labelers": out, "items": len(plan["items"]), "unlabeled": unlabeled, "history": plan.get("history", [])}


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
    names = sorted({l["labeler"] for l in labels} | set(plan["labelers"]))
    pairs = []
    for x, y in itertools.combinations(names, 2):
        ids = [i["item_id"] for i in plan["items"] if x in byitem[i["item_id"]] and y in byitem[i["item_id"]]]
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
            ties.append(it["item_id"])  # the labelers split: left out of judge-versus-human, listed so the owner can settle it
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
