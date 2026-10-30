"""Shells out to a local clone of OmarElzero/ReliOps and streams output line by line.

This is the v0, local-only "runner" from docs/plan/05-platform-spec.md — same job (clone,
patch, build, run, stream logs), just driven by this GUI instead of a human typing commands,
and running on your own machine instead of a sponsor VM. It never talks to the network except
to git-clone ReliOps/SREGym and call model APIs that SREGym itself calls — the GUI server
itself has no cloud dependency, so it works today with no Firebase project, no login.
"""

from __future__ import annotations

import csv
import json
import os
import shlex
import shutil
import subprocess
import re
import sys
import threading
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


def lab_dir() -> Path:
    """This tool's own project root (reliops-lab/ — the folder with backend/, frontend/, README.md
    in it), as opposed to reliops_dir(), the separately *cloned* copy of the main ReliOps repo this
    tool drives. Two different folders on purpose (this one is the GUI's own code; that one is the
    benchmark it's testing), but both used to have their own `.env`, and only the clone's was ever
    read — so a key saved in the one .env a person actually has open in their editor (this one,
    lab_env_path()) silently went nowhere. lab_env_path() is now the primary source precisely
    because it's the file next to backend/ and frontend/, not a path nobody is looking at."""
    return Path(__file__).resolve().parent.parent


def lab_env_path() -> Path:
    return lab_dir() / ".env"


def default_firebase_key_path() -> Path:
    """Where this tool suggests saving a shared Firebase service-account key — right next to this
    project's own .env, so "save the file next to the folder you already have open" is the whole
    instruction (README's "Shared results" section)."""
    return lab_dir() / "firebase-service-account.json"


def merged_env_vars() -> dict[str, str]:
    """This tool's own .env (lab_env_path()) is the source of truth for every key the GUI itself
    cares about — model keys, Firebase credentials — because it's the file a person actually opens
    and edits (see lab_dir()'s docstring). The cloned repo's own .env (reliops_dir()/.env) is read
    too and used for anything the lab .env doesn't set, since someone following
    docs/MANUAL-SETUP.md by hand may have set things up there directly before ever opening this
    GUI; the lab .env wins on any key both define."""
    merged = read_env_file(reliops_dir() / ".env")
    merged.update(read_env_file(lab_env_path()))
    return merged


_key_counter = 0


def key_pool(env_vars: dict[str, str]) -> list[dict]:
    """Optional round-robin over several API keys. Off unless the .env names a pool:

        RELIOPS_KEY_POOL=GEMINI_API_KEY,GEMINI_API_KEY_2,GEMINI_API_KEY_3
        RELIOPS_KEY_OWNERS=omar,sara,ahmed        (optional, same order; shown in the log and stored on each run)

    The first name is the variable the agent reads; each run gets the next key in the list copied into it.
    Returns [{var, owner, value}] for the listed variables that have a value. The secret is never logged or stored;
    a run records only its slot and owner."""
    names = [n.strip() for n in (env_vars.get("RELIOPS_KEY_POOL") or "").split(",") if n.strip()]
    owners = [o.strip() for o in (env_vars.get("RELIOPS_KEY_OWNERS") or "").split(",")]
    pool = []
    for i, n in enumerate(names):
        if env_vars.get(n):
            pool.append({"var": n, "owner": owners[i] if i < len(owners) and owners[i] else n, "value": env_vars[n]})
    return pool


# Key health. The app wires these in (it owns the store): a function returning {var: {"status", "until"}} and a
# function that records a change. Only variable names and owners are ever stored, never the secret.
KEY_STATE_PROVIDER = None
KEY_EVENT_SINK = None

_DEAD_RE = re.compile(r"PERMISSION_DENIED|API_KEY_INVALID|API key not valid|ACCOUNT_STATE_INVALID|CONSUMER_SUSPENDED|has been suspended", re.I)
# A rejected credential (HTTP 401) is a different failure from a rate limit. Whether it means "this key is bad" depends on
# where the request went: through a custom AGENT_API_BASE it is that gateway rejecting the key, not Google.
_AUTH_RE = re.compile(r"AuthenticationError|Invalid credential|UNAUTHENTICATED", re.I)
_LIMIT_RE = re.compile(r"RESOURCE_EXHAUSTED|RateLimitError|\b429\b|quota exceeded|exceeded your current quota", re.I)
_DAILY_RE = re.compile(r"PerDay|per day|daily|RequestsPerDay", re.I)
_MINUTE_RE = re.compile(r"PerMinute|per minute|RPM", re.I)


