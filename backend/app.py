"""ReliOps Lab — local GUI server (v0).

Pick a test environment, an agent, a model and a problem set, add your own API key, click Run,
and watch the log stream live. This is the local-first v0 of the console speced in
docs/plan/05-platform-spec.md of the main ReliOps repo (v1 there adds the browser-hosted,
multi-machine version with sealed-box keys — this is the one-laptop version that works today).

Run: `uvicorn app:app --port 8765 --reload` from this directory, then open http://localhost:8765
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

import firebase_sync
import runner
import stats

app = FastAPI(title="ReliOps Lab")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/app.js")
def app_js():
    return FileResponse(FRONTEND_DIR / "app.js")


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

    runs = []
    for r in raw_runs:
        normalized = runner.normalize_sregym_row(r.get("row"))
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
            "has_story": bool(r.get("transcript_rel")),
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
    if record is None:
        return PlainTextResponse("No such run on this machine (stories are stored where the run happened).", status_code=404)
    try:
        return PlainTextResponse(runner.render_story(record), media_type="text/markdown; charset=utf-8")
    except FileNotFoundError as e:
        return PlainTextResponse(str(e), status_code=404)


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
):
    problem_list = [p.strip() for p in problems.split(",") if p.strip()]

    def on_result(record: dict) -> None:
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
        on_result=on_result,
    )
    return _sse(gen, "run")
