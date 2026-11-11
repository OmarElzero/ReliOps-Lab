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
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

import audit
import audit_plan
import campaign
import claims
import cluster
import export
import firebase_sync
import jobs
import queueplan
import queuestate
import queuesummary
import runner
import stats
import pins
import dashboard
import publish
import store
import screening
import tasks as tasks_mod

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


@app.get("/tasks.js")
def tasks_js():
    return _static("tasks.js")


@app.get("/audit.js")
def audit_js():
    return _static("audit.js")


@app.get("/dashboard.js")
def dashboard_js():
    return _static("dashboard.js")


@app.get("/style.css")
def style_css():
    return _static("style.css")


@app.get("/api/config")
def config():
    return {
        "environments": [{"id": "sregym", "label": "SREGym (live Kubernetes faults)"}],
        "agents": runner.AGENTS,
        "agent_switches": [{"id": n, "help": runner.AGENT_SWITCH_HELP.get(n, "")} for n in runner.AGENT_SWITCHES],
        "variant_presets": runner.VARIANT_PRESETS,
        "model_presets": runner.MODEL_PRESETS,
        "problem_sets": _with_pins(_all_problem_sets()),
        "pins": [{k: v for k, v in p.items() if k != "problems"} for p in pins.load()],
        "pin_overlap": pins.overlap(),
        "purposes": [{"id": k, "help": v} for k, v in dashboard.PURPOSE_HELP.items()],
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


_REASON_CACHE: dict[tuple[str, float], str] = {}


def _incomplete_reason(r: dict) -> str:
    """Why a run has no result. Newer records carry it; older ones get it read from their own log when that log is on
    this machine (cached by file and modification time, so the table refresh does not re-read every log)."""
    if not runner.run_incomplete(r.get("row")):
        return ""
    if r.get("incomplete_reason"):
        return r["incomplete_reason"]
    name = r.get("log_file") or ""
    if not name or "/" in name or "\\" in name or ".." in name:
        return ""
    path = runner.reliops_dir() / "results" / "gui_logs" / name
    try:
        key = (name, path.stat().st_mtime)
        if key not in _REASON_CACHE:
            _REASON_CACHE[key] = runner.classify_incomplete(path.read_text(encoding="utf-8", errors="replace"))[1]
        return _REASON_CACHE[key]
    except OSError:
        return ""


_TOKEN_CACHE: dict[tuple[str, float], dict] = {}


def _token_fields(r: dict) -> dict:
    """Token counts of one run. Runs recorded before the 31 Oct fix stored 0 (the Lab read the wrong key names); when this
    machine still has the run's transcript the real counts are read from it now. Nothing stored is changed."""
    stored = {k: r.get(k) for k in ("total_tokens", "prompt_tokens", "completion_tokens")}
    rel = r.get("transcript_rel")
    if stored["total_tokens"] or not rel:
        return stored
    try:
        path = runner.sregym_dir() / rel
        key = (str(path), path.stat().st_mtime)
        if key not in _TOKEN_CACHE:
            _TOKEN_CACHE[key] = runner.transcript_stats(rel)
        fresh = _TOKEN_CACHE[key]
    except OSError:
        return stored
    return {k: (fresh.get(k) if fresh.get("total_tokens") else stored[k]) for k in stored}


def _collect_runs(limit: int) -> tuple[str, list[dict]]:
    """Every recorded run, newest first, as flat dicts the tables and the statistics both read.
    Firestore (the whole team's runs) wins when connected; this machine's own
    results/gui_runs.jsonl is the fallback when it isn't — either way the caller is told which
    one it got, since "no runs yet" and "not seeing the team's runs" are different problems."""
    remote = firebase_sync.list_runs_ex(limit)
    local_runs = runner.list_local_runs(limit)
    if remote["runs"] is None:
        source = "local"
        raw_runs = local_runs
    else:
        # The team's runs (live, or the last saved copy) plus this machine's own, so a run made here
        # never disappears from the page even when Firestore cannot be read.
        source = "firestore" if remote["live"] else "firestore-saved-copy"
        by_id = {r.get("run_id"): r for r in remote["runs"] if r.get("run_id")}
        for r in local_runs:
            by_id.setdefault(r.get("run_id"), r)
        raw_runs = sorted(by_id.values(), key=lambda r: str(r.get("timestamp_utc") or ""), reverse=True)[:limit]

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
            "agent_switches": r.get("agent_switches") or "",
            "experiment": r.get("experiment") or "",
            "origin": r.get("origin") or "",
            "incomplete_reason": _incomplete_reason(r),
            "run_class": r.get("run_class") or "exploration",
            "repeat_index": r.get("repeat_index"),
            "repeat_n": r.get("repeat_n"),
            "log_file": r.get("log_file"),  # servable via GET /api/log/<name>, if present
            "diagnosis_text": r.get("diagnosis_text") or diag["diagnosis_text"],
            "judge_reasoning": r.get("judge_reasoning") or diag["judge_reasoning"],
            "judge_checklist": r.get("judge_checklist") or diag["judge_checklist"],
            # The judge's D1-D3 and composite were stored with the run but never sent to the page, so the
            # detail panel showed "D1=? D2=? D3=?". Older records get them from SREGym's own row.
            **{k: (r.get(k) if r.get(k) is not None else diag[k])
               for k in ("diagnosis_d1", "diagnosis_d2", "diagnosis_d3", "diagnosis_composite")},
            "has_story": bool(r.get("transcript_rel") or r.get("story_shared")),
            "manifest_id": r.get("manifest_id"),
            "git_commit": r.get("git_commit"),
            # The tools this run used (kind, kubectl, docker, helm), and why it has no result: the paper needs both, and the
            # "machines differ" warning reads the first. They were recorded but dropped here, so results and exports lacked them.
            "machine_versions": r.get("machine_versions") or "",
            "incomplete_kind": r.get("incomplete_kind") or "",
            "key_slot": r.get("key_slot"),
            **_token_fields(r),
            "num_llm_calls": r.get("num_llm_calls"),
            "num_tool_calls": r.get("num_tool_calls"),
            "duration_s": r.get("duration_s"),
            "failure_mode_tag": review.get("failure_mode_tag") or "",
            "first_bad_step": review.get("first_bad_step"),
            "review_note": review.get("note") or "",
            "reviewed_by": review.get("reviewer") or "",
            **normalized,
        })
    _apply_external_fault_marks(runs)
    _enrich(runs)
    return source, runs


def _apply_external_fault_marks(runs: list[dict]) -> None:
    """A run a person tagged "external-fault" (a bad key, a broken cluster, a result recorded against the wrong run)
    says nothing about the agent, so it is not counted as a pass or a fail. The queue then treats its cell as
    incomplete and runs it again; the run itself stays in the record, tag and note included."""
    for r in runs:
        if (r.get("failure_mode_tag") or "").strip() == "external-fault":
            for k in ("diagnosis_pass", "mitigation_pass", "e2e_pass"):
                r[k] = None
            r["run_status"] = "incomplete"
            note = (r.get("review_note") or "").strip()
            r["incomplete_reason"] = "Marked as an external fault, so it is not counted" + (f": {note}" if note else ".")


def _problem_index() -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for s in runner.problem_sets():
        for p in s["problems"]:
            idx.setdefault(p["id"], {"category": p.get("category") or "", "app": p.get("app") or "",
                                     "difficulty": p.get("difficulty") or ""})
    return idx


def _enrich(runs: list[dict]) -> None:
    """Adds category, app and purpose to every run. The purpose is a person's mark when there is one,
    otherwise it follows from the pinned sets (see dashboard.purpose_of)."""
    idx = _problem_index()
    roles = pins.roles()
    marks = {m["run_id"]: m for m in store.read("marks") if m.get("run_id") and not m.get("deleted")}
    for r in runs:
        info = idx.get(r.get("problem_id") or "") or {}
        r["category"] = info.get("category") or "Uncategorized"
        r["app"] = info.get("app") or ""
        mark = marks.get(r.get("run_id"))
        r["purpose"], r["purpose_source"] = dashboard.purpose_of(r, mark, roles)
        r["mark_note"] = (mark or {}).get("note") or ""


@app.get("/api/results")
def results(limit: int = 300):
    """Backs the Results tab/dashboard: every recorded run, newest first, and who ran it."""
    source, runs = _collect_runs(limit)
    return {"source": source, "runs": runs}


@app.get("/api/experiments/summary")
def experiments_summary(limit: int = 5000):
    """One summary per experiment: solved / not solved per problem, time and tokens. Backs the Campaigns tab."""
    source, runs = _collect_runs(limit)
    return {"source": source, "experiments": queuesummary.summarize(runs)}


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


