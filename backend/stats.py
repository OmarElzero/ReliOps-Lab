"""Campaign statistics for ReliOps Lab: the numbers docs/plan/03-testing-plan.md section 6 requires
for every reported condition, so nobody has to compute them by hand or in a notebook.

  * Wilson 95% interval for a pass rate (stays accurate at small n, unlike mean +/- sd).
  * Problem-level bootstrap interval (resample problems, not runs, so a problem with 3 repeats is not
    counted as 3 independent problems).
  * Exact McNemar test for paired comparisons: same problem, same repeat index, two configs; only
    the pairs where the outcome flipped carry information.

Pure standard library, no numpy/scipy, so the Lab keeps its small requirements.txt.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Iterable

METRICS = ("e2e_pass", "diagnosis_pass", "mitigation_pass")
KEY_FIELDS = ("agent_id", "model_id", "action_space", "config_label", "judge_model_id", "run_class")
KEY_SEP = "||"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for k successes in n trials. None when n == 0."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value. b = pairs where only A passed, c = only B passed.
    Under the null the b+c discordant pairs split 50/50, so this is a two-sided binomial test."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def bootstrap_problem_ci(
    per_problem: dict[str, list[bool]], iters: int = 2000, seed: int = 0, alpha: float = 0.05
) -> tuple[float, float] | None:
    """Percentile bootstrap over problems of the mean per-problem pass rate. Deterministic (seeded)
    so the same data always shows the same interval. None when there are fewer than 2 problems."""
    rates = [sum(v) / len(v) for v in per_problem.values() if v]
    if len(rates) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        sample = [rates[rng.randrange(len(rates))] for _ in rates]
        means.append(sum(sample) / len(sample))
    means.sort()
    lo = means[int((alpha / 2) * iters)]
    hi = means[min(iters - 1, int((1 - alpha / 2) * iters))]
    return lo, hi


def cohen_kappa(a: list[bool], b: list[bool]) -> float | None:
    """Cohen's kappa for two binary raters over the same items. None if there are no items, or if
    chance agreement is 1 (both raters constant and identical), where kappa is undefined."""
    n = len(a)
    if n == 0 or n != len(b):
        return None
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe >= 1.0:
        return None
    return (po - pe) / (1 - pe)


def config_key(run: dict) -> str:
    return KEY_SEP.join(str(run.get(f) or "-") for f in KEY_FIELDS)


def key_to_dict(key: str) -> dict:
    return dict(zip(KEY_FIELDS, key.split(KEY_SEP)))


def _known(run: dict, metric: str) -> bool | None:
    v = run.get(metric)
    return v if isinstance(v, bool) else None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def cost_summary(rs: list[dict], passes: int) -> dict:
    """Cost per configuration from whatever runs recorded it (reliops_v1 transcripts). Averages use
    only the runs that have the number; `tokens_per_resolved` spends ALL measured tokens (failures
    included) over the number of passes, which is the cost of getting one fix, not one attempt."""
    toks = [t for t in (_num(r.get("total_tokens")) for r in rs) if t is not None]
    calls = [c for c in (_num(r.get("num_llm_calls")) for r in rs) if c is not None]
    times = [x for x in ((_num(r.get("ttd_s")) or 0) + (_num(r.get("ttm_s")) or 0) for r in rs
                         if _num(r.get("ttd_s")) is not None or _num(r.get("ttm_s")) is not None)]
    return {
        "cost_runs": len(toks),
        "mean_tokens": (sum(toks) / len(toks)) if toks else None,
        "mean_llm_calls": (sum(calls) / len(calls)) if calls else None,
        "mean_time_s": (sum(times) / len(times)) if times else None,
        "tokens_per_resolved": (sum(toks) / passes) if toks and passes else None,
    }


def summarize(runs: Iterable[dict], metric: str = "e2e_pass") -> list[dict]:
    """One row per config (agent, model, action space, config label, judge, run class).

    Denominator = runs where this metric is known. Runs that crashed or never reached the end
    (metric None) are counted in `incomplete`, never silently treated as failures or dropped."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        groups[config_key(r)].append(r)

    out = []
    for key, rs in groups.items():
        outcomes = [(r, _known(r, metric)) for r in rs]
        known = [(r, v) for r, v in outcomes if v is not None]
        k = sum(1 for _, v in known if v)
        n = len(known)
        per_problem: dict[str, list[bool]] = defaultdict(list)
        for r, v in known:
            per_problem[r.get("problem_id") or "?"].append(v)
        ci = wilson(k, n)
        bci = bootstrap_problem_ci(per_problem)
        # Failure tags only count runs known to have failed, so "untagged" shows how much reading is left.
        tags: dict[str, int] = defaultdict(int)
        untagged = 0
        for r, v in known:
            if v is False:
                t = (r.get("failure_mode_tag") or "").strip()
                if t:
                    tags[t] += 1
                else:
                    untagged += 1
        cost = cost_summary(rs, k)
        out.append({
            "key": key,
            **key_to_dict(key),
            "metric": metric,
            "runs": len(rs),
            "n": n,
            "passes": k,
            "incomplete": len(rs) - n,
            "rate": (k / n) if n else None,
            "wilson_lo": ci[0] if ci else None,
            "wilson_hi": ci[1] if ci else None,
            "problems": len(per_problem),
            "boot_lo": bci[0] if bci else None,
            "boot_hi": bci[1] if bci else None,
            "failure_tags": dict(tags),
            "failures_untagged": untagged,
            **cost,
        })
    out.sort(key=lambda g: (-(g["n"]), g["key"]))
    return out


