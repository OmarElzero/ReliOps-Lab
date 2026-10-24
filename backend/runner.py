"""Shells out to a local clone of OmarElzero/ReliOps and streams output line by line.

This is the v0, local-only "runner" from docs/plan/05-platform-spec.md — same job (clone,
patch, build, run, stream logs), just driven by this GUI instead of a human typing commands,
and running on your own machine instead of a sponsor VM. It never talks to the network except
to git-clone ReliOps/SREGym and call model APIs that SREGym itself calls — the GUI server
itself has no cloud dependency, so it works today with no Firebase project, no login.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

RELIOPS_REPO_URL = "https://github.com/OmarElzero/ReliOps"
DEFAULT_WORKDIR = Path(os.environ.get("RELIOPS_LAB_WORKDIR", Path.home() / "reliops-lab-workdir"))
RELIOPS_BRANCH = os.environ.get("RELIOPS_BRANCH", "claude/determined-einstein-mjmr56")

AGENTS = [
    {"id": "reliops_v1", "label": "ReliOps v1 (ours, typed)", "action_space": "typed"},
    {"id": "reliops_v1", "label": "ReliOps v1 (ours, typed + guarded shell)", "action_space": "typed+shell"},
    {"id": "stratus", "label": "STRATUS (multi-agent reference)", "action_space": None},
    {"id": "mini-sre", "label": "mini-sre (raw bash ReAct)", "action_space": None},
    {"id": "gemini", "label": "Gemini CLI (general coding agent)", "action_space": None},
    {"id": "opencode", "label": "OpenCode (general coding agent)", "action_space": None},
    {"id": "autosubmit", "label": "autosubmit (plumbing check only, no real intelligence)", "action_space": None},
]

MODEL_PRESETS = [
    "gemini/gemini-3.5-flash-lite",
    "gemini/gemini-3.8-flash",
]

DEFAULT_PROBLEMS = [
    "configmap_drift_hotel_reservation",
    "revoke_auth_mongodb-1",
]


def reliops_dir() -> Path:
    return DEFAULT_WORKDIR / "ReliOps"


def sregym_dir() -> Path:
    return reliops_dir() / "external" / "envs" / "sregym"


def _read_problem_ids(csv_path: Path) -> list[str]:
    try:
        lines = csv_path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return []
        header = lines[0].split(",")
        idx = header.index("problem_id")
        ids = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) > idx and parts[idx].strip():
                ids.append(parts[idx].strip())
        return ids
    except (OSError, ValueError):
        return []


def problem_sets() -> list[dict]:
    """Every CSV in experiments/ with a problem_id column becomes a selectable set in the GUI —
    so a new set the team adds later (another CSV in that folder) shows up with no code change.
    Always includes a 'custom' pseudo-set so one can still type a single problem id by hand."""
    sets = []
    exp_dir = reliops_dir() / "experiments"
    if exp_dir.is_dir():
        for csv_path in sorted(exp_dir.glob("*.csv")):
            ids = _read_problem_ids(csv_path)
            if ids:
                sets.append({"id": csv_path.stem, "label": f"{csv_path.stem} ({len(ids)} problems)", "problems": ids})
    if not sets:
        sets.append({"id": "default", "label": f"default ({len(DEFAULT_PROBLEMS)} problems)", "problems": DEFAULT_PROBLEMS})
    sets.append({"id": "custom", "label": "custom — type one problem id", "problems": []})
    return sets


def _stream(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> Iterator[str]:
    """Run a command, yielding its combined stdout/stderr as it's produced (unbuffered)."""
    full_env = {**os.environ, **(env or {})}
    yield f"$ {' '.join(shlex.quote(c) for c in cmd)}\n"
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line
    proc.wait()
    yield f"\n[exit code {proc.returncode}]\n"


def ensure_clone() -> Iterator[str]:
    DEFAULT_WORKDIR.mkdir(parents=True, exist_ok=True)
    if not (reliops_dir() / ".git").exists():
        yield from _stream(["git", "clone", RELIOPS_REPO_URL, str(reliops_dir())])
        yield from _stream(["git", "checkout", RELIOPS_BRANCH], cwd=reliops_dir())
    else:
        yield "ReliOps clone already present, skipping clone.\n"


