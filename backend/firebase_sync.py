"""Optional Firestore sync, matching the `runs/{id}` collection in
docs/plan/05-platform-spec.md §4 of the ReliOps repo (jobId/campaignId are left null here since
this GUI runs ad-hoc tests, not full campaigns yet — same collection, so results from this tool
and from the future job-queue runner end up side by side without a migration).

This tool's own `.env` — next to `backend/` and `frontend/`, the file you actually have open in
your editor (see `runner.lab_dir()`) — is the *only* source of credentials; there is no GUI field
or "Connect" button to keep in sync with it, by design (an earlier version had both, and they
drifted; an even earlier version read a *different* `.env` inside the separately-cloned ReliOps
repo, which nobody was looking at — see `runner.merged_env_vars()`). Checked in order:
  1. `GOOGLE_APPLICATION_CREDENTIALS=<path>` in `.env` — a path to the downloaded JSON file. A
     Windows/WSL-style path (`\\wsl.localhost\\...`, `C:\\...`) is normalized automatically, since
     this backend runs on Linux.
  2. `FIREBASE_SERVICE_ACCOUNT_JSON={...}` or `SERVICE_ACCOUNT_KEY={...}` in `.env` — the JSON
     file's contents pasted directly, no file or path needed at all.
  3. The default suggested file (runner.default_firebase_key_path()), if it happens to exist.
Nothing is sent anywhere unless one of these resolves to real credentials — with none, every run
is still saved locally (see runner.py), just not mirrored to Firestore. Credential content is
never read into a log, never printed, and `.env`/`*.json` key files must stay out of git (see
.gitignore). The frontend polls /api/firebase-status every few seconds so editing `.env` by hand
(or running Setup, which may create it) is picked up with no extra step.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_client = None
_status = "not configured"
_app = None
_resolved_label: str | None = None  # what we're connected with, so a repeat poll is a no-op
APP_NAME = "reliops-lab"


def _credential_from_env() -> tuple[Any, str] | None:
    """Looks in this tool's own .env (reliops_lab/.env — see runner.lab_dir()) for either a
    credentials path or inline JSON, falling back to the cloned ReliOps repo's .env for anything
    the lab .env doesn't set. Returns (credential_arg_for_firebase_admin, human-readable source)
    or None if nothing usable is there."""
    import runner  # local import to avoid a circular import at module load time

    env_vars = runner.merged_env_vars()

    path = env_vars.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if path:
        path = _normalize_windows_style_path(path)
        if Path(path).is_file():
            return path, f"path from .env ({path})"

    for var in ("FIREBASE_SERVICE_ACCOUNT_JSON", "SERVICE_ACCOUNT_KEY"):
        raw = env_vars.get(var)
        if raw:
            try:
                return json.loads(raw), f"inline JSON from .env ({var})"
            except json.JSONDecodeError as e:
                return None, f"bad-json:{var}:{e}"

    default_path = runner.default_firebase_key_path()
    if default_path.is_file():
        return str(default_path), f"default file ({default_path})"

    return None


def _connect(cred_source: Any, label: str) -> None:
    global _client, _status, _app
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if isinstance(cred_source, dict):
            cred = credentials.Certificate(cred_source)
        else:
            cred = credentials.Certificate(str(cred_source))

        # Always re-init under our own named app, so switching credentials (e.g. a bad path
        # fixed, or a different key tried) actually takes effect instead of silently keeping
        # whatever connected first — this was the "Connect does nothing" bug.
        if _app is not None:
            try:
                firebase_admin.delete_app(_app)
            except ValueError:
                pass
        _app = firebase_admin.initialize_app(cred, name=APP_NAME)
        _client = firestore.client(_app)
        _status = f"connected ({label})"
    except Exception as e:  # noqa: BLE001 — surfaced to the GUI, not swallowed
        _client = None
        _status = f"connection failed: {e}"


def _normalize_windows_style_path(raw: str) -> str:
    """This backend runs on Linux (including inside WSL). A path copied from Windows Explorer —
    either a real Windows path (C:\\Users\\...) or a \\\\wsl.localhost\\<distro>\\... one pointing
    right back into this same Linux filesystem — isn't openable as-is. Both are mechanical,
    well-defined conversions, so just do them instead of making the person do it:
      \\\\wsl.localhost\\<distro>\\rest\\of\\path  ->  /rest/of/path  (already this Linux box)
      \\\\wsl$\\<distro>\\rest\\of\\path            ->  /rest/of/path  (older WSL alias, same idea)
      C:\\Users\\name\\file                        ->  /mnt/c/Users/name/file  (Windows drive mount)
    Anything else (already a normal /… path) passes through untouched."""
    s = raw.strip().strip('"')
    for prefix in ("\\\\wsl.localhost\\", "\\\\wsl$\\"):
        if s.startswith(prefix):
            rest = s[len(prefix):]
            # rest starts with "<distro>\..." — drop the distro name, keep what's after it
            _, _, after_distro = rest.partition("\\")
            return "/" + after_distro.replace("\\", "/")
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        drive = s[0].lower()
        return f"/mnt/{drive}/" + s[3:].replace("\\", "/")
    return s


def try_auto_connect() -> str:
    """Called on startup and on every /api/firebase-status poll: (re-)detect from .env, so a
    teammate who has GOOGLE_APPLICATION_CREDENTIALS, FIREBASE_SERVICE_ACCOUNT_JSON,
    SERVICE_ACCOUNT_KEY or the default file in place is connected with no manual step at all.
    Re-resolves .env on every call (cheap — a small file read) but only actually reconnects to
    Firebase when the resolved source changed, so a poll with nothing new to say isn't also
    silently re-authenticating every few seconds."""
    global _status, _client, _resolved_label
    found = _credential_from_env()
    if found is None:
        _client = None
        _resolved_label = None
        _status = "not configured — no credentials found in .env or the default file"
        return _status
    cred_source, label = found
    if label.startswith("bad-json:"):
        _client = None
        _resolved_label = None
        _status = f"connection failed: {label.split(':', 2)[2]} (check the JSON you pasted into .env)"
        return _status
    if _client is not None and label == _resolved_label:
        return _status  # same credentials as last time — nothing to do
    _connect(cred_source, label)
    _resolved_label = label if _client is not None else None
    return _status


def status() -> str:
    return try_auto_connect()


def list_runs(limit: int = 300) -> list[dict[str, Any]] | None:
    """Fetches the most recent `runs/{id}` docs for the dashboard tab — the whole team's runs,
    since they all write to this same collection. Returns None (not an empty list) when Firebase
    isn't connected, so the caller can tell "no team data available" apart from "connected, team
    just hasn't run anything yet" and fall back to this machine's own local results."""
    status()  # re-resolve from .env first, same as every other call — see try_auto_connect
    if _client is None:
        return None
    try:
        docs = (
            _client.collection("runs")
            .order_by("timestamp_utc", direction="DESCENDING")
            .limit(limit)
            .stream()
        )
        return [d.to_dict() for d in docs]
    except Exception:  # noqa: BLE001 — a query hiccup falls back to local, not an error page
        return None


def write_run(record: dict[str, Any]) -> str:
    """Writes one `runs/{id}` doc. Returns a human-readable outcome; never raises — a Firestore
    hiccup must not take down a live test run, it only means that one result stays local-only."""
    if _client is None:
        return "not written: Firebase not connected (local copy is still saved)"
    try:
        doc = {
            "jobId": None,
            "campaignId": None,
            "source": "reliops-lab-gui-v0",
            **record,
        }
        self_id = record.get("run_id")
        _client.collection("runs").document(self_id).set(doc)
        return "written to Firestore runs/" + str(self_id)
    except Exception as e:  # noqa: BLE001
        return f"not written: {e} (local copy is still saved)"


def write_doc(collection: str, doc_id: str, doc: dict[str, Any]) -> str:
    """Generic mirror for the small shared collections (reviews, labels, rejudges, manifests).
    Never raises; a Firestore hiccup only means the record stays local-only."""
    if _client is None:
        return "not written: Firebase not connected (local copy is still saved)"
    try:
        _client.collection(collection).document(doc_id).set(doc)
        return f"written to Firestore {collection}/{doc_id}"
    except Exception as e:  # noqa: BLE001
        return f"not written: {e} (local copy is still saved)"


def list_docs(collection: str, limit: int = 5000) -> list[dict[str, Any]] | None:
    """All docs of a small shared collection, or None when Firebase is not connected."""
    status()
    if _client is None:
        return None
    try:
        return [d.to_dict() for d in _client.collection(collection).limit(limit).stream()]
    except Exception:  # noqa: BLE001
        return None


STORY_MAX_BYTES = 900_000  # a Firestore document is limited to 1 MiB


def write_story(run_id: str, text: str) -> str:
    """Shares a rendered story (Markdown, observations already clipped) so teammates on other
    machines can read it. Truncated at ~900 KB with a visible note rather than failing."""
    data = text.encode("utf-8")
    if len(data) > STORY_MAX_BYTES:
        text = data[:STORY_MAX_BYTES].decode("utf-8", errors="ignore") + "\n\n[story truncated to fit the shared store]\n"
    return write_doc("stories", run_id, {"run_id": run_id, "text": text})


def read_story(run_id: str) -> str | None:
    status()
    if _client is None:
        return None
    try:
        doc = _client.collection("stories").document(run_id).get()
        return (doc.to_dict() or {}).get("text") if doc.exists else None
    except Exception:  # noqa: BLE001
        return None