@app.get("/api/run/{run_id}")
def one_run(run_id: str):
    """One run with everything stored for it (diagnosis text, judge reasoning and checklist, tags, purpose)."""
    _, runs = _collect_runs(100000)
    r = next((x for x in runs if x.get("run_id") == run_id), None)
    if r is None:
        return JSONResponse({"error": "no such run"}, status_code=404)
    return r


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
        shared = firebase_sync.read_log(name)     # a teammate's run: the log was shared when it finished (keys masked)
        if shared:
            return PlainTextResponse(shared, media_type="text/plain; charset=utf-8")
        return PlainTextResponse(f"no such log file on this machine: {name}. No shared copy found either (logs are shared when Firebase is "
                                 "connected on the machine that ran it).", status_code=404)
    return FileResponse(log_path, media_type="text/plain; charset=utf-8")


@app.post("/api/logs/share-existing")
def share_existing_logs():
    """Shares the logs of runs already made on this machine (those made before log sharing existed, or while Firebase was off).
    Runs made here only; each log is one document write."""
    if not _log_mirror_on():
        return JSONResponse({"error": "Log sharing is turned off on this machine (RELIOPS_LOG_MIRROR=0 in the Lab's .env)."}, status_code=400)
    sent = skipped = 0
    for r in runner.list_local_runs(100000):
        if (r.get("log_file") or "") and _share_log(r):
            sent += 1
        else:
            skipped += 1
    return {"ok": True, "shared": sent, "skipped": skipped,
            "note": "Skipped runs have no log file on this machine, or Firebase is not connected."}


@app.get("/api/cluster")
def cluster_status():
    """Read-only health of the local kind cluster (see cluster.py): nodes, system pods, node containers, memory, disk."""
    return cluster.status(busy=jobs.busy())


class RepairIn(BaseModel):
    action: str
    confirm: bool = False


@app.post("/api/cluster/repair")
def cluster_repair(body: RepairIn):
    """Starts one of the fixed cluster repairs as the current job. The client sends only the repair's name; the
    commands are built on the server from names it reads itself, so this is not a way to run arbitrary commands.
    One job at a time: a run or queue in progress makes this refuse."""
    if body.action not in cluster.ACTION_IDS:
        return JSONResponse({"error": "unknown repair"}, status_code=400)
    if not body.confirm:
        return JSONResponse({"error": "this repair needs confirmation"}, status_code=400)
    titles = {"restart_nodes": "Restart the kind nodes", "clear_stuck_pods": "Clear stuck system pods", "rebuild": "Rebuild the cluster"}
    action = body.action
    job = jobs.start("cluster", titles[action], lambda: cluster.repair_lines(action), {"action": action})
    if job is None:
        return JSONResponse({"error": "A run, queue or other job is running. Wait for it or stop it first."}, status_code=409)
    return {"ok": True, "job_id": job.id}


@app.get("/api/setup")
def setup():
    return _sse(runner.stream_setup(), "setup")


@app.get("/api/demo")
def demo():
    return _sse(runner.stream_demo(), "demo")


def _log_mirror_on() -> bool:
    return str(runner.merged_env_vars().get("RELIOPS_LOG_MIRROR") or os.environ.get("RELIOPS_LOG_MIRROR") or "1").strip() not in ("0", "off", "false", "no")


def _share_log(record: dict) -> bool:
    """Shares this run's raw log with the other machines (logmirror.py). Best effort: the local log is never touched and a failure
    only means the log stays on this machine."""
    name = record.get("log_file") or ""
    if not name or "/" in name or "\\" in name or ".." in name or not _log_mirror_on():
        return False
    p = runner.reliops_dir() / "results" / "gui_logs" / name
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return firebase_sync.write_log(name, text, record.get("run_id") or "").startswith("written")


def _on_result(record: dict) -> None:
    """Called after every individual run with the record we could recover: share the story with
    teammates, mirror the row to Firestore, and append it to the local raw backup and the CSV."""
    # Share the story with teammates before the run record goes out, so the flag travels with it.
    try:
        record["story_shared"] = firebase_sync.write_story(record["run_id"], runner.render_story(record)).startswith("written")
    except (FileNotFoundError, ValueError, OSError):
        record["story_shared"] = False
    record["log_shared"] = _share_log(record)
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
               agent_switches: Optional[list] = None,
               manifest_id: Optional[str] = None, slice_k: int = 1, slice_n: int = 1,
               cells: Optional[list[tuple[str, int]]] = None, purpose: Optional[str] = None,
               experiment: Optional[str] = None, origin: Optional[str] = None):
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
            cell_list = campaign.slice_cells(todo, slice_k, slice_n, manifest)
        except ValueError as e:
            yield f"{e}\n"
            return
        if cells is not None:                      # a queue step on one machine share: only those cells
            wanted = {tuple(c) for c in cells}
            cell_list = [c for c in cell_list if tuple(c) in wanted]
        run_class = "frozen"
        repeat_n = manifest["repeat_n"]
        if not cell_list:
            yield "nothing left to run for this slice: every cell has a result\n"
            return
    elif run_class == "frozen":
        yield ("A frozen run needs a manifest: freeze one in the Campaigns tab first, so the settings "
               "it was measured with are recorded and enforced.\n")
        return

    def on_result(record: dict) -> None:
        _on_result(record)
        if purpose in dashboard.PURPOSES:       # the person said what this run is for; record it as a mark
            store.append("marks", {"run_id": record.get("run_id"), "purpose": purpose, "note": "set when the run was started",
                                   "marked_by": run_by or "unknown"})

    yield from runner.stream_campaign(
        agent=agent, action_space=action_space, model=model, judge_model=judge_model, problems=problem_list,
        repeat_n=repeat_n, force_build=force_build, run_by=run_by, config_label=config_label, run_class=run_class,
        record_walkthrough=record_walkthrough, cells=cell_list, manifest_id=manifest_id, experiment=experiment,
        agent_switches=agent_switches, on_result=on_result, origin=origin,
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
    purpose: Optional[str] = None,  # experiment | dev | final; empty = decided automatically from the pinned sets
    experiment: Optional[str] = None,  # the person's name for what this run belongs to (optional for a quick run)
    agent_switches: Optional[str] = None,  # comma-separated switch names (reliops_v2 only), e.g. from a ready-made variant
):
    problem_list = [p.strip() for p in problems.split(",") if p.strip()]
    try:
        switch_list = runner.clean_switches((agent_switches or "").split(","))
    except ValueError as e:
        return _refusal([f"{e}\n"])
    cell_list = None
    if cells:
        try:
            cell_list = [(c.rsplit(":", 1)[0], int(c.rsplit(":", 1)[1])) for c in cells.split(",") if c.strip()]
        except (ValueError, IndexError):
            return _refusal(["invalid cells list, expected problem:repeat,problem:repeat\n"])
    kwargs = dict(agent=agent, model=model, judge_model=judge_model, problems=problem_list, action_space=action_space,
                  force_build=force_build, repeat_n=repeat_n, run_by=run_by, config_label=config_label,
                  run_class=run_class, record_walkthrough=record_walkthrough, manifest_id=manifest_id,
                  slice_k=slice_k, slice_n=slice_n, cells=cell_list, purpose=purpose or None,
                  experiment=(experiment or "").strip() or None, agent_switches=switch_list or None, origin="quick")
    return _job_sse(lambda: _run_lines(**kwargs), "run", f"{agent} · {len(problem_list)} problem(s)")


# ---- Job state, saved problem sets, run queues ----------------------------------------------
@app.get("/api/state")
def state():
    """What the server is doing right now. Polled by the header so a run started in another tab (or
    before a page reload) is always visible, with a way to stop it."""
    job = jobs.current()
    return {"busy": jobs.busy(), "job": job.describe() if job else None, "key_alerts": _key_alerts()}


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


