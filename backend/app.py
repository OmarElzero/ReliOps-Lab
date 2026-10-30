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
import jobs
import queueplan
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


def _static(name: str, media_type: str | None = None) -> FileResponse:
    # no-cache: after a `git pull` the browser must pick up the new screens, not a stale copy
    return FileResponse(FRONTEND_DIR / name, media_type=media_type, headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return _static("index.html")


@app.get("/app.js")
def app_js():
    return _static("app.js")


@app.get("/v2.js")
def v2_js():
    return _static("v2.js")


@app.get("/queue.js")
def queue_js():
    return _static("queue.js")


@app.get("/style.css")
def style_css():
    return _static("style.css")


@app.get("/api/config")
def config():
    return {
        "environments": [{"id": "sregym", "label": "SREGym (live Kubernetes faults)"}],
        "agents": runner.AGENTS,
        "model_presets": runner.MODEL_PRESETS,
        "problem_sets": _all_problem_sets(),
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


def _data(line: str) -> str:
    return f"data: {json.dumps(line)}\n\n"


def _tail(job: jobs.Job, offset: int = 0):
    """Server-Sent Events view of a job's output, from `offset` (negative = last N lines). Ends
    with [[DONE]] once the job has finished and everything has been sent."""
    def gen():
        pos = offset
        while True:
            lines, pos, finished = job.read(pos, wait=15.0)
            first = pos - len(lines)
            for i, line in enumerate(lines, start=1):
                yield f"id: {first + i}\n" + _data(line)   # id = absolute line count, so a client can resume
            if finished:
                break
            if not lines:
                yield ": keep-alive\n\n"   # SSE comment: keeps proxies and the browser from timing out
        yield "data: [[DONE]]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def _refusal(lines: list[str]) -> StreamingResponse:
    def gen():
        for line in lines:
            yield _data(line)
        yield "data: [[DONE]]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


def _job_sse(gen_factory, kind: str, name: str, meta: dict | None = None) -> StreamingResponse:
    """Runs and queues are driven by a server thread (jobs.py), so closing the tab does not stop
    them; this starts the job and streams its output. Only one job at a time (one cluster)."""
    job = jobs.start(kind, name, gen_factory, meta)
    if job is None:
        cur = jobs.current()
        return _refusal([f"Another job is already running ({cur.kind}: {cur.name}). Open the Queue tab to watch it, "
                         "or stop it first.\n"])
    return _tail(job)


def _sse(gen, log_kind: str) -> StreamingResponse:
    """Streams `gen`'s lines to the browser as Server-Sent Events, and tees every line into a
    timestamped file under results/gui_logs/ at the same time — so the full output survives a
    closed tab or a cleared log panel, for debugging after the fact. (Setup and demo; runs and
    queues use _job_sse so they outlive the tab.)"""
    def wrap():
        log_path = runner.new_log_file(log_kind)
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write(f"[log file: {log_path}]\n")
            yield _data(f"[saving full output to {log_path}]")
            for line in gen:
                logf.write(line)
                logf.flush()
                yield _data(line)
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


def _on_result(record: dict) -> None:
    """Called after every individual run with the record we could recover: share the story with
    teammates, mirror the row to Firestore, and append it to the local raw backup and the CSV."""
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


def _run_lines(*, agent: str, model: str, judge_model: str, problems: list[str], action_space: Optional[str] = None,
               force_build: bool = False, repeat_n: int = 1, run_by: Optional[str] = None,
               config_label: Optional[str] = None, run_class: str = "exploration", record_walkthrough: bool = True,
               manifest_id: Optional[str] = None, slice_k: int = 1, slice_n: int = 1,
               cells: Optional[list[tuple[str, int]]] = None):
    """The lines of one run: refusals first (frozen runs must match their manifest), then
    runner.stream_campaign. Shared by /api/run and the queue."""
    problem_list = list(problems)
    cell_list = cells
    if manifest_id:
        manifest = next((m for m in store.read("manifests") if m.get("manifest_id") == manifest_id), None)
        if manifest is None:
            yield f"unknown manifest {manifest_id}\n"
            return
        problem_list = list(manifest["problems"])
        wrong = campaign.verify_run(manifest, agent_id=agent, model_id=model, action_space=action_space,
                                    judge_model_id=judge_model, config_label=config_label or "", problems=problem_list)
        if wrong:
            yield "REFUSED: this run does not match the frozen manifest:\n"
            for w in wrong:
                yield f"  - {w}\n"
            yield "Fix the settings above (or freeze a new manifest) and run again.\n"
            return
        _, all_runs = _collect_runs(5000)
        todo = campaign.grid(manifest, all_runs)["todo"]
        try:
            cell_list = campaign.slice_cells(todo, slice_k, slice_n)
        except ValueError as e:
            yield f"{e}\n"
            return
        run_class = "frozen"
        repeat_n = manifest["repeat_n"]
        if not cell_list:
            yield "nothing left to run for this slice: every cell has a result\n"
            return
    elif run_class == "frozen":
        yield ("A frozen run needs a manifest: freeze one in the Campaigns tab first, so the settings "
               "it was measured with are recorded and enforced.\n")
        return

    yield from runner.stream_campaign(
        agent=agent, action_space=action_space, model=model, judge_model=judge_model, problems=problem_list,
        repeat_n=repeat_n, force_build=force_build, run_by=run_by, config_label=config_label, run_class=run_class,
        record_walkthrough=record_walkthrough, cells=cell_list, manifest_id=manifest_id, on_result=_on_result,
    )


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
            return _refusal(["invalid cells list, expected problem:repeat,problem:repeat\n"])
    kwargs = dict(agent=agent, model=model, judge_model=judge_model, problems=problem_list, action_space=action_space,
                  force_build=force_build, repeat_n=repeat_n, run_by=run_by, config_label=config_label,
                  run_class=run_class, record_walkthrough=record_walkthrough, manifest_id=manifest_id,
                  slice_k=slice_k, slice_n=slice_n, cells=cell_list)
    return _job_sse(lambda: _run_lines(**kwargs), "run", f"{agent} · {len(problem_list)} problem(s)")


# ---- Job state, saved problem sets, run queues ----------------------------------------------
@app.get("/api/state")
def state():
    """What the server is doing right now. Polled by the header so a run started in another tab (or
    before a page reload) is always visible, with a way to stop it."""
    job = jobs.current()
    return {"busy": jobs.busy(), "job": job.describe() if job else None}


@app.get("/api/job/stream")
def job_stream(offset: int = -300):
    """Re-attach to the current (or last) job's output: the last `offset` lines, then live."""
    job = jobs.current()
    if job is None:
        return _refusal(["no job has run since the server started\n"])
    return _tail(job, offset)


class ProblemSetIn(BaseModel):
    name: str
    problems: list[str]
    created_by: str = ""


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def _all_problem_sets() -> list[dict]:
    """The CSV sets from the ReliOps clone plus sets saved from the Lab (for example a dev set),
    placed before the 'custom' pseudo-set. Saved sets borrow category and difficulty from the CSV
    rows when the problem is in one."""
    base = runner.problem_sets()
    known = {p["id"]: p for s in base for p in s["problems"]}
    saved = []
    for rec in sorted(store.read("problem_sets"), key=lambda r: r.get("name", "")):
        if rec.get("deleted") or not rec.get("problems"):
            continue
        rows = []
        for i, pid in enumerate(rec["problems"], start=1):
            k = known.get(pid) or {}
            rows.append({"id": pid, "n": str(i), "category": k.get("category") or "Saved set",
                         "difficulty": k.get("difficulty") or "", "app": k.get("app") or ""})
        saved.append({"id": "saved:" + rec["set_id"], "label": f"{rec['name']} ({len(rows)} problems, saved set)",
                      "problems": rows, "saved": True, "name": rec["name"]})
    custom = [s for s in base if s["id"] == "custom"]
    return [s for s in base if s["id"] != "custom"] + saved + custom


@app.post("/api/problem_set")
def save_problem_set(body: ProblemSetIn):
    name = body.name.strip()
    problems = list(dict.fromkeys(p.strip() for p in body.problems if p.strip()))
    if not name or not problems:
        return JSONResponse({"error": "a set needs a name and at least one problem"}, status_code=400)
    rec = store.append("problem_sets", {"set_id": _slug(name), "name": name, "problems": problems,
                                        "created_by": body.created_by or "unknown"})
    return {"ok": True, "set": rec}


@app.delete("/api/problem_set/{set_id}")
def delete_problem_set(set_id: str):
    rec = next((r for r in store.read("problem_sets") if r.get("set_id") == set_id), None)
    if rec is None:
        return JSONResponse({"error": "unknown set"}, status_code=404)
    store.append("problem_sets", {**rec, "deleted": True})
    return {"ok": True}


class QueueIn(BaseModel):
    queue_id: Optional[str] = None
    name: str
    steps: list[dict]
    created_by: str = ""


def _queues() -> list[dict]:
    return sorted((q for q in store.read("queues") if not q.get("deleted")),
                  key=lambda q: q.get("updated_at", ""), reverse=True)


def _find_queue(queue_id: str) -> Optional[dict]:
    return next((q for q in _queues() if q.get("queue_id") == queue_id), None)


@app.get("/api/queues")
def queues():
    return {"queues": _queues()}


@app.post("/api/queue")
def save_queue(body: QueueIn):
    try:
        q = queueplan.clean_queue(body.model_dump())
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "queue": store.append("queues", q)}


@app.delete("/api/queue/{queue_id}")
def delete_queue(queue_id: str):
    q = _find_queue(queue_id)
    if q is None:
        return JSONResponse({"error": "unknown queue"}, status_code=404)
    store.append("queues", {**q, "deleted": True})
    return {"ok": True}


@app.get("/api/queue/preview")
def queue_preview(queue_id: str):
    q = _find_queue(queue_id)
    if q is None:
        return JSONResponse({"error": "unknown queue"}, status_code=404)
    _, runs = _collect_runs(5000)
    return queueplan.preview(q, runs, store.read("manifests"))


@app.post("/api/queue/preview")
def queue_preview_draft(body: QueueIn):
    """Same as the GET preview but for a queue that has not been saved yet, so the screen can show
    live cell counts while a queue is being edited."""
    draft = body.model_dump()
    draft["name"] = draft["name"].strip() or "draft"
    try:
        q = queueplan.clean_queue(draft)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    _, runs = _collect_runs(5000)
    return queueplan.preview(q, runs, store.read("manifests"))


def _queue_lines(queue: dict, run_by: Optional[str]):
    steps = queue["steps"]
    manifests = store.read("manifests")
    runner._stop_event.clear()
    yield f"##### [QUEUE] {queue['name']}: {len(steps)} step(s) #####\n"
    for i, step in enumerate(steps, start=1):
        if runner._stop_event.is_set():
            yield f"\n##### [QUEUE] stopped before step {i}/{len(steps)}; the rest was not started #####\n"
            return
        if not step.get("enabled", True):
            yield f"##### [QUEUE] step {i}/{len(steps)} skipped (switched off): {step['name']} #####\n"
            continue
        manifest = next((m for m in manifests if m.get("manifest_id") == step.get("manifest_id")), None) \
            if step.get("manifest_id") else None
        if step.get("manifest_id") and manifest is None:
            yield f"##### [QUEUE] step {i}/{len(steps)} cannot run: frozen campaign {step['manifest_id']} not found #####\n"
            continue
        _, runs = _collect_runs(5000)
        cell_list = queueplan.todo(step, runs, manifest)
        yield f"\n##### [QUEUE] step {i}/{len(steps)} start: {step['name']} | runs {len(cell_list)} to do #####\n"
        if not cell_list:
            yield f"##### [QUEUE] step {i}/{len(steps)} nothing to do: every cell already has a result #####\n"
            continue
        yield from _run_lines(
            agent=step["agent_id"], model=step["model_id"], judge_model=step["judge_model_id"],
            problems=step["problems"], action_space=step.get("action_space"), force_build=bool(step.get("force_build")),
            repeat_n=step["repeat_n"], run_by=run_by, config_label=step.get("config_label") or None,
            run_class=step["run_class"], record_walkthrough=bool(step.get("record_walkthrough", True)),
            manifest_id=step.get("manifest_id"), cells=None if step.get("manifest_id") else cell_list)
        yield f"##### [QUEUE] step {i}/{len(steps)} end #####\n"
    yield "\n##### [QUEUE] finished #####\n"


@app.get("/api/queue/run")
def queue_run(queue_id: str, run_by: Optional[str] = None):
    q = _find_queue(queue_id)
    if q is None:
        return _refusal(["unknown queue\n"])
    if not q.get("steps"):
        return _refusal(["this queue has no steps\n"])
    return _job_sse(lambda: _queue_lines(q, run_by), "queue", q["name"], {"queue_id": queue_id})


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