def _latest_per_pair(runs: Iterable[dict], metric: str) -> dict[tuple[str, int], bool]:
    """(problem, repeat_index) -> outcome, keeping the most recent run when a pair was run twice
    (a re-run after a crash replaces the crashed attempt). Unknown outcomes are skipped."""
    best: dict[tuple[str, int], tuple[str, bool]] = {}
    for r in runs:
        v = _known(r, metric)
        if v is None:
            continue
        pair = (r.get("problem_id") or "?", int(r.get("repeat_index") or 1))
        ts = r.get("timestamp_utc") or ""
        if pair not in best or ts >= best[pair][0]:
            best[pair] = (ts, v)
    return {p: v for p, (_, v) in best.items()}


def compare(runs: list[dict], key_a: str, key_b: str, metric: str = "e2e_pass") -> dict:
    """Paired comparison of two configs on the (problem, repeat) pairs both have an outcome for."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    a = _latest_per_pair([r for r in runs if config_key(r) == key_a], metric)
    b = _latest_per_pair([r for r in runs if config_key(r) == key_b], metric)
    shared = sorted(set(a) & set(b))
    only_a = sum(1 for p in shared if a[p] and not b[p])
    only_b = sum(1 for p in shared if b[p] and not a[p])
    both = sum(1 for p in shared if a[p] and b[p])
    neither = sum(1 for p in shared if not a[p] and not b[p])

    by_problem: dict[str, dict] = {}
    for prob, rep in shared:
        row = by_problem.setdefault(prob, {"problem": prob, "pairs": 0, "a_pass": 0, "b_pass": 0})
        row["pairs"] += 1
        row["a_pass"] += int(a[(prob, rep)])
        row["b_pass"] += int(b[(prob, rep)])

    n = len(shared)
    ca = wilson(sum(a[p] for p in shared), n)
    cb = wilson(sum(b[p] for p in shared), n)
    warnings = []
    if n == 0:
        warnings.append("No (problem, repeat) pairs in common: run both configs on the same problems with the same repeat count.")
    elif only_a + only_b < 10:
        warnings.append(f"Only {only_a + only_b} pairs differ; the test has little power. Add problems or repeats before concluding.")
    ka, kb = key_to_dict(key_a), key_to_dict(key_b)
    if ka.get("judge_model_id") != kb.get("judge_model_id"):
        warnings.append("The two configs were judged by different judge models; diagnosis and end-to-end rates are not comparable.")
    return {
        "metric": metric,
        "a": ka, "b": kb,
        "pairs": n,
        "a_rate": (sum(a[p] for p in shared) / n) if n else None,
        "b_rate": (sum(b[p] for p in shared) / n) if n else None,
        "a_wilson": ca, "b_wilson": cb,
        "only_a": only_a, "only_b": only_b, "both": both, "neither": neither,
        "p_value": mcnemar_exact(only_a, only_b) if n else None,
        "by_problem": sorted(by_problem.values(), key=lambda r: r["problem"]),
        "warnings": warnings,
    }