def _with_pins(sets: list[dict]) -> list[dict]:
    """Tags the sets that are pinned for the phase: role (frozen / dev), and whether the file still
    matches what was pinned."""
    by_set = {p["set_id"]: p for p in pins.load()}
    out = []
    for s in sets:
        p = by_set.get(s["id"])
        if p:
            tag = {"frozen": "frozen set", "dev": "dev set"}[p["role"]]
            warn = "" if p["status"] == "ok" else " — CHANGED since pinned" if p["status"] == "changed" else " — file missing"
            s = {**s, "pin": {"role": p["role"], "status": p["status"], "pinned_on": p["pinned_on"], "hash": p["pinned_hash"]},
                 "label": f"{s['label']} · pinned {tag}{warn}"}
        out.append(s)
    return out


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
    machines: int = 1          # how many machines share this queue (1 = one machine runs it all)
    shares: Optional[list] = None   # optional hand-made problem lists, one per machine
    split: str = "problem"          # shares made of problems, or of (problem, repeat) pairs
    order: str = "step"             # run step by step, or every step of one (problem, repeat) back to back
    experiment: str = ""       # only used by the preview: the name typed in the run bar
    share: Optional[int] = None     # only used by the preview: the machine share picked on this machine
    layout_edited: bool = True      # False = this page did not change machines/shares/split/order, so a save must keep the stored ones


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
    raw = body.model_dump()
    # A page that opened the queue earlier still holds the old shares. If it did not edit them itself, saving (Run, Continue,
    # Restart) must not put them back over a redistribution made meanwhile: keep the stored layout.
    if not body.layout_edited and body.queue_id:
        stored = _find_queue(body.queue_id)
        if stored:
            for k in ("machines", "shares", "split", "order"):
                raw[k] = stored.get(k)
    try:
        q = queueplan.clean_queue(raw)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "queue": store.append("queues", q)}


def _delete_password() -> str:
    """The password that unlocks queue deletion on this machine: LAB_DELETE_PASSWORD in the Lab's .env (or the shell).
    Read on every call, so setting or changing it needs no restart. Empty means deletion is locked."""
    env = runner.merged_env_vars()
    return str(env.get("LAB_DELETE_PASSWORD") or os.environ.get("LAB_DELETE_PASSWORD") or "").strip()


@app.get("/api/queue/delete-lock")
def queue_delete_lock():
    """Tells the page whether this machine has a delete password set. The password itself never leaves the server."""
    return {"locked_off": not _delete_password()}


@app.delete("/api/queue/{queue_id}")
def delete_queue(queue_id: str, x_delete_password: Optional[str] = Header(default=None)):
    """Deleting a saved queue needs the password. It is checked here, not only in the page, and the deletion is mirrored to
    the other machines, so a teammate who does not know the password cannot remove a queue the whole team uses."""
    import hmac
    import time
    secret = _delete_password()
    if not secret:
        return JSONResponse({"error": "Deleting queues is locked on this machine. Set LAB_DELETE_PASSWORD in the Lab's .env "
                                      "to unlock it (see docs/GUIDE.md)."}, status_code=403)
    if not hmac.compare_digest((x_delete_password or "").encode(), secret.encode()):
        time.sleep(1.0)    # slows down guessing
        return JSONResponse({"error": "Wrong password. The queue was not deleted."}, status_code=403)
    q = _find_queue(queue_id)
    if q is None:
        return JSONResponse({"error": "unknown queue"}, status_code=404)
    store.append("queues", {**q, "deleted": True})
    return {"ok": True}


