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
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import firebase_sync
import runner

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
    }


class KeyIn(BaseModel):
    provider: str
    value: str


@app.post("/api/key")
def set_key(body: KeyIn):
    try:
        runner.write_key(body.provider, body.value)
        return {"ok": True, "message": f"saved {body.provider} to your local ReliOps/.env"}
    except OSError as e:
        return {"ok": False, "message": f"could not save {body.provider}: {e}"}


class FirebaseIn(BaseModel):
    service_account_path: str


@app.post("/api/firebase")
def connect_firebase(body: FirebaseIn):
    result = firebase_sync.configure(body.service_account_path)
    return {"status": result}


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
    run_by: Optional[str] = None,  # self-reported name, for a shared-key team — see README
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
        force_build=force_build,
        run_by=run_by,
        on_result=on_result,
    )
    return _sse(gen, "run")