def _next_pacific_midnight(now: float) -> float:
    """Gemini's daily quota resets at midnight Pacific time; fall back to +24h where tz data is missing."""
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        la = datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles"))
        nxt = (la + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
        return nxt.timestamp()
    except Exception:  # noqa: BLE001
        return now + 24 * 3600


def classify_key_problem(text: str, now: float | None = None, via_custom_base: bool = False) -> dict | None:
    """Reads a chunk of a run's output for a key problem. A heuristic on the provider's error wording, not a probe:
    - dead: the provider says the key or its account is invalid or suspended (stays out until a person clears it)
    - exhausted: a rate or quota error. Daily wording rests the key until the next Pacific midnight, per-minute wording
      for 2 minutes, anything else for an hour."""
    now = time.time() if now is None else now
    if _DEAD_RE.search(text):
        return {"status": "dead", "until": None, "reason": _DEAD_RE.search(text).group(0)}
    if not via_custom_base and _AUTH_RE.search(text):
        return {"status": "dead", "until": None, "reason": _AUTH_RE.search(text).group(0)}
    if _LIMIT_RE.search(text):
        if _DAILY_RE.search(text):
            until = _next_pacific_midnight(now)
        elif _MINUTE_RE.search(text):
            until = now + 120
        else:
            until = now + 3600
        return {"status": "exhausted", "until": until, "reason": _LIMIT_RE.search(text).group(0)}
    return None


def run_incomplete(row: dict | None) -> bool:
    """True when a run produced no usable result: no result row at all, or SREGym marked it incomplete."""
    return row is None or row.get("run_status") == "incomplete"


def max_incomplete_streak(env_vars: dict[str, str]) -> int:
    """How many incomplete runs in a row stop a queue (RELIOPS_MAX_INCOMPLETE, default 3, 0 turns the stop off).
    Incomplete runs are never counted as failures in the results; this only protects the night from a broken setup."""
    try:
        return max(0, int((env_vars.get("RELIOPS_MAX_INCOMPLETE") or "3").strip()))
    except ValueError:
        return 3


def preflight_warnings(env_vars: dict[str, str], model: str, agent: str, reliops: Path | None = None) -> list[str]:
    """Cheap checks before a campaign starts. Warnings only, never a block."""
    out: list[str] = []
    base = (env_vars.get("AGENT_API_BASE") or "").strip()
    if base:
        host = re.sub(r"^https?://", "", base).split("/")[0]
        google = host.endswith("googleapis.com")
        note = ""
        if model.startswith("gemini/") and not google:
            note = f" The model is {model}, so the agent will send Gemini requests to {host} with a key that host may not accept (a 401 'Invalid credential' is the usual sign)."
        out.append(
            f"[check] AGENT_API_BASE is set ({host}). SREGym then lets the agent reach only that host, and every model call goes there."
            f"{note} If you did not mean to use a gateway, remove AGENT_API_BASE (and AGENT_API_KEY) from the .env files and from your shell."
        )
    try:
        driver = (reliops or reliops_dir()) / "agent" / "react_v1" / "driver.py"
        if agent == "reliops_v1" and driver.exists() and shutil.which("docker"):
            r = subprocess.run(["docker", "image", "inspect", "-f", "{{.Created}}", "sregym-agent-base:latest"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                from datetime import datetime
                created = datetime.fromisoformat(r.stdout.strip().replace("Z", "+00:00")[:26].rstrip(".") + "+00:00")
                if driver.stat().st_mtime > created.timestamp():
                    out.append("[check] the agent image looks older than agent/react_v1/driver.py. Run scripts/sync_agent.sh and tick "
                               "'Force-build the agent image' for the next run, or the old driver code runs.")
    except Exception:  # noqa: BLE001  best effort only
        pass
    return out


def key_available(var: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    st = (KEY_STATE_PROVIDER() if KEY_STATE_PROVIDER else {}).get(var)
    if not st or st.get("status") in (None, "ok"):
        return True
    if st["status"] == "dead":
        return False
    return (st.get("until") or 0) <= now


def next_key(pool: list[dict]) -> tuple[int, dict] | None:
    """The next usable slot in the pool, advancing a counter that lives for the whole server session so every queue step
    and every run continues the rotation instead of starting again at the first key. Dead and resting keys are skipped.
    Returns None when there is nothing to rotate (fewer than two keys); raises KeyPoolEmpty when none is usable."""
    global _key_counter
    if len(pool) < 2:
        return None
    for _ in range(len(pool)):
        slot = _key_counter % len(pool)
        _key_counter += 1
        if key_available(pool[slot]["var"]):
            return slot, pool[slot]
    raise KeyPoolEmpty()


class KeyPoolEmpty(Exception):
    pass


def pool_wait_until(pool: list[dict]) -> float | None:
    """When the first resting key is usable again, or None if every key is dead."""
    st = KEY_STATE_PROVIDER() if KEY_STATE_PROVIDER else {}
    times = [(st.get(k["var"]) or {}).get("until") for k in pool if (st.get(k["var"]) or {}).get("status") == "exhausted"]
    times = [t for t in times if t]
    return min(times) if times else None


def normalize_category(raw: str) -> str:
    """The CSVs label some types with a suffix: ' [our label]' (the team's own class where SREGym has none),
    '(OS level)' (a qualifier) and a trailing ' Failure'. Grouping and filtering should treat
    'Misconfiguration Failure', 'Misconfiguration [our label]' and 'Misconfiguration Failure (OS level)'
    as one category, so strip those."""
    t = re.sub(r"\s*\[[^\]]*\]", "", raw or "")
    t = re.sub(r"\s*\([^)]*\)", "", t).strip()
    t = re.sub(r"\s+Failure$", "", t).strip()
    if t == "Cloud Management System":
        t = "Cloud Management"
    return t or "Uncategorized"


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
                    "category": normalize_category((row.get("type") or "").strip()),
                    "type": (row.get("type") or "").strip(),
                    "difficulty": (row.get("expected_difficulty") or "").strip(),
                    "app": (row.get("app") or "").strip(),  # used to guess the namespace to record
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


def _stream(
    cmd: list[str],
    cwd: Path | None = None,
    env: dict | None = None,
    result: list | None = None,
    capture: list | None = None,
) -> Iterator[str]:
    """Run a command, yielding its combined stdout/stderr as it's produced (unbuffered). If
    `result` is given, the process's exit code is appended to it once the command finishes, so a
    caller can gate later steps on whether this one actually succeeded instead of always running
    them next regardless (that gap used to let a failed `git clone` be followed straight into a
    `git checkout` in a directory that was never a git repo). If `capture` is given, every line is
    also appended to it, so a caller can inspect what the command actually said (e.g. to tell a
    private-repo auth failure apart from a network error) instead of only knowing it failed.

    stdin is always closed (DEVNULL), never inherited, and GIT_TERMINAL_PROMPT=0 is always set: a
    GUI server has no terminal for a subprocess to prompt against, so a `git` that would otherwise
    sit waiting for a username/password or an SSH host-key confirmation — looking like the whole
    run has frozen, with no error and no way to answer it — fails fast instead and the real reason
    shows up in the log."""
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
    yield f"$ {' '.join(shlex.quote(c) for c in cmd)}\n"
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if capture is not None:
            capture.append(line)
        yield line
    proc.wait()
    if result is not None:
        result.append(proc.returncode)
    yield f"\n[exit code {proc.returncode}]\n"


def ensure_clone() -> Iterator[str]:
    """Clones (or updates) ReliOps into DEFAULT_WORKDIR. Handles the two failure modes seen in
    practice: a leftover, non-empty directory from an earlier interrupted/failed setup attempt
    (git clone refuses to clone into it — we now clear it out first), and a clone that itself
    failed (we now stop before `git checkout`, instead of running it in a directory that was
    never actually a git repo and getting a confusing second error on top of the first)."""
    DEFAULT_WORKDIR.mkdir(parents=True, exist_ok=True)

    if (reliops_dir() / ".git").exists():
        yield "ReliOps clone already present — fetching the latest and switching to the working branch.\n"
        yield from _stream(["git", "fetch", "origin", RELIOPS_BRANCH], cwd=reliops_dir())
        yield from _stream(["git", "checkout", RELIOPS_BRANCH], cwd=reliops_dir())
        yield from _stream(["git", "pull", "--ff-only", "origin", RELIOPS_BRANCH], cwd=reliops_dir())
        return

    if reliops_dir().exists():
        yield (
            f"{reliops_dir()} exists but isn't a git clone (likely left over from an earlier "
            "failed setup attempt) — clearing it out before cloning fresh.\n"
        )
        shutil.rmtree(reliops_dir())

    clone_result: list[int] = []
    clone_output: list[str] = []
    yield from _stream(
        ["git", "clone", RELIOPS_REPO_URL, str(reliops_dir())],
        result=clone_result, capture=clone_output,
    )
    if clone_result and clone_result[0] != 0:
        joined = "".join(clone_output)
        auth_markers = (
            "could not read Username", "could not read Password", "Authentication failed",
            "terminal prompts disabled", "Repository not found", "403",
        )
        if any(m in joined for m in auth_markers):
            yield (
                "\nThat's a GitHub auth failure, not a network problem — ReliOps is a private repo, "
                "so a plain anonymous clone can't read it. Two things need to be true, once per "
                "machine (not once per run):\n"
                "  1. The repo owner adds you as a Collaborator: "
                "github.com/OmarElzero/ReliOps/settings/access — a private repo won't clone for "
                "anyone without access, even with a correct token.\n"
                "  2. Your machine has git credentials for GitHub that don't need a terminal prompt "
                "(this GUI can't show you one). Easiest: install the GitHub CLI and run "
                "`gh auth login` once — it wires up git for you. Or generate a Personal Access "
                "Token (classic, 'repo' scope) at github.com/settings/tokens, run "
                "`git config --global credential.helper store`, then clone the repo manually one "
                "time by hand, pasting your GitHub username and the token as the password when "
                "asked — after that, this tool's own clone will reuse the same saved credentials.\n"
            )
        yield "git clone failed — stopping before checkout (see the exit code above).\n"
        return
    yield from _stream(["git", "checkout", RELIOPS_BRANCH], cwd=reliops_dir())


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
    """Which of the known model-key providers already have a non-empty value saved — used by the
    GUI to show "already saved" instead of a blank field that looks unconfigured."""
    env_vars = merged_env_vars()
    return {p: bool(env_vars.get(p)) for p in KNOWN_PROVIDERS}


# Vars this tool reads/writes for its own reasons, not a model key a person typed into the "Model
# API key" field — left out of custom_saved_keys() so a "Custom…" provider's status isn't lost
# among unrelated plumbing.
_NON_MODEL_KEY_VARS = {
    "GOOGLE_APPLICATION_CREDENTIALS", "FIREBASE_SERVICE_ACCOUNT_JSON", "SERVICE_ACCOUNT_KEY",
    "AGENT_MODEL_ID", "REACT_V1_ACTION_SPACE", "SREGYM_AGENT_IMAGE",
}


def custom_saved_keys() -> list[str]:
    """Env var names already saved that aren't one of KNOWN_PROVIDERS — e.g. a self-hosted
    open-weight model's own key, typed into the "Custom…" option of the Model API key field (see
    frontend/index.html). Without this, a custom-named key you already saved would look
    unconfigured every time you reopen the GUI, since nothing else tracks it by name."""
    env_vars = merged_env_vars()
    return sorted(k for k in env_vars if k not in KNOWN_PROVIDERS and k not in _NON_MODEL_KEY_VARS and env_vars.get(k))


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


# Columns the Lab adds on top of the team's template. They are appended to whatever header the template
# has (and skipped if the template already lists them), so the template stays the single source of truth
# and this tool never silently drops the diagnosis text again.
LAB_EXTRA_COLUMNS = [
    "judge_model_id", "run_by", "run_class", "config_label", "experiment",
    "diagnosis_text", "judge_reasoning", "judge_checklist", "transcript_rel", "manifest_id",
]


def _run_log_header() -> list[str]:
    cols: list[str] = []
    template = reliops_dir() / "experiments" / "run_log_template.csv"
    try:
        first_line = template.read_text(encoding="utf-8").splitlines()[0]
        cols = [c.strip() for c in first_line.split(",") if c.strip()]
    except (OSError, IndexError):
        pass
    if not cols:
        cols = list(_RUN_LOG_HEADER_FALLBACK)
    return cols + [c for c in LAB_EXTRA_COLUMNS if c not in cols]


def _ensure_csv_header(csv_path: Path, header: list[str]) -> list[str]:
    """If results/gui_runs.csv already exists with a different (older) header, rewrite it under the
    new header so appended rows line up with their columns. Columns that exist only in the old file
    are kept at the end (never dropped). Returns the header to write rows with."""
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return header
    with csv_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        old_header = list(reader.fieldnames or [])
        if old_header == header:
            return header
        old_rows = list(reader)
    merged = header + [c for c in old_header if c not in header]
    tmp = csv_path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=merged, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(old_rows)
    tmp.replace(csv_path)
    return merged


def _parse_literal(value: object) -> object:
    """SREGym writes dict/list result values into the CSV with Python's repr (str(dict)), so a value
    read back from a CSV is a string like "{'D1': {'name': ..., 'score': 1.0}}". Try JSON first,
    then Python literal syntax; anything else is returned unchanged."""
    import ast

    if not isinstance(value, str) or not value.strip():
        return value
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(value)
        except (ValueError, SyntaxError):
            continue
    return value


def extract_diagnosis(row: dict | None) -> dict:
    """Pulls the diagnosis text the agent submitted, the judge's reasoning and checklist, and the
    D1-D3 scores out of SREGym's own result row.

    Key names checked against the SREGym source (conductor.py `_evaluate_diagnosis` sets
    r["submission"]; llm_as_a_judge_oracle.py sets reasoning, composite_score,
    dimensions={"D1": {"name", "score"}, ...} and checklist=[{id, answer, evidence, confidence}];
    main.py flattens one level into "Diagnosis.<key>" columns). Rows read back from a CSV carry the
    dict/list values as Python-repr strings, so they are parsed with _parse_literal. Anything not
    found is None, never guessed."""
    row = row or {}

    def first(*keys):
        for k in keys:
            v = row.get(k)
            if v not in (None, ""):
                return v
        return None

    dims = _parse_literal(row.get("Diagnosis.dimensions"))
    scores: dict[str, float | None] = {"D1": None, "D2": None, "D3": None}
    for d in scores:
        entry = dims.get(d) if isinstance(dims, dict) else None
        scores[d] = _as_float(entry.get("score") if isinstance(entry, dict) else entry)
        if scores[d] is None:  # a CSV that was flattened further than SREGym does today
            scores[d] = _as_float(row.get(f"Diagnosis.dimensions.{d}.score"))

    checklist = _parse_literal(row.get("Diagnosis.checklist"))
    return {
        "diagnosis_text": first("Diagnosis.submission"),
        "judge_reasoning": first("Diagnosis.reasoning"),
        "judge_checklist": json.dumps(checklist, default=str) if isinstance(checklist, list) and checklist else None,
        "diagnosis_d1": scores["D1"],
        "diagnosis_d2": scores["D2"],
        "diagnosis_d3": scores["D3"],
        "diagnosis_composite": _as_float(first("Diagnosis.composite_score")),
    }


def _as_bool(value: object) -> bool | None:
    """Same parsing SREGym's own sregym/results/report.py uses for its CSV's string-typed
    booleans — matched deliberately so our reading of its row agrees with its own tooling."""
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _as_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def normalize_sregym_row(row: dict | None) -> dict:
    """Pulls the few fields we can state with confidence out of SREGym's own raw result row.
    Verified against its source, not guessed: sregym/conductor/conductor.py sets
    `self.results[stage_name.capitalize()] = outcome` (so a stage's own fields land at
    "Diagnosis.<key>" / "Mitigation.<key>") and a top-level timing key per stage — "TTL" when the
    stage is "diagnosis", "TTM" otherwise (conductor.py ~line 802); sregym/results/report.py reads
    `row.get("Diagnosis.success")` / `row.get("Mitigation.success")` for pass/fail and computes
    "overall" as diagnosis-and-mitigation itself — there's no separate e2e/overall column in the
    raw row, so we compute it the same way it does. Anything not covered here (token counts, tool
    call counts, durability) isn't in this row at all as far as this reading goes, and is left
    blank rather than guessed."""
    row = row or {}
    diagnosis = _as_bool(row.get("Diagnosis.success"))
    mitigation = _as_bool(row.get("Mitigation.success"))
    incomplete = row.get("run_status") == "incomplete" or diagnosis is None or mitigation is None
    e2e = None if incomplete else (diagnosis and mitigation)
    return {
        "diagnosis_pass": diagnosis,
        "mitigation_pass": mitigation,
        "e2e_pass": e2e,
        "ttd_s": _as_float(row.get("TTL")),  # diagnosis-stage timing, per conductor.py
        "ttm_s": _as_float(row.get("TTM")),  # mitigation-stage timing, per conductor.py
        "run_status": row.get("run_status"),
        "deploy_failed": _as_bool(row.get("deploy_failed")),
    }


def append_csv_run(record: dict) -> Path:
    """Appends one row to results/gui_runs.csv in the team's canonical schema (same columns as
    experiments/run_log_template.csv), alongside the fuller results/gui_runs.jsonl this tool
    already wrote (that one keeps everything — run_by, the Firebase outcome, SREGym's raw row
    untouched — this CSV is the trimmed, standard-shape version meant to sit next to the rest of
    the team's results/*.csv files). A column the template expects that SREGym's own row didn't
    return, or that this GUI doesn't know, is left blank — never guessed (diagnosis_pass,
    mitigation_pass, e2e_pass, ttd_s and ttm_s are the exception — see normalize_sregym_row)."""
    header = _run_log_header()
    row_data = dict(record.get("row") or {})  # SREGym's own metrics.md-shaped row, if present
    normalized = normalize_sregym_row(record.get("row"))
    # What the GUI itself knows for certain always wins over whatever (if anything) `row` has
    # under the same name — these came straight from the form, not a derived/parsed value.
    row_data.update({
        "run_id": record.get("run_id", ""),
        "timestamp_utc": record.get("timestamp_utc", ""),
        "problem_id": record.get("problem_id", ""),
        "agent_id": record.get("agent_id", ""),
        "model_id": record.get("model_id", ""),
        "action_space": record.get("action_space") or "",
        "repeat_n": record.get("repeat_n", ""),
        "diagnosis_pass": normalized["diagnosis_pass"],
        "mitigation_pass": normalized["mitigation_pass"],
        "e2e_pass": normalized["e2e_pass"],
        "ttd_s": normalized["ttd_s"],
        "ttm_s": normalized["ttm_s"],
        # Fields the Lab itself records (not in SREGym's row): who/what configuration, and the text
        # that lets a better judge re-grade this run later.
        "judge_model_id": record.get("judge_model_id", ""),
        "run_by": record.get("run_by", ""),
        "run_class": record.get("run_class", ""),
        "config_label": record.get("config_label", ""),
        "experiment": record.get("experiment", ""),
        "diagnosis_text": record.get("diagnosis_text"),
        "judge_reasoning": record.get("judge_reasoning"),
        "judge_checklist": record.get("judge_checklist"),
        "manifest_id": record.get("manifest_id"),
        "transcript_rel": record.get("transcript_rel"),
    })
    for col in ("diagnosis_d1", "diagnosis_d2", "diagnosis_d3", "diagnosis_composite", "git_commit",
                "prompt_tokens", "completion_tokens", "total_tokens", "num_llm_calls", "num_tool_calls"):
        if record.get(col) is not None:
            row_data[col] = record[col]
    csv_path = reliops_dir() / "results" / "gui_runs.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    header = _ensure_csv_header(csv_path, header)
    out_row = {col: ("" if row_data.get(col) is None else row_data.get(col)) for col in header}
    is_new = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerow(out_row)
    return csv_path


def list_local_runs(limit: int = 300) -> list[dict]:
    """Reads this machine's own results/gui_runs.jsonl for the dashboard tab — the fallback when
    Firebase isn't connected (or the team hasn't shared a key), so "all runs" still means
    something: just scoped to this laptop instead of the whole team. Newest first."""
    jsonl_path = reliops_dir() / "results" / "gui_runs.jsonl"
    if not jsonl_path.is_file():
        return []
    records = []
    with jsonl_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    records.sort(key=lambda r: r.get("timestamp_utc", ""), reverse=True)
    return records[:limit]


def new_log_file(kind: str) -> Path:
    """Opens a fresh timestamped log file under results/gui_logs/ for one Setup/Demo/Run action,
    so the full raw output survives past the browser tab closing or the log panel being cleared —
    for debugging a problem after the fact, not just while it's on screen."""
    log_dir = reliops_dir() / "results" / "gui_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return log_dir / f"{stamp}_{kind}.log"


# Set by /api/stop, read by stream_campaign — a plain module-level flag is enough because this
# tool only ever drives one run at a time on one person's own machine (not a shared server with
# concurrent campaigns), so there's no "whose run am I stopping" ambiguity to track.
_stop_event = threading.Event()


def request_stop() -> None:
    """Asks the campaign currently in stream_campaign() to stop after whatever problem it's in the
    middle of finishes and its result is recorded — never mid-subprocess. So "stop while problem
    2/5 is running" means: let 2 finish, record it, then skip 3-5, not kill anything half-done."""
    _stop_event.set()


def _problem_log_path(problem: str, rep: int, repeat_n: int) -> Path:
    """A dedicated log file for one (problem, repeat) run, separate from the combined per-request
    SSE log app.py already writes — so a single run can be reopened and read on its own later
    (what actually happened, in what order, any retries/timeouts) without scrolling through a
    whole batch's output to find it. Stored next to the other logs, named so it's obvious which
    run it belongs to without opening it."""
    log_dir = reliops_dir() / "results" / "gui_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    suffix = f"_rep{rep}of{repeat_n}" if repeat_n > 1 else ""
    safe_problem = "".join(c if (c.isalnum() or c in "-_") else "_" for c in problem)
    return log_dir / f"{stamp}_{safe_problem}{suffix}.log"


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


def _write_key_to(env_path: Path, provider: str, value: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
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


def write_key(provider: str, value: str) -> None:
    """Saves an API key into this tool's own .env (lab_env_path()) — the file next to backend/
    and frontend/, stays on this machine, never sent anywhere by this tool. Also mirrors it into
    the cloned ReliOps repo's .env, once that clone exists, because scripts/quickstart.sh reads
    its own copy of that file directly off disk (see _sync_model_keys_to_clone's docstring) — so a
    key saved here still works for Setup without you having to save it twice."""
    _write_key_to(lab_env_path(), provider, value)
    if reliops_dir().exists():
        _write_key_to(reliops_dir() / ".env", provider, value)


def _sync_model_keys_to_clone() -> None:
    """Mirrors just the known model-key vars from this tool's own .env (lab_env_path()) into the
    cloned ReliOps repo's .env. scripts/quickstart.sh checks for and reads ReliOps/.env directly
    off disk during Setup — it has no way to know about keys saved anywhere else — so without this,
    Setup would find no .env there, create a blank one from .env.example, and stop asking you to
    fill in a key you'd already saved in this tool."""
    lab_vars = read_env_file(lab_env_path())
    for provider in KNOWN_PROVIDERS:
        value = lab_vars.get(provider)
        if value:
            _write_key_to(reliops_dir() / ".env", provider, value)


def stream_setup() -> Iterator[str]:
    yield from ensure_clone()
    quickstart = reliops_dir() / "scripts" / "quickstart.sh"
    if not quickstart.is_file():
        yield (
            f"no {quickstart} found — the clone above didn't finish, so there's nothing to run "
            "yet. Fix whatever the git step above reported and click Run setup again.\n"
        )
        return
    _sync_model_keys_to_clone()
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
    parsed row still has its full raw log; it is never reported as pass/fail without this row.

    Uses csv.DictReader on the whole file, not a naive `line.split(",")` on each line (what this
    used to do). SREGym's own row embeds the judge's full reasoning/checklist as a quoted
    comma-and-newline-filled blob in one field — a raw split shifts every column after it out of
    alignment, so `row.get("problem_id")` silently stops matching. That's why a run could show
    "no row parsed" even right after SREGym itself printed that it wrote the CSV: the file was
    there and readable, this just wasn't parsing it correctly."""
    results_root = sregym_dir() / "results"
    if not results_root.is_dir():
        return None
    candidates = sorted(results_root.glob("*/*_ALL_results.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    for csv_path in candidates:
        if csv_path.stat().st_mtime < after_ts - 5:
            break
        try:
            with csv_path.open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
        except (OSError, csv.Error):
            continue
        for row in reversed(rows):
            if row.get("problem_id") == problem or row.get("problem") == problem:
                return row
    return None


def _find_transcript(problem: str, after_ts: float) -> str | None:
    """Path (relative to the SREGym clone) of the react-v1 transcript this run wrote, if any. Only
    reliops_v1 writes one; other agents return None and simply have no story."""
    root = sregym_dir()
    best: tuple[float, Path] | None = None
    for p in (root / "results").glob(f"*/*/{problem}/run_*/react_v1_transcript.jsonl"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m >= after_ts - 5 and (best is None or m > best[0]):
            best = (m, p)
    return str(best[1].relative_to(root)) if best else None


def namespace_for(problem: str) -> str | None:
    """Kubernetes namespace to record for a problem, taken from the problem set's `app` column
    (hotel-reservation -> namespace hotel-reservation, verified in a real run log; other apps use
    the same convention but are unverified). The recorder tolerates a namespace that does not
    exist yet or at all: it just records nothing."""
    for s in problem_sets():
        for p in s.get("problems", []):
            if p["id"] == problem and p.get("app"):
                return p["app"]
    return None


def walkthrough_dir(run_id: str) -> Path:
    return reliops_dir() / "results" / "gui_walkthrough" / run_id


def _start_recorder(namespace: str | None, out_path: Path):
    script = reliops_dir() / "scripts" / "cluster_recorder.py"
    if not namespace or not script.is_file():
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return subprocess.Popen(
            [sys.executable, str(script), "-n", namespace, "--out", str(out_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def _stop_recorder(proc) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001 - never let cleanup break a finished run
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _git_commit_safe() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=reliops_dir(), capture_output=True, text=True, timeout=20)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def transcript_stats(transcript_rel: str | None) -> dict:
    """Token and step counts from a reliops_v1 transcript (the template's own column names). Empty dict
    for agents that write no transcript; those runs simply have no cost numbers."""
    if not transcript_rel:
        return {}
    path = sregym_dir() / transcript_rel
    if not path.is_file():
        return {}
    prompt = completion = calls = actions = 0
    seen = False
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "model_call":
                    seen = True
                    calls += 1
                    u = rec.get("usage") or {}
                    prompt += int(u.get("prompt_tokens") or 0)
                    completion += int(u.get("completion_tokens") or 0)
                elif rec.get("type") == "action":
                    actions += 1
    except OSError:
        return {}
    if not seen:
        return {}
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion,
            "num_llm_calls": calls, "num_tool_calls": actions}


def cards_dir() -> Path:
    return reliops_dir() / "experiments" / "problem-cards"


def list_cards() -> list[dict]:
    """Problem cards in the ReliOps clone (experiments/problem-cards/<problem_id>.md; TEMPLATE.md and
    README are not cards). The file stays the single source of truth; the Lab only displays it."""
    out = []
    d = cards_dir()
    if d.is_dir():
        for p in sorted(d.glob("*.md")):
            if p.stem.upper() in ("TEMPLATE", "README"):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^Status:\s*\**([A-Za-z-]+)", text, re.M)
            out.append({"problem_id": p.stem, "status": (m.group(1).lower() if m else "unknown")})
    return out


def read_card(problem_id: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", problem_id or ""):
        return None
    p = cards_dir() / f"{problem_id}.md"
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None


def card_section(card: str | None, number: int) -> str | None:
    """Text of '## <number>. ...' up to the next '## ' heading; used to show 'what a correct diagnosis
    must say' next to a diagnosis a person is hand-labelling."""
    if not card:
        return None
    m = re.search(rf"^## {number}\..*?$(.*?)(?=^## |\Z)", card, re.M | re.S)
    return m.group(0).strip() if m else None


def stream_rejudge(judge_model: str, items: list[dict], on_result=None, script: Path | None = None) -> Iterator[str]:
    """Re-judges stored diagnoses with `judge_model` by running scripts/rejudge.py inside SREGym's own
    environment (so the reference answer is SREGym's, and only the judge changes). `items` are dicts with
    run_id, problem_id, submission. Each verdict is handed to `on_result` as it is read back."""
    if not items:
        yield "nothing to re-judge\n"
        return
    script = script or (reliops_dir() / "scripts" / "rejudge.py")
    if not script.is_file():
        yield f"{script} not found: git pull the ReliOps clone.\n"
        return
    work = reliops_dir() / "results" / "gui_rejudge_work"
    work.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    inp, out = work / f"{stamp}_in.jsonl", work / f"{stamp}_out.jsonl"
    inp.write_text("\n".join(json.dumps(i) for i in items) + "\n", encoding="utf-8")
    env = {"JUDGE_MODEL_ID": judge_model, "PYTHONPATH": str(sregym_dir())}
    for k, v in merged_env_vars().items():
        env.setdefault(k, v)
    cmd = ["uv", "run", "python", str(script), "--in", str(inp), "--out", str(out)]
    yield from _stream(cmd, cwd=sregym_dir(), env=env)
    n = 0
    if out.is_file():
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                continue
            if v.get("judge_model_id") == "dry-run":
                continue  # never store the script's test verdicts as if a judge had said them
            dims = v.get("dimensions") or {}
            rec = {"run_id": v.get("run_id"), "problem_id": v.get("problem_id"), "judge_model_id": judge_model,
                   "success": v.get("success"), "judgment": v.get("judgment"),
                   "composite_score": v.get("composite_score"),
                   "d1": (dims.get("D1") or {}).get("score"), "d2": (dims.get("D2") or {}).get("score"),
                   "d3": (dims.get("D3") or {}).get("score"), "reasoning": v.get("reasoning"),
                   "checklist": json.dumps(v.get("checklist") or [], default=str), "error": v.get("error")}
            if on_result:
                on_result(rec)
            n += 1
    yield f"----- stored {n} re-judge verdict(s) from {judge_model} -----\n"


def render_story(record: dict) -> str:
    """Markdown story (agent steps interleaved with cluster events) for one recorded run. Uses
    scripts/replay_trace.py from the ReliOps clone, so there is one renderer for the Lab and the
    command line. Raises FileNotFoundError with a plain explanation when the pieces are missing."""
    import importlib.util

    rel = record.get("transcript_rel")
    if not rel:
        raise FileNotFoundError("This run has no agent transcript (only reliops_v1 writes one).")
    transcript = sregym_dir() / rel
    if not transcript.is_file():
        raise FileNotFoundError(f"Transcript not found on this machine: {transcript}")
    script = reliops_dir() / "scripts" / "replay_trace.py"
    if not script.is_file():
        raise FileNotFoundError("scripts/replay_trace.py is missing; git pull the ReliOps clone.")
    spec = importlib.util.spec_from_file_location("replay_trace", script)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    cluster = walkthrough_dir(record.get("run_id", "")) / "cluster.jsonl"
    return mod.render_story(str(transcript), str(cluster) if cluster.is_file() else None)


def stream_campaign(
    *,
    agent: str,
    action_space: str | None,
    model: str,
    judge_model: str,
    problems: list[str],
    repeat_n: int = 1,
    force_build: bool,
    run_by: str | None = None,
    config_label: str | None = None,
    run_class: str = "exploration",
    record_walkthrough: bool = True,
    cells: list[tuple[str, int]] | None = None,
    manifest_id: str | None = None,
    experiment: str | None = None,
    on_result=None,
) -> Iterator[str]:
    """Runs one problem at a time (SREGym itself is single-run per process) and yields log lines
    for all of them in order, with a banner between problems. Each problem runs `repeat_n` times
    in a row (repeat_n > 1 is how you get a real n for noisy metrics like time-to-diagnose — see
    the canonical schema's `repeat_n` column). `on_result(record)` is called after each individual
    run with whatever row we could recover — the GUI uses it to push to Firestore."""
    if not sregym_dir().exists():
        yield "SREGym clone not found — run Setup first.\n"
        return

    _stop_event.clear()  # a fresh "Run test(s)" click always starts clean, even after a past stop
    repeat_n = max(1, repeat_n)
    base_env = {"AGENT_MODEL_ID": model}
    if action_space:
        base_env["REACT_V1_ACTION_SPACE"] = action_space
    # merged_env_vars() (this tool's own .env, falling back to the clone's) rather than reading
    # reliops_dir()/.env directly — these go straight into the subprocess's env via Popen, so the
    # actual test run gets your key whether you saved it here or edited the clone's .env by hand.
    for k, v in merged_env_vars().items():
        base_env.setdefault(k, v)

    pool = key_pool(base_env)
    for w in preflight_warnings(base_env, model, agent):
        yield w + "\n"
    via_base = bool((base_env.get("AGENT_API_BASE") or "").strip())
    streak_limit = max_incomplete_streak(base_env)
    incomplete_streak = 0

    # `cells` (problem, repeat) pairs override the plain grid: used to run only the missing cells of a
    # frozen campaign, or one machine's slice of it.
    runs = list(cells) if cells else [(problem, rep) for problem in problems for rep in range(1, repeat_n + 1)]
    total_runs = len(runs)
    commit = _git_commit_safe()
    for i, (problem, rep) in enumerate(runs, start=1):
        repeat_note = f" repeat={rep}/{repeat_n}" if repeat_n > 1 else ""
        key_slot = None
        key_var, key_owner = ("GEMINI_API_KEY", "you") if base_env.get("GEMINI_API_KEY") else (None, None)
        picked = None
        while True:
            usable = True
            if len(pool) >= 2:
                try:
                    picked = next_key(pool)
                except KeyPoolEmpty:
                    usable = False
            elif key_var and not key_available(key_var):
                usable = False
            if usable:
                break
            wake = pool_wait_until(pool if len(pool) >= 2 else [{"var": key_var}])
            if wake is None:
                yield "[key] the key is marked dead. Stopping here; replace it or clear the mark, then run the queue again to resume.\n"
                return
            yield f"[key] key quota is used up. Waiting until {time.strftime('%H:%M UTC', time.gmtime(wake))}, then the queue continues by itself.\n"
            while time.time() < wake and not _stop_event.is_set():
                time.sleep(5)
            if _stop_event.is_set():
                return
        if picked:
            slot, k = picked
            key_slot = f"{slot + 1}/{len(pool)} {k['owner']}"
            key_var, key_owner = k["var"], k["owner"]
            yield f"[key] this run uses key {key_slot}\n"
        key_problem = None
        yield (
            f"\n===== [{i}/{total_runs}] problem={problem}{repeat_note} agent={agent} "
            f"action_space={action_space or '-'} =====\n"
        )
        started_at = time.time()
        run_id = str(__import__("uuid").uuid4())
        recorder = None
        if record_walkthrough:
            # Cluster timeline for this run's story; read-only kubectl polling, see scripts/cluster_recorder.py.
            recorder = _start_recorder(namespace_for(problem), walkthrough_dir(run_id) / "cluster.jsonl")
        env = dict(base_env)
        if picked:
            env[pool[0]["var"]] = picked[1]["value"]
        if os.environ.get("REACT_V1_LOG_OBS") is None:
            env["REACT_V1_LOG_OBS"] = "on"  # record what each command returned, for the story
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

        # Tee this one problem's output into its own log file as it streams, in addition to the
        # combined per-request SSE log app.py writes — so this exact run's log can be reopened and
        # read on its own from the Results tab later (see _problem_log_path's docstring).
        problem_log_path = _problem_log_path(problem, rep, repeat_n)
        try:
            with problem_log_path.open("w", encoding="utf-8") as plog:
                for line in _stream(cmd, cwd=sregym_dir(), env=env):
                    plog.write(line)
                    if key_var and key_problem is None:
                        key_problem = classify_key_problem(line, via_custom_base=via_base)
                    yield line
        finally:
            _stop_recorder(recorder)  # also runs if the browser tab closes mid-run
        if key_problem and key_var and KEY_EVENT_SINK:
            KEY_EVENT_SINK(key_var, key_owner, key_problem["status"], key_problem["until"], key_problem["reason"])
            when = "" if not key_problem["until"] else " until " + time.strftime("%H:%M UTC", time.gmtime(key_problem["until"]))
            yield f"[key] marked {key_owner} ({key_var}) {key_problem['status']}{when}: {key_problem['reason']}\n"
        row = _latest_result_row(problem, started_at)
        diagnosis = extract_diagnosis(row)
        transcript_rel = _find_transcript(problem, started_at)
        record = {
            "run_id": run_id,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "problem_id": problem,
            "agent_id": agent,
            "action_space": action_space,
            "model_id": model,
            "judge_model_id": judge_model,
            "repeat_n": repeat_n,
            "repeat_index": rep,
            # Self-reported, not an authenticated identity — see README's "Shared results" section.
            # Whoever ran this typed it into the "Your name" field; it's a label for the team to
            # read, not an access-control boundary (the shared Firebase key has full access either way).
            "run_by": run_by or "unknown",
            # "exploration" = any config, any time; "frozen" = the agent/judge/problem set were frozen
            # for a measured campaign. The results tables keep the two apart so they are never averaged.
            "run_class": run_class if run_class in ("exploration", "frozen") else "exploration",
            "config_label": (config_label or "").strip(),
            # What this run belongs to, in the person's own words (one experiment can hold many runs and queue starts).
            "experiment": (experiment or "").strip(),
            "transcript_rel": transcript_rel,
            "manifest_id": manifest_id,
            "git_commit": commit,
            "key_slot": key_slot,
            **transcript_stats(transcript_rel),
            **diagnosis,  # diagnosis_text, judge_reasoning, diagnosis_d1..d3, diagnosis_composite
            "row": row,  # SREGym's own metrics.md-shaped row, or None if we couldn't locate it
            "log_file": problem_log_path.name,  # served by GET /api/log/<name> — this run's own log
        }
        if on_result:
            on_result(record)
        found_note = "parsed row found" if row else "no row parsed — check the raw log above"
        repeat_suffix = f" (repeat {rep}/{repeat_n})" if repeat_n > 1 else ""
        yield f"----- recorded result for {problem}: {found_note}{repeat_suffix} -----\n"

        incomplete_streak = incomplete_streak + 1 if run_incomplete(row) else 0
        if streak_limit and incomplete_streak >= streak_limit and i < total_runs and not _stop_event.is_set():
            yield (
                f"\n[stopped: {incomplete_streak} runs in a row ended incomplete, which usually means the setup is broken "
                "(model credential, stale agent image, cluster). The queue stops here instead of spending the night on empty runs. "
                "Look at the last log, fix it, then run the queue again; finished runs are kept and only missing cells run. "
                "Set RELIOPS_MAX_INCOMPLETE=0 in .env to turn this stop off.]\n"
            )
            return

        if _stop_event.is_set():
            remaining = total_runs - i
            yield (
                f"\n[stopped — {i}/{total_runs} run(s) done, {remaining} skipped. "
                "Every finished run above is already recorded; click Run test(s) again to pick up "
                "the rest later.]\n"
            )
            return