# ---- Screening report ---------------------------------------------------------------
@app.get("/api/screening")
def screening_report(experiment: str = "", queue_id: str = "", base: str = screening.DEFAULT_BASE,
                     all_label: str = Query(screening.DEFAULT_ALL, alias="all"), metric: str = "e2e_pass", exclude_commits: str = "", include_null: bool = False, format: str = "json"):
    """The switch-ladder screening of one experiment (screening.py): per-configuration rates, the 16 paired contrasts with Holm,
    a verdict per switch. It counts what the queue counts: the newest round of the experiment name, with any "Restart my share"
    applied, and never a quick run. Problems the null agent passes are left out unless include_null is set."""
    name = (experiment or "").strip()
    if not name:
        return JSONResponse({"error": "Give the experiment name: the report is made from the runs recorded under it."}, status_code=400)
    _, runs = _collect_runs(5000)
    label = queueplan.experiment_label(name, runs)
    q = _find_queue(queue_id) if queue_id else None
    mine = [r for r in runs if (r.get("experiment") or "") == label and r.get("origin") != "quick"]
    if q is not None:
        mine = queueplan.apply_resets(q, mine, _resets(), label)
    expected = None
    if q is not None:
        in_design = lambda lab: lab in (base, all_label) or lab.startswith(base + " +") or lab.startswith(all_label + " -")     # noqa: E731
        expected = {(p, rep) for st in q["steps"] if st["enabled"] and in_design(st["config_label"] or "")
                    for p in st["problems"] for rep in range(1, int(st["repeat_n"]) + 1)} or None
    null_problems = set() if include_null else {r["problem_id"] for r in runs if "NULL_AGENT" in (r.get("agent_switches") or "") and r.get("e2e_pass") is True and r.get("problem_id")}
    ladder_runs = [r for r in mine if "NULL_AGENT" not in (r.get("agent_switches") or "")]
    try:
        rep = screening.screen(ladder_runs, base=base, all_label=all_label, metric=metric, expected=expected, excluded_problems=null_problems,
                               exclude_commits={c.strip() for c in exclude_commits.split(",") if c.strip()})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    rep["experiment"] = label
    rep["runs_used"] = len(ladder_runs)
    rep["people"] = sorted({r.get("run_by") or "?" for r in ladder_runs})
    rep["commits"] = sorted({(r.get("git_commit") or "?")[:8] for r in ladder_runs})
    if format == "md":
        return PlainTextResponse(screening.to_markdown(rep, f"Screening report: {label}"), media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename=screening_report.md"})
    if format in ("csv", "csv-configs"):
        return PlainTextResponse(screening.configs_csv(rep), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename=screening_configs.csv"})
    if format == "csv-contrasts":
        return PlainTextResponse(screening.contrasts_csv(rep), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": "attachment; filename=screening_contrasts.csv"})
    return rep


# ---- Tasks tab ----------------------------------------------------------------------
def _owner_password() -> str:
    """LAB_OWNER_PASSWORD in the Lab's .env (or the shell): the password that lets this Lab add, edit and delete tasks.
    Read on every call, so no restart is needed. Empty means task editing is locked on this machine."""
    env = runner.merged_env_vars()
    return str(env.get("LAB_OWNER_PASSWORD") or os.environ.get("LAB_OWNER_PASSWORD") or "").strip()


def _owner_gate(password: Optional[str], what: str = "Adding tasks"):
    """None when the password is right; otherwise the error response. Checked here, not only in the page."""
    import hmac
    import time
    secret = _owner_password()
    if not secret:
        return JSONResponse({"error": f"{what} is locked on this machine. Set LAB_OWNER_PASSWORD in the Lab's .env to unlock it "
                                       "(see docs/GUIDE.md)."}, status_code=403)
    if not hmac.compare_digest((password or "").encode(), secret.encode()):
        time.sleep(1.0)    # slows down guessing
        return JSONResponse({"error": "Wrong owner password."}, status_code=403)
    return None


class TaskIn(BaseModel):
    title: str = ""
    detail: str = ""
    phase: str = ""
    assignees: list[str] = []
    queue_id: str = ""
    experiment: str = ""
    due: str = ""
    run_by: Optional[str] = None


class TaskProgressIn(BaseModel):
    who: str = ""
    status: str = "doing"
    note: str = ""


@app.get("/api/tasks/lock")
def tasks_lock():
    """Whether this machine has an owner password set. The password itself never leaves the server."""
    return {"locked_off": not _owner_password()}


@app.post("/api/tasks/unlock")
def tasks_unlock(x_owner_password: Optional[str] = Header(default=None)):
    bad = _owner_gate(x_owner_password)
    return bad if bad else {"ok": True}


@app.get("/api/tasks")
def tasks_list():
    tasks = [t for t in store.read("tasks") if not t.get("deleted")]
    tasks.sort(key=lambda t: (t.get("phase") or "~", t.get("created_at") or t.get("updated_at") or ""))
    ids = {t["task_id"] for t in tasks}
    return {"tasks": tasks, "progress": [p for p in store.read("task_progress") if p.get("task_id") in ids],
            "locked_off": not _owner_password()}


@app.post("/api/tasks")
def tasks_create(body: TaskIn, x_owner_password: Optional[str] = Header(default=None)):
    bad = _owner_gate(x_owner_password)
    if bad:
        return bad
    try:
        t = tasks_mod.clean_task(body.model_dump(), created_by=body.run_by or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    import time as _t
    return {"ok": True, "task": store.append("tasks", {**t, "created_at": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())})}


@app.put("/api/tasks/{task_id}")
def tasks_edit(task_id: str, body: TaskIn, x_owner_password: Optional[str] = Header(default=None)):
    bad = _owner_gate(x_owner_password)
    if bad:
        return bad
    old = next((t for t in store.read("tasks") if t["task_id"] == task_id and not t.get("deleted")), None)
    if old is None:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    try:
        t = tasks_mod.clean_task(body.model_dump(), task_id=task_id, created_by=old.get("created_by") or body.run_by or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "task": store.append("tasks", {**t, "created_at": old.get("created_at", "")})}


@app.delete("/api/tasks/{task_id}")
def tasks_delete(task_id: str, x_owner_password: Optional[str] = Header(default=None)):
    bad = _owner_gate(x_owner_password)
    if bad:
        return bad
    old = next((t for t in store.read("tasks") if t["task_id"] == task_id and not t.get("deleted")), None)
    if old is None:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    store.append("tasks", {**old, "deleted": True})
    return {"ok": True}


@app.post("/api/tasks/{task_id}/progress")
def tasks_progress(task_id: str, body: TaskProgressIn):
    """Anyone can say where they are on a task that is for them (todo, doing, done, blocked, with a note)."""
    t = next((t for t in store.read("tasks") if t["task_id"] == task_id and not t.get("deleted")), None)
    if t is None:
        return JSONResponse({"error": "unknown task"}, status_code=404)
    try:
        rec = tasks_mod.clean_progress(t, body.who, body.status, body.note)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "progress": store.append("task_progress", rec)}


_head_cache: dict = {"at": 0.0, "sha": ""}


def _this_commit() -> str:
    """Short commit of this machine's ReliOps clone, the same one runs are stamped with. Cached for 20 s."""
    import time
    if time.time() - _head_cache["at"] > 20:
        _head_cache.update(at=time.time(), sha=(runner._git_commit_safe() or "")[:8])
    return _head_cache["sha"]


@app.get("/api/queue/preview")
def queue_preview(queue_id: str, experiment: Optional[str] = None, share: Optional[int] = None):
    q = _find_queue(queue_id)
    if q is None:
        return JSONResponse({"error": "unknown queue"}, status_code=404)
    _, runs = _collect_runs(5000)
    try:
        return {**queueplan.preview(q, runs, store.read("manifests"), experiment, share, claims.for_label, resets=_resets(), assigned=_assigned(queue_id)), "this_commit": _this_commit()}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/queue/preview")
def queue_preview_draft(body: QueueIn):
    """Same as the GET preview but for a queue that has not been saved yet, so the screen can show
    live cell counts while a queue is being edited."""
    draft = body.model_dump()
    draft["name"] = draft["name"].strip() or "draft"
    try:
        q = queueplan.clean_queue(draft)
        _, runs = _collect_runs(5000)
        return {**queueplan.preview(q, runs, store.read("manifests"), body.experiment, body.share, claims.for_label, resets=_resets(), assigned=_assigned(q.get("queue_id"))), "this_commit": _this_commit()}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


def _assigned(queue_id) -> dict:
    """{share: [names]}: who each share of this queue is planned for. Best effort: a failed read means nobody assigned."""
    if not queue_id:
        return {}
    try:
        return {int(a.get("share") or 0): [str(n) for n in (a.get("names") or [])]
                for a in store.read("assignments") if a.get("queue_id") == queue_id and not a.get("deleted")}
    except Exception:  # noqa: BLE001
        return {}


def _resets() -> list:
    """"Restart my share" markers (queueplan.apply_resets). Best effort: a failed read means no resets."""
    try:
        return store.read("share_resets")
    except Exception:  # noqa: BLE001
        return []


def _queue_runs(queue: dict, label: str) -> list:
    """Recorded runs as the queue counts them: runs of a share that was restarted under this label do not count."""
    _, runs = _collect_runs(5000)
    return queueplan.apply_resets(queue, runs, _resets(), label)


def _runs_left(queue: dict, label: str, runs: Optional[list] = None, share: Optional[int] = None) -> int:
    """Cells the queue still has to run under this experiment label (enabled steps only; this machine's share only,
    when one is picked)."""
    only = queueplan.share_set(queue, share)
    manifests = store.read("manifests")
    if runs is None:
        runs = _queue_runs(queue, label)
    n = 0
    for step in queue["steps"]:
        if not step.get("enabled", True):
            continue
        manifest = next((m for m in manifests if m.get("manifest_id") == step.get("manifest_id")), None) if step.get("manifest_id") else None
        if step.get("manifest_id") and manifest is None:
            continue
        n += len(queueplan.todo(step, runs, manifest, label, only))
    return n


def _queue_lines(queue: dict, run_by: Optional[str], experiment: str, restart: bool = False, share: Optional[int] = None):
    """Runs the queue and remembers how it ended (queuestate), so the Continue button can pick up from there."""
    queuestate.begin(queue, experiment, run_by, share)
    outcome: dict = {"status": "interrupted"}
    tracker = claims.Tracker(queue, run_by, share)       # the shared "who took which share" board; best effort
    try:
        for line in _queue_steps(queue, run_by, experiment, restart, outcome, share, tracker):
            tracker.beat(line)
            yield line
    finally:
        queuestate.end(**outcome)
        tracker.finish(outcome)


def _queue_steps(queue: dict, run_by: Optional[str], experiment: str, restart: bool, outcome: dict,
                 share: Optional[int] = None, tracker: Optional[claims.Tracker] = None):
    steps = queue["steps"]
    only = queueplan.share_set(queue, share)
    manifests = store.read("manifests")
    runner._stop_event.clear()
    _, all_runs = _collect_runs(5000)
    label = queueplan.experiment_label(experiment, all_runs, restart=restart)
    if tracker is not None:
        busy = tracker.start(label)
        if busy:
            yield busy
    yield f"##### [QUEUE] {queue['name']}: {len(steps)} step(s) #####\n"
    yield f"##### [QUEUE] experiment: {label}{' (restarted from scratch)' if restart else ''} #####\n"
    if only is not None:
        mine = [p for p in queueplan.share_lists(queue)[share - 1]]
        what = "problem-repeat pair(s)" if queue.get("split") == "repeat" else "problem(s)"
        yield (f"##### [QUEUE] this machine runs share {share} of {queue.get('machines')}: {len(mine)} {what}, "
               f"{', '.join(mine)}. The other shares are left to the other machines. #####\n")

    def paused(i: int, step_name: str):
        left = _runs_left(queue, label, share=share)
        outcome.update(status="paused" if left else "finished", step=i, step_name=step_name, left=left)
        return (f"\n##### [QUEUE] paused after the current run, in step {i}/{len(steps)} ({step_name}); {left} run(s) left under \"{label}\". "
                "Every finished run is recorded. Press Continue to carry on from here. #####\n") if left else \
               "\n##### [QUEUE] finished #####\n"

    if queue.get("order") == "cell":
        yield from _queue_by_cell(queue, steps, only, manifests, label, run_by, outcome, paused, share)
        return

    for i, step in enumerate(steps, start=1):
        if runner._stop_event.is_set():
            yield paused(i, step["name"])
            return
        if not step.get("enabled", True):
            yield f"##### [QUEUE] step {i}/{len(steps)} skipped (switched off): {step['name']} #####\n"
            continue
        manifest = next((m for m in manifests if m.get("manifest_id") == step.get("manifest_id")), None) \
            if step.get("manifest_id") else None
        if step.get("manifest_id") and manifest is None:
            yield f"##### [QUEUE] step {i}/{len(steps)} cannot run: frozen campaign {step['manifest_id']} not found #####\n"
            continue
        runs = _queue_runs(queue, label)
        cell_list = queueplan.todo(step, runs, manifest, label, only)
        yield f"\n##### [QUEUE] step {i}/{len(steps)} start: {step['name']} | runs {len(cell_list)} to do #####\n"
        if not cell_list:
            yield (f"##### [QUEUE] step {i}/{len(steps)} nothing to do: every cell "
                   f"{'of this machine’s share ' if only is not None else ''}already has a result #####\n")
            continue
        if runner._stop_event.is_set():      # Pause pressed while the step was being prepared
            yield paused(i, step["name"])
            return
        yield f"[step] agent image: {'force-build on the first run' if step.get('force_build') else 'use the existing image (force-build is off for this step)'}\n"
        yield from _run_lines(
            agent=step["agent_id"], model=step["model_id"], judge_model=step["judge_model_id"],
            problems=step["problems"], action_space=step.get("action_space"), force_build=bool(step.get("force_build")),
            repeat_n=step["repeat_n"], run_by=run_by, config_label=step.get("config_label") or None,
            run_class=step["run_class"], record_walkthrough=bool(step.get("record_walkthrough", True)),
            agent_switches=step.get("agent_switches") or None,
            manifest_id=step.get("manifest_id"),
            cells=cell_list if (only is not None or not step.get("manifest_id")) else None,
            purpose=step.get("purpose") or None, experiment=label, origin="queue")
        yield f"##### [QUEUE] step {i}/{len(steps)} end #####\n"
        halted = runner.halt_reason()
        if halted:
            # The step stopped itself because the setup looks broken; the later steps would hit the same thing.
            left = len(steps) - i
            outcome.update(status="halted", step=i, step_name=step["name"], reason=halted, left=_runs_left(queue, label, share=share))
            yield (f"\n##### [QUEUE] stopped after step {i}/{len(steps)} ({halted}); {left} later step(s) were not started. "
                   "Fix the cause, then press Continue: finished runs are kept and only missing cells run. #####\n")
            return
        if runner._stop_event.is_set():
            yield paused(i, step["name"])
            return
    outcome.update(status="finished", left=0)
    yield "\n##### [QUEUE] finished #####\n"


def _queue_by_cell(queue: dict, steps: list, only, manifests: list, label: str, run_by: Optional[str], outcome: dict,
                   paused, share: Optional[int]):
    """Cell order: for each (problem, repeat) of this machine's share, every step that still needs it, back to back.
    The configurations of one pair then run close together on the same machine, so a slow hour of the model service
    or a tired cluster falls on all of them alike instead of on whichever step happened to run then. Each pair starts
    one step later than the one before, so no configuration is always the first (or last) of its block."""
    runs = _queue_runs(queue, label)
    plan = []
    for i, step in enumerate(steps, start=1):
        if not step.get("enabled", True):
            yield f"##### [QUEUE] step {i}/{len(steps)} skipped (switched off): {step['name']} #####\n"
            continue
        manifest = next((m for m in manifests if m.get("manifest_id") == step.get("manifest_id")), None) \
            if step.get("manifest_id") else None
        if step.get("manifest_id") and manifest is None:
            yield f"##### [QUEUE] step {i}/{len(steps)} cannot run: frozen campaign {step['manifest_id']} not found #####\n"
            continue
        plan.append((i, step, set(queueplan.todo(step, runs, manifest, label, only))))
    pos = {p: n for n, p in enumerate(queueplan.queue_problems(queue))}
    pairs = sorted({c for _, _, t in plan for c in t}, key=lambda c: (c[1], pos.get(c[0], 0)))
    total = sum(len(t) for _, _, t in plan)
    yield (f"##### [QUEUE] cell order: {len(pairs)} problem-repeat pair(s), every step of a pair back to back; "
           f"{total} run(s) to do #####\n")
    built = False
    for n, (problem, rep) in enumerate(pairs, start=1):
        rot = (n - 1) % len(plan) if plan else 0          # each pair starts one step later, so no step is always first
        for i, step, todo in plan[rot:] + plan[:rot]:
            if (problem, rep) not in todo:
                continue
            if runner._stop_event.is_set():
                yield paused(i, step["name"])
                return
            force = any(st.get("force_build") for _, st, _ in plan) and not built
            built = built or force
            yield f"\n##### [QUEUE] step {i}/{len(steps)} start: {step['name']} | runs 1 to do #####\n"
            yield f"[cell] {problem}, repeat {rep} (pair {n} of {len(pairs)})\n"
            yield from _run_lines(
                agent=step["agent_id"], model=step["model_id"], judge_model=step["judge_model_id"],
                problems=step["problems"], action_space=step.get("action_space"), force_build=force,
                repeat_n=step["repeat_n"], run_by=run_by, config_label=step.get("config_label") or None,
                run_class=step["run_class"], record_walkthrough=bool(step.get("record_walkthrough", True)),
                agent_switches=step.get("agent_switches") or None, manifest_id=step.get("manifest_id"),
                cells=[(problem, rep)], purpose=step.get("purpose") or None, experiment=label, origin="queue")
            yield f"##### [QUEUE] step {i}/{len(steps)} end #####\n"
            halted = runner.halt_reason()
            if halted:
                outcome.update(status="halted", step=i, step_name=step["name"], reason=halted,
                               left=_runs_left(queue, label, share=share))
                yield (f"\n##### [QUEUE] stopped at {problem}, repeat {rep}, step {i}/{len(steps)} ({halted}). "
                       "Fix the cause, then press Continue: finished runs are kept and only missing cells run. #####\n")
                return
            if runner._stop_event.is_set():
                yield paused(i, step["name"])
                return
    outcome.update(status="finished", left=0)
    yield "\n##### [QUEUE] finished #####\n"


@app.get("/api/queue/state")
def queue_state():
    """Where the queue stands for the Continue button: paused, stopped by itself, cut off, or nothing to continue."""
    st = queuestate.describe(jobs.busy())
    if st and st.get("status") in queuestate.RESUMABLE:
        q = _find_queue(st["queue_id"])
        if q is None:
            return {"state": {**st, "can_continue": False, "problem": "The queue was deleted, so it cannot be continued."}}
        try:
            st["left"] = _runs_left(q, queueplan.experiment_label(st["experiment"], _collect_runs(5000)[1]), share=st.get("share"))
        except Exception:  # noqa: BLE001 - the stored count is still shown
            pass
        if st["left"] == 0:
            st["can_continue"] = False
    return {"state": st}


@app.get("/api/queue/continue")
def queue_continue(run_by: Optional[str] = None):
    """Carries on the queue that was paused, stopped by itself or cut off: same queue, same experiment name, finished runs kept."""
    st = queuestate.describe(jobs.busy())
    if not st or st["status"] not in queuestate.RESUMABLE:
        return _refusal(["Nothing to continue: the last queue finished, or none has run on this machine.\n"])
    q = _find_queue(st["queue_id"])
    if q is None:
        return _refusal(["The queue that was running has been deleted, so it cannot be continued.\n"])
    exp = st["experiment"]
    share = st.get("share") or None
    try:
        queueplan.share_set(q, share)
    except ValueError:
        return _refusal([f"The queue now has fewer machine shares than the share this machine was running ({share}). "
                         "Pick a share and press Run queue.\n"])
    return _job_sse(lambda: _queue_lines(q, run_by or st.get("run_by") or None, exp, False, share), "queue", q["name"],
                    {"queue_id": q["queue_id"], "experiment": exp, "share": share})


@app.get("/api/queue/run")
def queue_run(queue_id: str, run_by: Optional[str] = None, experiment: str = "", restart: bool = False,
              share: Optional[int] = None):
    q = _find_queue(queue_id)
    if q is None:
        return _refusal(["unknown queue\n"])
    if not q.get("steps"):
        return _refusal(["this queue has no steps\n"])
    experiment = (experiment or "").strip()
    if not experiment:
        return _refusal(["Give this experiment a name first (the box next to Run queue). Every run is recorded under it.\n"])
    share = share or None
    try:
        queueplan.share_set(q, share)
    except ValueError as e:
        return _refusal([f"{e}\n"])
    return _job_sse(lambda: _queue_lines(q, run_by, experiment, restart, share), "queue", q["name"],
                    {"queue_id": queue_id, "experiment": experiment, "share": share})


class RestartShareIn(BaseModel):
    experiment: str
    share: int
    run_by: Optional[str] = None


@app.post("/api/queue/{queue_id}/restart-share")
def queue_restart_share(queue_id: str, body: RestartShareIn):
    """Restart ONE machine share of a shared queue under its experiment name. Nothing is deleted: a reset marker is stored
    (shared with the other machines) and the queue and the board ignore that share's earlier runs under this label, so its
    cells run again. Other shares and other experiments are not touched."""
    q = _find_queue(queue_id)
    if q is None:
        return JSONResponse({"error": "unknown queue"}, status_code=404)
    n = int(q.get("machines") or 1)
    if n <= 1:
        return JSONResponse({"error": "This queue is not split across machines; use Restart."}, status_code=400)
    try:
        queueplan.share_set(q, body.share)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    base = (body.experiment or "").strip()
    if not base:
        return JSONResponse({"error": "Give the experiment a name first."}, status_code=400)
    if jobs.busy():
        return JSONResponse({"error": "A job is running on this machine. Pause it first, then restart the share."}, status_code=409)
    _, runs = _collect_runs(5000)
    label = queueplan.experiment_label(base, runs)
    lists = queueplan.share_lists(q)
    before = [r for r in queueplan.apply_resets(q, runs, _resets(), label)
              if (r.get("experiment") or "") == label
              and queueplan.share_of(q, r.get("problem_id") or "?", int(r.get("repeat_index") or 1), lists) == body.share]
    if not before:
        return JSONResponse({"error": f"Nothing is recorded in share {body.share} under \"{label}\"; use Run queue."}, status_code=400)
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rec = store.append("share_resets", {"experiment": label, "share": body.share, "at": at, "queue_id": queue_id,
                                        "queue_name": q.get("name"), "run_by": body.run_by or "", "machine": claims.machine_name(),
                                        "runs_set_aside": len(before)})
    return {"ok": True, "experiment": label, "share": body.share, "at": at, "runs_set_aside": len(before),
            "shared": rec.get("_firebase")}


class AssignIn(BaseModel):
    share: int
    names: list[str] = []
    run_by: Optional[str] = None


class MoveIn(BaseModel):
    experiment: str = ""
    units: list[str] = []         # problems, or "problem #r" pairs, to move
    to_share: int
    run_by: Optional[str] = None


class ReassignIn(BaseModel):
    experiment: str = ""
    moves: list[dict] = []        # [{"unit": "problem" or "problem #r", "to_share": 2}, ...]
    run_by: Optional[str] = None


class RedistributeIn(BaseModel):
    experiment: str = ""
    from_share: int
    to_shares: list[int] = []     # empty = every other share
    run_by: Optional[str] = None


@app.post("/api/queue/{queue_id}/assign")
def queue_assign(queue_id: str, body: AssignIn):
    """Plan who runs a share. Names are free text (people typed in the header, or a name you type). It is a plan, not a
    lock: the screen warns when someone else starts the share."""
    q = _find_queue(queue_id)
    if q is None:
        return JSONResponse({"error": "unknown queue"}, status_code=404)
    try:
        queueplan._check_share(q, body.share)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    names = [n.strip() for n in body.names if n and n.strip()]
    if len(set(x.lower() for x in names)) != len(names):
        return JSONResponse({"error": "a name appears twice"}, status_code=400)
    rec = store.append("assignments", {"queue_id": queue_id, "share": body.share, "names": names,
                                       "assigned_by": body.run_by or "", "deleted": not names})
    return {"ok": True, "share": body.share, "names": names, "shared": rec.get("_firebase")}


def _share_move_response(q: dict, lists: list, moved: list, experiment: str, also: tuple = ()):
    try:
        new_q = queueplan.clean_queue({**q, "shares": lists})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    saved = store.append("queues", new_q)
    warnings = []
    label = queueplan.experiment_label((experiment or "").strip(), _collect_runs(5000)[1]) if (experiment or "").strip() else ""
    taken = claims.for_label(label) if label else []
    touched = sorted({k for _, k in moved} | set(also))
    import time as _t
    for k in touched:
        c = claims.claim_for(taken, k)
        if c and c.get("status") == "running":
            seen = claims._epoch(c.get("updated_at"))
            if seen is not None and _t.time() - seen < claims.STALE_S:
                warnings.append(f"{c.get('run_by') or 'someone'} is running share {k} right now. A running machine keeps the shares it "
                                f"started with until it presses Continue or Run queue again, so tell them.")
    return {"ok": True, "queue": saved, "moved": [{"unit": u, "to": t} for u, t in moved], "warnings": warnings,
            "note": "The change is saved in the shared queue. Each machine sees it when it opens the queue again; a machine that is "
                    "already running keeps its old share until it presses Continue."}


@app.post("/api/queue/{queue_id}/move")
def queue_move(queue_id: str, body: MoveIn):
    """Move chosen problems (or problem #repeat pairs) to another share. Runs already recorded stay recorded."""
    q = _find_queue(queue_id)
    if q is None:
        return JSONResponse({"error": "unknown queue"}, status_code=404)
    try:
        lists = queueplan.move_units(q, body.units, body.to_share)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return _share_move_response(q, lists, [(u, body.to_share) for u in body.units], body.experiment)


@app.post("/api/queue/{queue_id}/redistribute")
def queue_redistribute(queue_id: str, body: RedistributeIn):
    """Take the work of one share that is not done yet and deal it out to other shares (all of them, or the ones chosen)."""
    q = _find_queue(queue_id)
    if q is None:
        return JSONResponse({"error": "unknown queue"}, status_code=404)
    if not (body.experiment or "").strip():
        return JSONResponse({"error": "Type the experiment name first: what is left is counted under it."}, status_code=400)
    _, runs = _collect_runs(5000)
    movable = queueplan.undone_runs(q, runs, store.read("manifests"), body.experiment, resets=_resets())
    try:
        lists, moved = queueplan.redistribute(q, body.from_share, body.to_shares or None, movable)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return _share_move_response(q, lists, moved, body.experiment, (body.from_share,))


@app.post("/api/queue/{queue_id}/reassign")
def queue_reassign(queue_id: str, body: ReassignIn):
    """You choose, problem by problem, which share each one goes to (the Redistribute dialog sends exactly this). Only
    problems that still have runs to do can move."""
    q = _find_queue(queue_id)
    if q is None:
        return JSONResponse({"error": "unknown queue"}, status_code=404)
    if not (body.experiment or "").strip():
        return JSONResponse({"error": "Type the experiment name first: what is left is counted under it."}, status_code=400)
    try:
        moves = [(str(m.get("unit", "")).strip(), int(m.get("to_share"))) for m in body.moves]
    except (TypeError, ValueError):
        return JSONResponse({"error": "each move needs a unit and a share number"}, status_code=400)
    _, runs = _collect_runs(5000)
    movable = queueplan.undone_units(q, runs, store.read("manifests"), body.experiment, resets=_resets())
    try:
        lists, moved = queueplan.reassign(q, moves, movable)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    where = {u: i + 1 for i, x in enumerate(queueplan.share_lists(q)) for u in x}
    return _share_move_response(q, lists, moved, body.experiment, tuple(sorted({where[u] for u, _ in moved if u in where})))


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
                "fix-didnt-hold", "tool-error", "timeout", "invented-incident", "external-fault", "other"]


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


# ---- Assigned judge audit (frozen blind sample, named labelers) --------------------------------
class AuditDrawIn(BaseModel):
    name: str
    labelers: list[str]
    groups: str = audit_plan.DEFAULT_GROUPS
    seed: int = 20261112
    anchors_per_stratum: int = 3
    max_per_problem: int = 2
    preview: bool = False
    by: str = ""


class AuditLabelIn(BaseModel):
    audit_id: str
    item_id: str
    labeler: str
    d1: Optional[bool] = None
    d2: Optional[bool] = None
    d3: Optional[bool] = None
    confidence: Optional[int] = None
    reason: str = ""


def _audit_plan(audit_id: str):
    return next((p for p in store.read("audit_plans") if p.get("audit_id") == audit_id), None)


@app.get("/api/audit/configs")
def audit_configs():
    """Eligible runs per configuration, so the owner can see which prefixes to put in the draw."""
    _, runs = _collect_runs(100000)
    counts: dict = {}
    for r in audit_plan.eligible(runs):
        c = counts.setdefault(r.get("config_label") or "", {"config_label": r.get("config_label") or "", "eligible": 0, "judged_pass": 0})
        c["eligible"] += 1
        c["judged_pass"] += 1 if r["diagnosis_pass"] else 0
    names = sorted({r.get("run_by") for r in runs if r.get("run_by") and r.get("run_by") != "unknown"})
    return {"configs": sorted(counts.values(), key=lambda c: c["config_label"]), "people": names,
            "default_groups": audit_plan.DEFAULT_GROUPS}


@app.get("/api/audit/plans")
def audit_plans():
    out = []
    for p in store.read("audit_plans"):
        out.append({"audit_id": p["audit_id"], "name": p.get("name"), "labelers": p.get("labelers"), "created_by": p.get("created_by"),
                    "created_at": p.get("created_at"), "items": len(p.get("items") or []),
                    "progress": audit_plan.progress(p, [l for l in store.read("audit_labels") if l.get("audit_id") == p["audit_id"]])})
    out.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    return {"plans": out}


@app.post("/api/audit/draw")
def audit_draw(body: AuditDrawIn, x_owner_password: Optional[str] = Header(default=None)):
    bad = _owner_gate(x_owner_password, "Drawing an audit sample")
    if bad:
        return bad
    audit_id = audit_plan.slug(body.name)
    if not audit_id:
        return JSONResponse({"error": "Give the audit a name."}, status_code=400)
    try:
        groups = audit_plan.parse_groups(body.groups)
        _, runs = _collect_runs(100000)
        items, notes = audit_plan.draw(runs, groups, body.labelers, body.seed, body.anchors_per_stratum, body.max_per_problem)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if body.preview:
        return {"preview": True, "items": len(items), "notes": notes}
    existing = _audit_plan(audit_id)
    if existing and any(l.get("audit_id") == audit_id for l in store.read("audit_labels")):
        return JSONResponse({"error": "This audit already has labels, so its sample is frozen. Use another name."}, status_code=409)
    labelers = list(dict.fromkeys(x.strip() for x in body.labelers if x.strip()))
    plan = store.append("audit_plans", {"audit_id": audit_id, "name": body.name.strip(), "labelers": labelers, "seed": body.seed,
                                        "groups": body.groups, "anchors_per_stratum": body.anchors_per_stratum,
                                        "created_by": body.by.strip(),
                                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "notes": notes, "items": items})
    return {"ok": True, "audit_id": audit_id, "items": len(items), "notes": notes, "firebase": plan.get("_firebase")}


@app.get("/api/audit/mine")
def audit_mine(audit_id: str, labeler: str):
    """The labeler's own items: problem, what the card says a correct diagnosis must say, the diagnosis. Nothing else
    about the run (no configuration, no judge verdict, no other labeler's answer)."""
    plan = _audit_plan(audit_id)
    if not plan:
        return JSONResponse({"error": "No such audit."}, status_code=404)
    who = next((l for l in plan["labelers"] if l.casefold() == labeler.strip().casefold()), None)
    if not who:
        return JSONResponse({"error": f"{labeler!r} is not a labeler of this audit. Labelers: {', '.join(plan['labelers'])}."}, status_code=403)
    _, runs = _collect_runs(100000)
    by_run = {r["run_id"]: r for r in runs}
    mine_labels = {l["item_id"]: l for l in store.read("audit_labels") if l.get("audit_id") == audit_id and l.get("labeler") == who}
    items = []
    for it in plan["items"]:
        if who not in it["labelers"]:
            continue
        r = by_run.get(it["run_id"]) or {}
        card = runner.read_card(r.get("problem_id") or "")
        lab = mine_labels.get(it["item_id"])
        items.append({"item_id": it["item_id"], "problem_id": r.get("problem_id"), "diagnosis_text": r.get("diagnosis_text") or "(run not found on this machine)",
                      "expected": runner.card_section(card, 4), "done": bool(lab),
                      "mine": ({k: lab.get(k) for k in ("d1", "d2", "d3", "overall_correct", "confidence", "reason")} if lab else None)})
    return {"audit_id": audit_id, "labeler": who, "items": items, "done": sum(1 for i in items if i["done"]), "total": len(items)}


@app.post("/api/audit/label")
def audit_label(body: AuditLabelIn):
    plan = _audit_plan(body.audit_id)
    if not plan:
        return JSONResponse({"error": "No such audit."}, status_code=404)
    item = next((i for i in plan["items"] if i["item_id"] == body.item_id), None)
    if not item:
        return JSONResponse({"error": "No such item."}, status_code=404)
    rec, err = audit_plan.validate_label(body.model_dump(), item)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    stored = store.append("audit_labels", {"audit_id": body.audit_id, "item_id": body.item_id, **rec})
    return {"ok": True, "overall_correct": rec["overall_correct"], "firebase": stored.get("_firebase")}


@app.get("/api/audit/progress")
def audit_progress(audit_id: str):
    plan = _audit_plan(audit_id)
    if not plan:
        return JSONResponse({"error": "No such audit."}, status_code=404)
    return {"progress": audit_plan.progress(plan, [l for l in store.read("audit_labels") if l.get("audit_id") == audit_id])}


@app.get("/api/audit/score")
def audit_score(audit_id: str, x_owner_password: Optional[str] = Header(default=None)):
    """Owner only: the scores show the judge's verdicts and the strata, which would unblind a labeler."""
    bad = _owner_gate(x_owner_password, "The audit score")
    if bad:
        return bad
    plan = _audit_plan(audit_id)
    if not plan:
        return JSONResponse({"error": "No such audit."}, status_code=404)
    _, runs = _collect_runs(100000)
    return audit_plan.score(plan, runs, [l for l in store.read("audit_labels") if l.get("audit_id") == audit_id])


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
        mine = campaign.slice_cells(g["todo"], k, n, manifest)
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


@app.get("/api/export/experiments")
def export_experiments():
    _, runs = _collect_runs(100000)
    return {"experiments": export.experiment_names(runs)}


@app.get("/api/export/experiment.zip")
def export_experiment_zip(name: str):
    """One zip per experiment name (however many runs): each run's log, story and diagnosis text, plus a CSV/JSONL
    index and a README listing what is missing and why. No keys or .env are ever read."""
    from fastapi.responses import Response
    name = (name or "").strip()
    _, runs = _collect_runs(100000)
    mine = [r for r in runs if (r.get("experiment") or "").strip() == name]
    if not name or not mine:
        return JSONResponse({"error": f"no runs recorded under experiment '{name}'"}, status_code=404)
    raw = {r.get("run_id"): r for r in runner.list_local_runs(100000)}

    def read_log(run):
        fn = run.get("log_file") or ""
        if not fn or "/" in fn or "\\" in fn or ".." in fn:
            return None
        p = runner.reliops_dir() / "results" / "gui_logs" / fn
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
        return firebase_sync.read_log(fn)     # a teammate's run: the shared copy (keys masked)

    def read_story(run):
        rec = raw.get(run.get("run_id"))
        why = "run is not on this machine (a teammate's) and no shared story was found"
        if rec is not None:
            try:
                return runner.render_story(rec), ""
            except Exception as e:  # missing transcript, replay script, or a renderer error: say why, keep going
                why = str(e)
        shared = firebase_sync.read_story(run.get("run_id") or "")
        return (shared, "") if shared else (None, why)

    data, _ = export.experiment_zip(name, mine, read_log, read_story)
    safe = export._slug(name, 60)
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f"attachment; filename=experiment_{safe}.zip"})


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


