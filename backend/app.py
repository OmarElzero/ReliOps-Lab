"""ReliOps Lab — local GUI server (v0).

Pick a test environment, an agent, a model and a problem set, add your own API key, click Run,
and watch the log stream live. This is the local-first v0 of the console speced in
docs/plan/05-platform-spec.md of the main ReliOps repo (v1 there adds the browser-hosted,
multi-machine version with sealed-box keys — this is the one-laptop version that works today).

Run: `uvicorn app:app --port 8765 --reload` from this directory, then open http://localhost:8765
"""

from __future__ import annotations

import json
import os
import queue
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

import audit
import campaign
import export
import firebase_sync
import runner
import stats
import store

app = FastAPI(title="ReliOps Lab")

# This server holds API keys, can start runs that change a cluster, and can write to the team's
# Firestore, and it has no login by design: it is a single-user local tool, bound to 127.0.0.1.
# What it must still refuse is a *web page* in the user's browser calling it (cross-site requests,
# DNS rebinding). So: only accept requests whose Host is a loopback name, and refuse state-changing
# requests that carry a foreign Origin. LAB_ALLOWED_HOSTS (comma-separated) adds names, for example
# when reaching the Lab through a tunnel you control.
_LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1", "testserver"}


def _allowed_hosts() -> set[str]:
    extra = {h.strip().lower() for h in os.environ.get("LAB_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return _LOOPBACK | extra


def _host_only(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):  # [::1]:8765
        return value.split("]")[0] + "]"
    return value.split(":")[0]


@app.middleware("http")
async def local_only_guard(request, call_next):
    allowed = _allowed_hosts()
    if _host_only(request.headers.get("host", "")) not in allowed:
        return PlainTextResponse("ReliOps Lab only answers on localhost.", status_code=403)
    origin = request.headers.get("origin")
    if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
        from urllib.parse import urlparse

        if (urlparse(origin).hostname or "").lower() not in {h.strip("[]") for h in allowed}:
            return PlainTextResponse("Cross-site request refused.", status_code=403)
    return await call_next(request)
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/app.js")
def app_js():
    return FileResponse(FRONTEND_DIR / "app.js")


@app.get("/v2.js")
def v2_js():
    return FileResponse(FRONTEND_DIR / "v2.js")


@app.get("/style.css")
def style_css():
    return FileResponse(FRONTEND_DIR / "style.css")


@app.get("/api/config")
def config():
    return {
        "environments": [{"id": "sregym", "label": "SREGym (live Kubernetes faults)"}],
        "agents": runner.AGENTS,
        "model_presets": runner.MODEL_PRESETS,
        "problem_sets": runner.problem_sets(),
        "firebase_status": firebase_sync.status(),
        "reliops_dir": str(runner.reliops_dir()),
        "default_firebase_key_path": str(runner.default_firebase_key_path()),
        "saved_providers": runner.saved_providers(),
        "custom_saved_keys": runner.custom_saved_keys(),
    }


class KeyIn(BaseModel):
    provider: str
    value: str


@app.post("/api/key")
def set_key(body: KeyIn):
    try:
        runner.write_key(body.provider, body.value)
        return {"ok": True, "message": f"saved {body.provider} to .env in this project's own folder"}
    except OSError as e:
        return {"ok": False, "message": f"could not save {body.provider}: {e}"}


@app.get("/api/firebase-status")
def firebase_status():
    """Polled by the frontend every few seconds so the status pill reflects this project's own
    .env — there is no manual "Connect" step any more; .env is the only source (firebase_sync.py)."""
    return {"status": firebase_sync.status()}


def _sse(gen, log_kind: str) -> StreamingResponse:
    """Streams `gen`'s lines to the browser as Server-Sent Events, and tees every line into a
    timestamped file under results/gui_logs/ at the same time — so the full output survives a
    closed tab or a cleared log panel, for debugging after the fact."""
    def wrap():
        log_path = runner.new_log_file(log_kind)
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write(f"[log file: {log_path}]\n")
            yield f"data: {json.dumps(f'[saving full output to {log_path}]')}\n\n"
            for line in gen:
                logf.write(line)
                logf.flush()
                yield f"data: {json.dumps(line)}\n\n"
        yield "data: [[DONE]]\n\n"

    return StreamingResponse(wrap(), media_type="text/event-stream")


@app.get("/api/doctor")
def doctor():
    return runner.doctor()


def _collect_runs(limit: int) -> tuple[str, list[dict]]:
    """Every recorded run, newest first, as flat dicts the tables and the statistics both read.
    Firestore (the whole team's runs) wins when connected; this machine's own
    results/gui_runs.jsonl is the fallback when it isn't — either way the caller is told which
    one it got, since "no runs yet" and "not seeing the team's runs" are different problems."""
    firestore_runs = firebase_sync.list_runs(limit)
    if firestore_runs is not None:
        source = "firestore"
        raw_runs = firestore_runs
    else:
        source = "local"
        raw_runs = runner.list_local_runs(limit)

    reviews = {x["run_id"]: x for x in store.read("reviews") if x.get("run_id")}
    runs = []
    for r in raw_runs:
        normalized = runner.normalize_sregym_row(r.get("row"))
        review = reviews.get(r.get("run_id")) or {}
        # Older records (before the Lab stored these) get them recovered from SREGym's own row.
        diag = runner.extract_diagnosis(r.get("row"))
        runs.append({
            "run_id": r.get("run_id"),
            "timestamp_utc": r.get("timestamp_utc"),
            "run_by": r.get("run_by") or "unknown",
            "problem_id": r.get("problem_id"),
            "agent_id": r.get("agent_id"),
            "model_id": r.get("model_id"),
            "judge_model_id": r.get("judge_model_id"),
            "action_space": r.get("action_space"),
            "config_label": r.get("config_label") or "",
            "run_class": r.get("run_class") or "exploration",
            "repeat_index": r.get("repeat_index"),
            "repeat_n": r.get("repeat_n"),
            "log_file": r.get("log_file"),  # servable via GET /api/log/<name>, if present
            "diagnosis_text": r.get("diagnosis_text") or diag["diagnosis_text"],
            "judge_reasoning": r.get("judge_reasoning") or diag["judge_reasoning"],
            "judge_checklist": r.get("judge_checklist") or diag["judge_checklist"],
            "has_story": bool(r.get("transcript_rel") or r.get("story_shared")),
            "manifest_id": r.get("manifest_id"),
            "git_commit": r.get("git_commit"),
            "total_tokens": r.get("total_tokens"),
            "prompt_tokens": r.get("prompt_tokens"),
            "completion_tokens": r.get("completion_tokens"),
            "num_llm_calls": r.get("num_llm_calls"),
            "num_tool_calls": r.get("num_tool_calls"),
            "failure_mode_tag": review.get("failure_mode_tag") or "",
            "first_bad_step": review.get("first_bad_step"),
            "review_note": review.get("note") or "",
            "reviewed_by": review.get("reviewer") or "",
            **normalized,
        })
    return source, runs


@app.get("/api/results")
def results(limit: int = 300):
    """Backs the Results tab/dashboard: every recorded run, newest first, and who ran it."""
    source, runs = _collect_runs(limit)
    return {"source": source, "runs": runs}


@app.get("/api/summary")
def summary(metric: str = "e2e_pass", run_class: Optional[str] = None, limit: int = 5000):
    """Pass rate per configuration with a Wilson interval and a problem-level bootstrap interval
    (docs/plan/03-testing-plan.md section 6). Runs that crashed or never finished are reported as
    `incomplete`, never silently counted as failures."""
    if metric not in stats.METRICS:
        return JSONResponse({"error": f"metric must be one of {stats.METRICS}"}, status_code=400)
    source, runs = _collect_runs(limit)
    if run_class:
        runs = [r for r in runs if r["run_class"] == run_class]
    return {"source": source, "metric": metric, "groups": stats.summarize(runs, metric)}


@app.get("/api/compare")
def compare(a: str, b: str, metric: str = "e2e_pass", limit: int = 5000):
    """Paired comparison of two configurations (keys come from /api/summary): matched on
    (problem, repeat), exact McNemar on the pairs where the outcome flipped."""
    if metric not in stats.METRICS:
        return JSONResponse({"error": f"metric must be one of {stats.METRICS}"}, status_code=400)
    _, runs = _collect_runs(limit)
    return stats.compare(runs, a, b, metric)


@app.get("/api/story/{run_id}")
def story(run_id: str):
    """Step-by-step story of one run (agent thoughts, actions and observations, interleaved with the
    cluster timeline), as Markdown. Only available on the machine that ran it and only for reliops_v1."""
    if not run_id.replace("-", "").isalnum():
        return PlainTextResponse("invalid run id", status_code=400)
    record = next((r for r in runner.list_local_runs(100000) if r.get("run_id") == run_id), None)
    local_error = "No such run on this machine."
    if record is not None:
        try:
            return PlainTextResponse(runner.render_story(record), media_type="text/markdown; charset=utf-8")
        except FileNotFoundError as e:
            local_error = str(e)
    shared = firebase_sync.read_story(run_id)  # a teammate's run: the story was shared when it finished
    if shared:
        return PlainTextResponse(shared, media_type="text/markdown; charset=utf-8")
    return PlainTextResponse(local_error + " No shared copy found either (shared stories need Firebase connected on both machines).",
                             status_code=404)


@app.post("/api/stop")
def stop():
    """Asks the batch currently streaming from /api/run to stop after the problem it's on finishes
    and its result is recorded, instead of starting the next one — see runner.request_stop()."""
    runner.request_stop()
    return {"ok": True}


@app.get("/api/log/{name}")
def get_log(name: str):
    """Serves one per-problem log file from results/gui_logs/ (see
    runner.stream_campaign's per-problem log, linked from the Results tab) so a specific run can
    be inspected after the fact without scrolling the combined Live Log for it. `name` must be a
    bare filename — never trusted to walk outside that one directory."""
    if "/" in name or "\\" in name or ".." in name:
        return PlainTextResponse("invalid log file name", status_code=400)
    log_path = runner.reliops_dir() / "results" / "gui_logs" / name
    if not log_path.is_file():
        return PlainTextResponse(f"no such log file: {name}", status_code=404)
    return FileResponse(log_path, media_type="text/plain; charset=utf-8")


@app.get("/api/setup")
def setup():
    return _sse(runner.stream_setup(), "setup")


@app.get("/api/demo")
def demo():
    return _sse(runner.stream_demo(), "demo")


@app.get("/api/run")
def run(
    agent: str,
    model: str,
    judge_model: str,
    problems: str,  # comma-separated problem ids
    action_space: Optional[str] = None,
    force_build: bool = False,
    repeat_n: int = 1,  # run each problem this many times in a row — see runner.stream_campaign
    run_by: Optional[str] = None,  # self-reported name, for a shared-key team — see README
    config_label: Optional[str] = None,  # free text naming the agent variant, e.g. "F7-on" or "base"
    run_class: str = "exploration",  # "exploration" or "frozen" — kept apart in every statistic
    record_walkthrough: bool = True,  # record the cluster timeline so the run has a story
    manifest_id: Optional[str] = None,  # run (the missing cells of) a frozen campaign
    slice_k: int = 1,  # this machine's share of the missing cells: k of n
    slice_n: int = 1,
    cells: Optional[str] = None,  # explicit "problem:repeat,problem:repeat" list
):
    problem_list = [p.strip() for p in problems.split(",") if p.strip()]
    cell_list = None
    if cells:
        try:
            cell_list = [(c.rsplit(":", 1)[0], int(c.rsplit(":", 1)[1])) for c in cells.split(",") if c.strip()]
        except (ValueError, IndexError):
            return _sse(iter(["invalid cells list, expected problem:repeat,problem:repeat\n"]), "run")

    if manifest_id:
        manifest = next((m for m in store.read("manifests") if m.get("manifest_id") == manifest_id), None)
        if manifest is None:
            return _sse(iter([f"unknown manifest {manifest_id}\n"]), "run")
        problem_list = list(manifest["problems"])
        wrong = campaign.verify_run(manifest, agent_id=agent, model_id=model, action_space=action_space,
                                    judge_model_id=judge_model, config_label=config_label or "", problems=problem_list)
        if wrong:
            return _sse(iter(["REFUSED: this run does not match the frozen manifest:\n"] +
                             [f"  - {w}\n" for w in wrong] +
                             ["Fix the settings above (or freeze a new manifest) and run again.\n"]), "run")
        _, all_runs = _collect_runs(5000)
        todo = campaign.grid(manifest, all_runs)["todo"]
        try:
            cell_list = campaign.slice_cells(todo, slice_k, slice_n)
        except ValueError as e:
            return _sse(iter([f"{e}\n"]), "run")
        run_class = "frozen"
        repeat_n = manifest["repeat_n"]
        if not cell_list:
            return _sse(iter(["nothing left to run for this slice: every cell has a result\n"]), "run")
    elif run_class == "frozen":
        return _sse(iter(["A frozen run needs a manifest: freeze one in the Campaigns tab first, so the settings "
                          "it was measured with are recorded and enforced.\n"]), "run")

    def on_result(record: dict) -> None:
        # Share the story with teammates before the run record goes out, so the flag travels with it.
        try:
            record["story_shared"] = firebase_sync.write_story(record["run_id"], runner.render_story(record)).startswith("written")
        except (FileNotFoundError, ValueError, OSError):
            record["story_shared"] = False
        outcome = firebase_sync.write_run(record)
        record["_firebase"] = outcome
        # Full record, every field, nothing dropped — the raw backup.
        results_path = runner.reliops_dir() / "results" / "gui_runs.jsonl"
        results_path.parent.mkdir(parents=True, exist_ok=True)
        with results_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
        # Same row, trimmed to the team's canonical results/*.csv schema — see
        # runner.append_csv_run's docstring for why both files exist.
        runner.append_csv_run(record)

    gen = runner.stream_campaign(
        agent=agent,
        action_space=action_space,
        model=model,
        judge_model=judge_model,
        problems=problem_list,
        repeat_n=repeat_n,
        force_build=force_build,
        run_by=run_by,
        config_label=config_label,
        run_class=run_class,
        record_walkthrough=record_walkthrough,
        cells=cell_list,
        manifest_id=manifest_id,
        on_result=on_result,
    )
    return _sse(gen, "run")


# ---- Problem cards ------------------------------------------------------------------
@app.get("/api/cards")
def cards():
    return {"cards": runner.list_cards()}


@app.get("/api/card/{problem_id}")
def card(problem_id: str):
    text = runner.read_card(problem_id)
    if text is None:
        return PlainTextResponse("No problem card for this problem yet. Copy experiments/problem-cards/TEMPLATE.md "
                                 "in the ReliOps repo to experiments/problem-cards/<problem_id>.md.", status_code=404)
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


# ---- Failure tags / reviews -----------------------------------------------------------
FAILURE_TAGS = ["blamed-wrong-service", "wrong-mechanism", "anchored-on-first-claim", "stopped-early", "no-action-fits",
                "fix-didnt-hold", "tool-error", "timeout", "invented-incident", "other"]


class ReviewIn(BaseModel):
    run_id: str
    failure_mode_tag: str = ""
    first_bad_step: Optional[int] = None
    note: str = ""
    reviewer: str = ""


@app.get("/api/failure_tags")
def failure_tags():
    return {"tags": FAILURE_TAGS}


@app.post("/api/review")
def review(body: ReviewIn):
    if body.failure_mode_tag and body.failure_mode_tag not in FAILURE_TAGS:
        return JSONResponse({"error": f"tag must be one of {FAILURE_TAGS}"}, status_code=400)
    rec = store.append("reviews", {"run_id": body.run_id, "failure_mode_tag": body.failure_mode_tag,
                                   "first_bad_step": body.first_bad_step, "note": body.note,
                                   "reviewer": body.reviewer or "unknown"})
    return {"ok": True, "review": rec}


# ---- Judge audit ----------------------------------------------------------------------
class LabelIn(BaseModel):
    run_id: str
    correct: bool
    labeler: str = ""


@app.get("/api/label_queue")
def label_queue(limit: int = 20):
    _, runs = _collect_runs(5000)
    labels = store.read("labels")
    queue_ = audit.label_queue(runs, labels, limit)
    for item in queue_:
        card = runner.read_card(item["problem_id"] or "")
        item["expected"] = runner.card_section(card, 4)  # "What a correct diagnosis must say", if the card has it
    return {"queue": queue_, "labeled": len(labels)}


@app.post("/api/label")
def label(body: LabelIn):
    return {"ok": True, "label": store.append("labels", {"run_id": body.run_id, "correct": body.correct,
                                                         "labeler": body.labeler or "unknown"})}


@app.get("/api/judge_audit")
def judge_audit():
    _, runs = _collect_runs(5000)
    return audit.judge_audit(runs, store.read("labels"), store.read("rejudges"))


@app.get("/api/rejudge")
def rejudge(judge_model: str, scope: str = "labeled"):
    """Re-judge stored diagnoses with another judge. scope=labeled (default) re-judges only runs a person
    has hand-labelled, which is what the agreement table needs; scope=all re-judges every stored diagnosis."""
    _, runs = _collect_runs(5000)
    labeled = {l["run_id"] for l in store.read("labels")}
    have = {r["run_id"] for r in store.read("rejudges") if r.get("judge_model_id") == judge_model}
    items = [{"run_id": r["run_id"], "problem_id": r["problem_id"], "submission": r["diagnosis_text"]}
             for r in runs if (r.get("diagnosis_text") or "").strip() and r["run_id"] not in have
             and (scope == "all" or r["run_id"] in labeled)]
    return _sse(runner.stream_rejudge(judge_model, items, on_result=lambda rec: store.append("rejudges", rec)), "rejudge")


# ---- Frozen campaigns -----------------------------------------------------------------
class ManifestIn(BaseModel):
    model_config = {"protected_namespaces": ()}
    name: str = ""
    agent_id: str
    model_id: str
    action_space: Optional[str] = None
    judge_model_id: str
    config_label: str
    problems: list[str]
    repeat_n: int = 3
    created_by: str = ""
    notes: str = ""


@app.post("/api/manifest")
def create_manifest(body: ManifestIn):
    try:
        m = campaign.create_manifest(name=body.name, agent_id=body.agent_id, model_id=body.model_id,
                                     action_space=body.action_space, judge_model_id=body.judge_model_id,
                                     problems=body.problems, repeat_n=body.repeat_n, config_label=body.config_label,
                                     created_by=body.created_by, notes=body.notes)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "manifest": store.append("manifests", m)}


@app.get("/api/manifests")
def manifests():
    return {"manifests": sorted(store.read("manifests"), key=lambda m: m.get("created_at", ""), reverse=True)}


@app.get("/api/campaign")
def campaign_grid(manifest_id: str, metric: str = "e2e_pass", k: int = 1, n: int = 1):
    manifest = next((m for m in store.read("manifests") if m.get("manifest_id") == manifest_id), None)
    if manifest is None:
        return JSONResponse({"error": "unknown manifest"}, status_code=404)
    _, runs = _collect_runs(5000)
    g = campaign.grid(manifest, runs, metric)
    try:
        mine = campaign.slice_cells(g["todo"], k, n)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    g["slice"] = {"k": k, "n": n, "cells": [{"problem": p, "repeat": r} for p, r in mine]}
    g["todo"] = [{"problem": p, "repeat": r} for p, r in g["todo"]]
    g["summary"] = campaign.manifest_runs_summary(manifest, runs, metric)
    g["current_commit"] = campaign.git_commit()
    g["agent_dirty"] = campaign.agent_dirty()
    return g


# ---- Standing audit and exports ---------------------------------------------------------
@app.get("/api/standing_audit")
def standing_audit(metric: str = "e2e_pass"):
    _, runs = _collect_runs(5000)
    return audit.standing_audit(runs, metric)


@app.get("/api/export/runs.csv")
def export_runs():
    _, runs = _collect_runs(100000)
    return PlainTextResponse(export.runs_csv(runs), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=reliops_runs.csv"})


@app.get("/api/export/summary.csv")
def export_summary(metric: str = "e2e_pass", run_class: Optional[str] = None):
    _, runs = _collect_runs(100000)
    if run_class:
        runs = [r for r in runs if r["run_class"] == run_class]
    return PlainTextResponse(export.summary_csv(stats.summarize(runs, metric)), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename=reliops_summary_{metric}.csv"})


@app.get("/api/export/summary.svg")
def export_summary_svg(metric: str = "e2e_pass", run_class: Optional[str] = None):
    _, runs = _collect_runs(100000)
    if run_class:
        runs = [r for r in runs if r["run_class"] == run_class]
    return PlainTextResponse(export.summary_svg(stats.summarize(runs, metric), metric), media_type="image/svg+xml",
                             headers={"Content-Disposition": f"attachment; filename=reliops_summary_{metric}.svg"})
