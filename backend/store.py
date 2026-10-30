"""Small append-only stores next to results/gui_runs.jsonl: reviews (failure tags), hand labels,
re-judge verdicts and frozen-campaign manifests. Each kind is a JSONL file; the latest record per key
wins, so a correction is just another append and the history stays in the file. Every record is also
mirrored to Firestore when connected, and reads merge both so a whole team sees the same data."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import firebase_sync
import runner

# kind -> (file name, key fields)
KINDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "reviews": ("gui_reviews.jsonl", ("run_id",)),
    "labels": ("gui_labels.jsonl", ("run_id",)),
    "rejudges": ("gui_rejudge.jsonl", ("run_id", "judge_model_id")),
    "manifests": ("gui_manifests.jsonl", ("manifest_id",)),
    "queues": ("gui_queues.jsonl", ("queue_id",)),
    "problem_sets": ("gui_problem_sets.jsonl", ("set_id",)),
    "marks": ("gui_marks.jsonl", ("run_id",)),
    "key_status": ("gui_key_status.jsonl", ("owner_machine", "var")),
}


def _path(kind: str) -> Path:
    return runner.reliops_dir() / "results" / KINDS[kind][0]


def _key(kind: str, rec: dict) -> str:
    return "||".join(str(rec.get(f) or "") for f in KINDS[kind][1])


def _doc_id(kind: str, rec: dict) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", "__".join(str(rec.get(f) or "") for f in KINDS[kind][1]))[:200]


def append(kind: str, rec: dict) -> dict:
    """Stamps `updated_at`, appends locally, mirrors to Firestore. Returns the stored record."""
    rec = {**rec, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    path = _path(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    rec["_firebase"] = firebase_sync.write_doc(kind, _doc_id(kind, rec), rec)
    return rec


def read(kind: str) -> list[dict]:
    """Latest record per key, merging this machine's file with Firestore (when connected)."""
    records: list[dict] = []
    path = _path(kind)
    if path.is_file():
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    remote = firebase_sync.list_docs(kind)
    if remote:
        records.extend(remote)
    latest: dict[str, dict] = {}
    for r in records:
        k = _key(kind, r)
        if k not in latest or (r.get("updated_at") or "") >= (latest[k].get("updated_at") or ""):
            latest[k] = r
    return list(latest.values())