# ---- Run purposes (marks) and the Results dashboard -------------------------------------
class MarkIn(BaseModel):
    run_ids: list[str]
    purpose: Optional[str] = None      # experiment | dev | final; empty clears the mark (back to automatic)
    note: Optional[str] = None
    marked_by: Optional[str] = None


@app.post("/api/marks")
def set_marks(body: MarkIn):
    """Marks runs as experiment (just look), dev (analyse, may tune on) or final (a frozen-set run that is
    reported). A mark overrides the automatic purpose; clearing it restores the automatic one."""
    ids = list(dict.fromkeys(i.strip() for i in body.run_ids if i and i.strip()))
    if not ids:
        return JSONResponse({"error": "no runs given"}, status_code=400)
    if body.purpose and body.purpose not in dashboard.PURPOSES:
        return JSONResponse({"error": f"purpose must be one of {list(dashboard.PURPOSES)} or empty"}, status_code=400)
    for rid in ids:
        store.append("marks", {"run_id": rid, "purpose": body.purpose or None, "deleted": not body.purpose,
                               "note": (body.note or "").strip(), "marked_by": body.marked_by or "unknown"})
    return {"ok": True, "marked": len(ids), "purpose": body.purpose or "automatic"}


class DashboardIn(BaseModel):
    filters: dict = {}
    metric: str = "e2e_pass"