def write_key(provider: str, value: str) -> None:
    """Save an API key into ReliOps/.env — stays on this machine, is never sent anywhere by this
    tool (SREGym itself reads it directly as an env var when it calls the model provider)."""
    reliops_dir().mkdir(parents=True, exist_ok=True)
    env_path = reliops_dir() / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    key_line = f'{provider}="{value}"'
    replaced = False
    out = []
    for line in lines:
        if line.strip().startswith(f"{provider}="):
            out.append(key_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(key_line)
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def stream_setup() -> Iterator[str]:
    yield from ensure_clone()
    yield from _stream(["bash", "scripts/quickstart.sh", "--setup-only"], cwd=reliops_dir())


def stream_demo() -> Iterator[str]:
    """Fakes a run's output, paced like a real one, so the wizard can be demoed with no cluster,
    no API key and no 10-minute cluster build — e.g. in a meeting, before the real machine is
    ready. Clearly labelled, never mixed up with a real result file."""
    lines = [
        "[DEMO MODE — simulated output, not a real run]",
        "waiting for conductor stage... diagnosis",
        "THOUGHT: pods are Running but one keeps restarting; checking its logs first.",
        '{"action": "get_pod_status", "args": {"namespace": "hotel-reservation"}}',
        "<exit_code>0</exit_code>",
        "mongo-0   1/1   Running   3 (2m ago)",
        "THOUGHT: 3 restarts on mongo-0 — checking previous logs for the crash reason.",
        '{"action": "get_logs", "args": {"namespace": "hotel-reservation", "pod": "mongo-0", "previous": true}}',
        "<exit_code>0</exit_code>",
        "Authentication failed: command createUser requires authentication",
        '{"action": "submit", "args": {"solution": "MongoDB auth is misconfigured; createUser calls are failing."}}',
        "Submission accepted for stage 'diagnosis'.",
        "waiting for conductor stage... mitigation",
        '{"action": "patch_configmap_key", "args": {"namespace": "hotel-reservation", "configmap": "mongo-config", "key": "auth", "value": "enabled"}}',
        "<exit_code>0</exit_code>",
        '{"action": "submit", "args": {"solution": ""}}',
        "Submission accepted for stage 'mitigation'.",
        "diagnosis_pass=true  mitigation_pass=true  e2e_pass=true",
        "[DEMO MODE — this did not touch any cluster]",
    ]
    for line in lines:
        yield line + "\n"
        time.sleep(0.5)


def _latest_result_row(problem: str, after_ts: float) -> dict | None:
    """Best-effort: find SREGym's own summary CSV written after the run started and pull out the
    row for this problem. Returns None rather than a guess if nothing matches — a run with no
    parsed row still has its full raw log; it is never reported as pass/fail without this row."""
    results_root = sregym_dir() / "results"
    if not results_root.is_dir():
        return None
    candidates = sorted(results_root.glob("*/*_ALL_results.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    for csv_path in candidates:
        if csv_path.stat().st_mtime < after_ts - 5:
            break
        try:
            lines = csv_path.read_text(encoding="utf-8").splitlines()
            header = lines[0].split(",")
            for line in reversed(lines[1:]):
                parts = line.split(",")
                row = dict(zip(header, parts, strict=False))
                if row.get("problem_id") == problem or row.get("problem") == problem:
                    return row
        except (OSError, IndexError):
            continue
    return None


def stream_campaign(
    *,
    agent: str,
    action_space: str | None,
    model: str,
    judge_model: str,
    problems: list[str],
    force_build: bool,
    run_by: str | None = None,
    on_result=None,
) -> Iterator[str]:
    """Runs one problem at a time (SREGym itself is single-run per process) and yields log lines
    for all of them in order, with a banner between problems. `on_result(record)` is called after
    each problem with whatever row we could recover — the GUI uses it to push to Firestore."""
    if not sregym_dir().exists():
        yield "SREGym clone not found — run Setup first.\n"
        return

    base_env = {"AGENT_MODEL_ID": model}
    if action_space:
        base_env["REACT_V1_ACTION_SPACE"] = action_space
    env_file = reliops_dir() / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                base_env.setdefault(k.strip(), v.strip().strip('"'))

    for i, problem in enumerate(problems, start=1):
        yield f"\n===== [{i}/{len(problems)}] problem={problem} agent={agent} action_space={action_space or '-'} =====\n"
        started_at = time.time()
        env = dict(base_env)
        cmd = [
            "uv", "run", "main.py",
            "--agent", agent,
            "--model", model,
            "--judge-model", judge_model,
            "--problem", problem,
            "--profile", "svelte",
        ]
        first_run_of_agent = force_build and i == 1
        if first_run_of_agent:
            cmd.append("--force-build")
        elif agent == "reliops_v1":
            env["SREGYM_AGENT_IMAGE"] = "sregym-agent-base:latest"

        yield from _stream(cmd, cwd=sregym_dir(), env=env)

        row = _latest_result_row(problem, started_at)
        record = {
            "run_id": str(__import__("uuid").uuid4()),
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "problem_id": problem,
            "agent_id": agent,
            "action_space": action_space,
            "model_id": model,
            "judge_model_id": judge_model,
            # Self-reported, not an authenticated identity — see README's "Shared results" section.
            # Whoever ran this typed it into the "Your name" field; it's a label for the team to
            # read, not an access-control boundary (the shared Firebase key has full access either way).
            "run_by": run_by or "unknown",
            "row": row,  # SREGym's own metrics.md-shaped row, or None if we couldn't locate it
        }
        if on_result:
            on_result(record)
        yield f"----- recorded result for {problem}: {'parsed row found' if row else 'no row parsed — check the raw log above'} -----\n"
