"""Shells out to a local clone of OmarElzero/ReliOps and streams output line by line.

This is the v0, local-only "runner" from docs/plan/05-platform-spec.md — same job (clone,
patch, build, run, stream logs), just driven by this GUI instead of a human typing commands,
and running on your own machine instead of a sponsor VM. It never talks to the network except
to git-clone ReliOps/SREGym and call model APIs that SREGym itself calls — the GUI server
itself has no cloud dependency, so it works today with no Firebase project, no login.
"""

from __future__ import annotations

import csv
import os
import shlex
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

RELIOPS_REPO_URL = "https://github.com/OmarElzero/ReliOps"
DEFAULT_WORKDIR = Path(os.environ.get("RELIOPS_LAB_WORKDIR", Path.home() / "reliops-lab-workdir"))
RELIOPS_BRANCH = os.environ.get("RELIOPS_BRANCH", "claude/determined-einstein-mjmr56")

AGENTS = [
    {
        "id": "reliops_v1", "label": "ReliOps v1 (ours, typed)", "action_space": "typed",
        "desc": "Our own agent. The model can only call a fixed list of safe, typed actions "
                "(restart, scale, roll back, patch config) — never a free shell command.",
    },
    {
        "id": "reliops_v1", "label": "ReliOps v1 (ours, typed + guarded shell)", "action_space": "typed+shell",
        "desc": "Same agent, with a guarded shell tool added — can run arbitrary kubectl commands "
                "too, routed through SREGym's own safety runner. In the pilot this arm scored "
                "53% end-to-end vs the typed-only arm's 33% (n=15 each).",
    },
    {
        "id": "stratus", "label": "STRATUS (multi-agent reference)", "action_space": None,
        "desc": "A published multi-agent reference system (xlab-uiuc/stratus) — the external "
                "baseline we compare against, not something we built.",
    },
    {
        "id": "mini-sre", "label": "mini-sre (raw bash ReAct)", "action_space": None,
        "desc": "SREGym's own built-in baseline agent, not ours — raw shell access, no typed tool "
                "list at all. A simple reference point for how much a tool catalogue matters.",
    },
    {
        "id": "gemini", "label": "Gemini CLI (general coding agent)", "action_space": None,
        "desc": "Google's general-purpose coding CLI, pointed at the same problem — not an "
                "SRE-specific agent, another reference point.",
    },
    {
        "id": "opencode", "label": "OpenCode (general coding agent)", "action_space": None,
        "desc": "A general-purpose open-source coding agent, same idea as Gemini CLI — not "
                "SRE-specific.",
    },
    {
        "id": "autosubmit", "label": "autosubmit (plumbing check only, no real intelligence)", "action_space": None,
        "desc": "Submits immediately with no real diagnosis. Checks that the cluster/harness "
                "plumbing works end to end — not a real test of any agent.",
    },
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


def default_firebase_key_path() -> Path:
    """Where this tool suggests saving a shared Firebase service-account key — next to the ReliOps
    clone, outside any git repo. The GUI pre-fills its Firebase field with this, so a teammate who
    saves the file here never has to type a path by hand (README's "Shared results" section)."""
    return DEFAULT_WORKDIR / "firebase-service-account.json"


def _read_problems(csv_path: Path) -> list[dict]:
    """Reads a problem-set CSV into a list of {id, n, category, difficulty}. Only `problem_id` is
    required — `n` (testing order), `type` (category) and `expected_difficulty` are used when the
    CSV has them (e.g. experiments/first-30.csv does) and left blank otherwise, so the GUI can
    group and number problems instead of showing one flat list."""
    try:
        with csv_path.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = []
            for i, row in enumerate(reader, start=1):
                pid = (row.get("problem_id") or "").strip()
                if not pid:
                    continue
                rows.append({
                    "id": pid,
                    "n": (row.get("n") or "").strip() or str(i),
                    "category": (row.get("type") or "").strip() or "Uncategorized",
                    "difficulty": (row.get("expected_difficulty") or "").strip(),
                })
            return rows
    except (OSError, csv.Error):
        return []


def problem_sets() -> list[dict]:
    """Every CSV in experiments/ with a problem_id column becomes a selectable set in the GUI —
    so a new set the team adds later (another CSV in that folder) shows up with no code change.
    Each set's `problems` is a list of {id, n, category, difficulty} dicts, in file order (which
    is testing order for experiments/first-30.csv). Always includes a 'custom' pseudo-set so one
    can still type a single problem id by hand."""
    sets = []
    exp_dir = reliops_dir() / "experiments"
    if exp_dir.is_dir():
        for csv_path in sorted(exp_dir.glob("*.csv")):
            rows = _read_problems(csv_path)
            if rows:
                sets.append({"id": csv_path.stem, "label": f"{csv_path.stem} ({len(rows)} problems)", "problems": rows})
    if not sets:
        default_rows = [{"id": p, "n": str(i), "category": "Uncategorized", "difficulty": ""}
                         for i, p in enumerate(DEFAULT_PROBLEMS, start=1)]
        sets.append({
            "id": "default",
            "label": f"default ({len(default_rows)} problems — run Setup to get the real 30-problem set)",
            "problems": default_rows,
        })
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


KNOWN_PROVIDERS = ["GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"]


def read_env_file(env_path: Path) -> dict[str, str]:
    """Parses a .env file into a dict. Values can be a single line or span many lines (a pasted
    JSON blob, e.g. a Firebase service-account key) — anything from a `KEY=` line up to the next
    `KEY=` line or end of file belongs to that key, not just the first line."""
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    current_key: str | None = None
    buf: list[str] = []

    def _is_key(candidate: str) -> bool:
        c = candidate.strip()
        return bool(c) and all(ch.isalnum() or ch == "_" for ch in c) and not c[0].isdigit()

    def _flush() -> None:
        if current_key is not None:
            out[current_key] = "\n".join(buf).strip().strip('"').strip("'")

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if current_key is not None:
                buf.append(line)
            continue
        head = line.split("=", 1)[0]
        if "=" in line and not line.startswith((" ", "\t")) and _is_key(head):
            _flush()
            current_key, _, rest = line.partition("=")
            current_key = current_key.strip()
            buf = [rest]
        elif current_key is not None:
            buf.append(line)
    _flush()
    return out


def saved_providers() -> dict[str, bool]:
    """Which of the known model-key providers already have a non-empty value in ReliOps/.env —
    used by the GUI to show "already saved" instead of a blank field that looks unconfigured."""
    env_vars = read_env_file(reliops_dir() / ".env")
    return {p: bool(env_vars.get(p)) for p in KNOWN_PROVIDERS}


# The team's canonical per-run row, mirrored from experiments/run_log_template.csv in the main
# repo so every run (GUI or terminal) lands in the same shape (CLAUDE.md: "results/ | CSV
# summaries"). Kept as a fallback for when that template isn't in the clone yet; _run_log_header()
# always prefers reading the real file over this copy, so a column the team adds there shows up
# here automatically with no code change.
_RUN_LOG_HEADER_FALLBACK = [
    "run_id", "timestamp_utc", "git_commit", "env_id", "problem_id", "fault_class", "app",
    "agent_id", "model_id", "flow_variant", "instruction_variant", "action_space", "noise",
    "repeat_n", "diagnosis_d1", "diagnosis_d2", "diagnosis_d3", "diagnosis_composite",
    "diagnosis_pass", "mitigation_pass", "e2e_pass", "ttd_s", "ttm_s", "prompt_tokens",
    "completion_tokens", "total_tokens", "num_llm_calls", "num_tool_calls", "durability_pass",
    "unneeded_actions", "forbidden_actions", "hallucinated_incident", "scope_note",
    "failure_mode_tag", "notes",
]


def _run_log_header() -> list[str]:
    template = reliops_dir() / "experiments" / "run_log_template.csv"
    try:
        first_line = template.read_text(encoding="utf-8").splitlines()[0]
        cols = [c.strip() for c in first_line.split(",") if c.strip()]
        if cols:
            return cols
    except OSError:
        pass
    return list(_RUN_LOG_HEADER_FALLBACK)


def append_csv_run(record: dict) -> Path:
    """Appends one row to results/gui_runs.csv in the team's canonical schema (same columns as
    experiments/run_log_template.csv), alongside the fuller results/gui_runs.jsonl this tool
    already wrote (that one keeps everything — run_by, the Firebase outcome, SREGym's raw row
    untouched — this CSV is the trimmed, standard-shape version meant to sit next to the rest of
    the team's results/*.csv files). A column the template expects that SREGym's own row didn't
    return, or that this GUI doesn't know, is left blank — never guessed."""
    header = _run_log_header()
    row_data = dict(record.get("row") or {})  # SREGym's own metrics.md-shaped row, if present
    # What the GUI itself knows for certain always wins over whatever (if anything) `row` has
    # under the same name — these came straight from the form, not a derived/parsed value.
    row_data.update({
        "run_id": record.get("run_id", ""),
        "timestamp_utc": record.get("timestamp_utc", ""),
        "problem_id": record.get("problem_id", ""),
        "agent_id": record.get("agent_id", ""),
        "model_id": record.get("model_id", ""),
        "action_space": record.get("action_space") or "",
    })
    out_row = {col: row_data.get(col, "") for col in header}

    csv_path = reliops_dir() / "results" / "gui_runs.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow(out_row)
    return csv_path


def new_log_file(kind: str) -> Path:
    """Opens a fresh timestamped log file under results/gui_logs/ for one Setup/Demo/Run action,
    so the full raw output survives past the browser tab closing or the log panel being cleared —
    for debugging a problem after the fact, not just while it's on screen."""
    log_dir = reliops_dir() / "results" / "gui_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return log_dir / f"{stamp}_{kind}.log"


def _tool_version(cmd: str) -> str | None:
    """None means the command isn't on PATH at all; a string (even empty) means it is."""
    if shutil.which(cmd) is None:
        return None
    probe = ["version", "--client", "--short"] if cmd == "kubectl" else ["--version"]
    try:
        out = subprocess.run([cmd, *probe], capture_output=True, text=True, timeout=5)
        text = (out.stdout or out.stderr or "").strip().splitlines()
        return text[0] if text else "present"
    except Exception:  # noqa: BLE001 — a tool that exists but errors on --version still "exists"
        return "present"


def doctor() -> dict:
    """A rough "can this machine run SREGym?" check — CPU/RAM/disk, Docker, and the CLI tools
    Setup needs. Thresholds come from CLAUDE.md's own machine notes: the cloud sandbox (4 CPU,
    ~15 GB RAM) is the smallest machine this project has actually run a kind cluster on; this is
    a rough comparison against that, not a hard pass/fail line."""
    cpu = os.cpu_count()

    ram_gb = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    ram_gb = round(int(line.split()[1]) / 1024 / 1024, 1)
                    break
    except OSError:
        pass  # not Linux/WSL, or /proc unavailable — leave as None, note it below

    disk_free_gb = None
    try:
        probe_dir = DEFAULT_WORKDIR if DEFAULT_WORKDIR.exists() else DEFAULT_WORKDIR.parent
        disk_free_gb = round(shutil.disk_usage(probe_dir).free / 1024**3, 1)
    except OSError:
        pass

    docker_running = False
    try:
        docker_running = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5
        ).returncode == 0
    except Exception:  # noqa: BLE001 — docker not installed, daemon down, whatever: just "not running"
        docker_running = False

    tools = {name: _tool_version(name) for name in ("kubectl", "helm", "kind", "uv")}
    missing_tools = [name for name, v in tools.items() if v is None]

    notes: list[str] = []
    verdict = "looks fine for one SREGym kind cluster"
    if cpu is not None and cpu < 4:
        notes.append(f"{cpu} CPU cores detected — the smallest machine this project has actually "
                      f"run on had 4")
        verdict = "might struggle — few CPU cores"
    if ram_gb is not None and ram_gb < 12:
        notes.append(f"{ram_gb} GB RAM detected — ~15 GB is the smallest this project has "
                      f"actually run a kind cluster on")
        verdict = "might struggle — RAM is tight"
    if disk_free_gb is not None and disk_free_gb < 20:
        notes.append(f"only {disk_free_gb} GB free disk — SREGym's images and cluster data can "
                      f"add up over a few runs")
    if not docker_running:
        notes.append("`docker info` failed — Docker isn't running (try `dockerd &` or start "
                      "Docker Desktop) before clicking Run setup")
        verdict = "blocked — Docker isn't running"
    if missing_tools:
        notes.append("missing from PATH: " + ", ".join(missing_tools) +
                      " — Run setup installs `kind` for you; the rest need installing by hand, "
                      "see ReliOps/docs/MANUAL-SETUP.md §0–2")
        verdict = "blocked — missing tools: " + ", ".join(missing_tools)

    return {
        "cpu_cores": cpu,
        "ram_gb": ram_gb,
        "disk_free_gb": disk_free_gb,
        "docker_running": docker_running,
        "tools": tools,
        "verdict": verdict,
        "notes": notes,
    }


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
