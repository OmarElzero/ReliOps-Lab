"""Pinned problem sets. `experiments/pins.json` in the ReliOps clone names the sets that are fixed for a
phase (the frozen set that is measured once, the development set that may be read and tuned on) together
with a hash of each set's problem ids. The Lab compares every CSV with its pin, so a set that was edited
after pinning is flagged instead of silently measured."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import campaign
import runner

ROLES = ("frozen", "dev")


def _ids(path: Path) -> list[str] | None:
    try:
        with path.open(encoding="utf-8", newline="") as f:
            return [(r.get("problem_id") or "").strip() for r in csv.DictReader(f) if (r.get("problem_id") or "").strip()]
    except OSError:
        return None


def load() -> list[dict]:
    exp = runner.reliops_dir() / "experiments"
    try:
        raw = json.loads((exp / "pins.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for name, p in raw.items():
        if name.startswith("_") or not isinstance(p, dict) or p.get("role") not in ROLES:
            continue
        ids = _ids(exp / p.get("file", ""))
        if ids is None:
            status = "missing"
        else:
            status = "ok" if campaign.problems_hash(ids) == p.get("problems_hash") else "changed"
        out.append({"name": name, "role": p["role"], "file": p.get("file"), "set_id": Path(p.get("file", "")).stem,
                    "pinned_hash": p.get("problems_hash"), "pinned_on": p.get("pinned_on"),
                    "pinned_by": p.get("pinned_by"), "note": p.get("note", ""), "count": len(ids or []),
                    "problems": ids or [], "status": status})
    return out


def roles(pins: list[dict] | None = None) -> dict[str, str]:
    """problem id -> 'frozen' or 'dev'. A problem pinned in both roles is reported by overlap(), and
    left out here so it is never silently counted as either."""
    seen: dict[str, set[str]] = {}
    for p in (pins if pins is not None else load()):
        for pid in p["problems"]:
            seen.setdefault(pid, set()).add(p["role"])
    return {pid: next(iter(r)) for pid, r in seen.items() if len(r) == 1}


def overlap(pins: list[dict] | None = None) -> list[str]:
    seen: dict[str, set[str]] = {}
    for p in (pins if pins is not None else load()):
        for pid in p["problems"]:
            seen.setdefault(pid, set()).add(p["role"])
    return sorted(pid for pid, r in seen.items() if len(r) > 1)