def _dashboard(filters: dict, metric: str) -> dict:
    if metric not in stats.METRICS:
        raise ValueError(f"metric must be one of {stats.METRICS}")
    source, runs = _collect_runs(100000)
    out = dashboard.build(runs, filters, metric, store.read("manifests"), store.read("labels"),
                          store.read("rejudges"), source)
    out["provenance"]["firebase"] = firebase_sync.sync_info("runs")
    return out


@app.post("/api/dashboard")
def dashboard_view(body: DashboardIn):
    """Everything the Results tab shows, computed from exactly the runs the filters leave in view."""
    try:
        return _dashboard(body.filters, body.metric)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/report.md")
def dashboard_report(body: DashboardIn):
    try:
        d = _dashboard(body.filters, body.metric)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return PlainTextResponse(dashboard.report_markdown(d), media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=reliops_results_report.md"})


# ---- API key health (names and owners only; the secret never leaves .env) -------------------
def _machine() -> str:
    import socket
    return socket.gethostname()


def _fp(var: str, env: dict) -> str:
    """A short fingerprint of a key's value, so a mark can tell the key was replaced. One-way; the secret is not stored."""
    import hashlib
    v = env.get(var) or os.environ.get(var) or ""
    return hashlib.sha256(f"{var}:{v}".encode()).hexdigest()[:10] if v else ""


def _key_states() -> dict:
    """{var: {status, until, owner, reason}} for this machine. Keys are per person, so each laptop tracks its own.
    A mark made against an earlier value of the key no longer applies once the value in .env has changed."""
    me = _machine()
    env = runner.merged_env_vars()
    out = {}
    for r in store.read("key_status"):
        if r.get("owner_machine") != me or not r.get("var"):
            continue
        out[r["var"]] = r
    for var, r in list(out.items()):
        if r.get("fp") and r["fp"] != _fp(var, env) and r.get("status") != "ok":
            out[var] = {**r, "status": "ok", "until": None, "reason": "the key value changed since it was marked", "stale_mark": True}
    return out


def _record_key_event(var: str, owner: str | None, status: str, until: float | None, reason: str | None) -> None:
    store.append("key_status", {"owner_machine": _machine(), "var": var, "owner": owner or var, "status": status,
                                "until": until, "reason": reason or "", "marked_by": "lab",
                                "fp": _fp(var, runner.merged_env_vars())})


runner.KEY_STATE_PROVIDER = _key_states
runner.KEY_EVENT_SINK = _record_key_event


def _key_source(var: str) -> tuple[str, str, bool]:
    """(where the value comes from, its shape, whether it carries text a key never has).

    The shape is the first 4 characters and the length, never the key."""
    lab = runner.read_env_file(runner.lab_env_path())
    if lab.get(var):
        src, val = "Lab .env", lab[var]
    elif os.environ.get(var):
        src, val = "shell environment (not in any .env)", os.environ[var]
    else:
        return "not set", "", False
    return src, f"{val[:4]}… · {len(val)} characters", runner.key_looks_garbled(val)


def _gemini_vars(env: dict) -> list[str]:
    names = {k for k in list(env) + list(os.environ) if runner.GEMINI_FAMILY_RE.match(k) and (env.get(k) or os.environ.get(k))}
    return sorted(names, key=lambda n: (n != "GEMINI_API_KEY", n))


def _key_rows(env: dict, pool: list[dict], states: dict) -> list[dict]:
    in_pool = [p["var"] for p in pool]
    names = _gemini_vars(env) + [k for k in runner.KNOWN_PROVIDERS if k != "GEMINI_API_KEY" and (env.get(k) or os.environ.get(k))]
    out = []
    for n in dict.fromkeys(in_pool + names):
        st = states.get(n) or {}
        status = st.get("status") or "ok"
        if status == "exhausted" and (st.get("until") or 0) <= __import__("time").time():
            status = "ok"
        owner = next((p["owner"] for p in pool if p["var"] == n), n)
        source, shape, garbled = _key_source(n)
        out.append({"var": n, "owner": owner, "status": status, "until": st.get("until"), "reason": st.get("reason") or "",
                    "source": source, "shape": shape, "in_pool": n in in_pool, "rotatable": bool(runner.GEMINI_FAMILY_RE.match(n)),
                    "note": "This value contains a space, a quote or a '#', which a key never has; check the line in the .env." if garbled else ""})
    return out


@app.get("/api/keys")
def key_health():
    """The model keys this machine has, with their health and whether they are in the rotation pool."""
    env = runner.merged_env_vars()
    pool = runner.key_pool(env)
    return {"pool": len(pool) >= 2, "keys": _key_rows(env, pool, _key_states()),
            "help": "Tick two or more Gemini keys and press Save to rotate between them. Off until you do."}


class PoolIn(BaseModel):
    vars: list[str]
    owners: dict[str, str] = {}


@app.post("/api/keys/pool")
def key_pool_save(body: PoolIn):
    """Rotation on/off, from the screen: writes RELIOPS_KEY_POOL / RELIOPS_KEY_OWNERS in this tool's .env."""
    env = runner.merged_env_vars()
    have = set(_gemini_vars(env))
    bad = [v for v in body.vars if v not in have]
    if bad:
        return JSONResponse({"error": "not a saved Gemini key: " + ", ".join(bad)}, status_code=400)
    runner.write_pool(body.vars, body.owners)
    n = len(body.vars)
    return {"ok": True, "rotation": n >= 2, "message": f"rotation on for {n} keys" if n >= 2 else "rotation is off (it needs two or more keys)"}


class PoolRemoveIn(BaseModel):
    var: str


@app.post("/api/keys/pool/remove")
def key_pool_remove(body: PoolRemoveIn):
    env = runner.merged_env_vars()
    pool = runner.key_pool(env)
    keep = [p["var"] for p in pool if p["var"] != body.var]
    runner.write_pool(keep, {p["var"]: p["owner"] for p in pool})
    return {"ok": True, "rotation": len(keep) >= 2, "left": len(keep)}


def _key_alerts() -> list[dict]:
    """Keys in use that need attention: dead (a person must replace or remove them) or resting (comes back by itself)."""
    env = runner.merged_env_vars()
    pool = runner.key_pool(env)
    states = _key_states()
    in_use = [p["var"] for p in pool] or [v for v in ("GEMINI_API_KEY",) if env.get(v) or os.environ.get(v)]
    now = __import__("time").time()
    out = []
    for v in in_use:
        st = states.get(v) or {}
        status = st.get("status")
        if status == "exhausted" and (st.get("until") or 0) <= now:
            continue
        if status in ("dead", "exhausted"):
            owner = next((p["owner"] for p in pool if p["var"] == v), v)
            out.append({"var": v, "owner": owner, "status": status, "until": st.get("until"), "reason": st.get("reason") or "",
                        "in_pool": v in [p["var"] for p in pool], "pool_size": len(pool)})
    return out


class KeyMarkIn(BaseModel):
    var: str
    status: str            # ok | dead | exhausted
    by: Optional[str] = None


@app.post("/api/keys/mark")
def key_mark(body: KeyMarkIn):
    if body.status not in ("ok", "dead", "exhausted"):
        return JSONResponse({"error": "status must be ok, dead or exhausted"}, status_code=400)
    env = runner.merged_env_vars()
    if body.var not in runner.KNOWN_PROVIDERS and body.var not in [p["var"] for p in runner.key_pool(env)]:
        return JSONResponse({"error": "unknown key variable"}, status_code=400)
    until = None
    if body.status == "exhausted":
        until = runner._next_pacific_midnight(__import__("time").time())
    owner = next((p["owner"] for p in runner.key_pool(env) if p["var"] == body.var), body.var)
    store.append("key_status", {"owner_machine": _machine(), "var": body.var, "owner": owner, "status": body.status,
                                "until": until, "reason": "set by hand", "marked_by": body.by or "unknown",
                                "fp": _fp(body.var, env)})
    return {"ok": True}


# ---- Shared, read-only results page (see publish.py) -------------------------------------------
_SHARE_TTL_S = 15.0
_share_cache: dict = {"at": 0.0, "runs": ("local", [])}
_publisher: Optional[publish.Publisher] = None
publish.SYNC_PROVIDER = lambda: firebase_sync.sync_info("runs")


def _share_runs() -> tuple[str, list[dict]]:
    """The runs for the shared page, recomputed at most every few seconds however many readers there are."""
    import time as _t

    if _t.time() - _share_cache["at"] > _SHARE_TTL_S:
        _share_cache["runs"] = _collect_runs(5000)
        _share_cache["at"] = _t.time()
    return _share_cache["runs"]


def _share_live() -> dict:
    job = jobs.current()
    desc = job.describe() if job else None
    qstate = queuestate.describe(jobs.busy())
    left = None
    if desc and desc["state"] == "running" and qstate:
        queue = _find_queue(qstate.get("queue_id") or "")
        if queue is not None:
            left = _runs_left(queue, qstate.get("experiment") or queue.get("name", ""), _share_runs()[1])
    return publish.build_live(desc, qstate, left)


@app.get("/share/data/snapshot.json")
def share_snapshot():
    source, runs = _share_runs()
    return JSONResponse(publish.build_snapshot(runs, source), headers={"Cache-Control": "no-store"})


@app.get("/share/data/live.json")
def share_live():
    return JSONResponse(_share_live(), headers={"Cache-Control": "no-store"})


@app.get("/share/")
def share_page():
    return FileResponse(publish.SITE_SRC / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/share/config.js")
def share_config():
    # the local preview reads its data from this server, so no override
    return PlainTextResponse("// local preview: data comes from this server\n", media_type="text/javascript")


@app.get("/api/publish/status")
def publish_status():
    if _publisher is None:
        return {"enabled": False, "preview": "/share/",
                "how": "Set RELIOPS_PUBLISH=on in .env (and RELIOPS_PUBLISH_GIT to a repository URL to host it); see docs/GUIDE.md."}
    return {"enabled": True, "preview": "/share/", **_publisher.describe()}


@app.on_event("startup")
def _start_publisher():
    global _publisher
    env = runner.merged_env_vars()
    for k in ("RELIOPS_PUBLISH_FINAL", "RELIOPS_PUBLISH_NAMES"):   # also used by the local /share/ preview
        if k in env:
            os.environ.setdefault(k, str(env[k]))
    if str(env.get("RELIOPS_PUBLISH", "")).strip().lower() not in ("1", "on", "true", "yes"):
        return
    out = Path(env.get("RELIOPS_PUBLISH_DIR") or (runner.reliops_dir() / "results" / "site"))
    _publisher = publish.Publisher(
        _share_runs, _share_live, out, remote=str(env.get("RELIOPS_PUBLISH_GIT") or ""),
        branch=str(env.get("RELIOPS_PUBLISH_BRANCH") or "results-data"),
        every_s=float(env.get("RELIOPS_PUBLISH_EVERY_S") or 60))
    _publisher.start()
