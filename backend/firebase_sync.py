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
import threading
import time
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
            parsed, error = parse_service_account_json(raw)
            if parsed is not None:
                return parsed, f"inline JSON from .env ({var})"
            return None, f"bad-json:{var}:{error}"

    default_path = runner.default_firebase_key_path()
    if default_path.is_file():
        return str(default_path), f"default file ({default_path})"

    return None


def parse_service_account_json(raw: str) -> tuple[dict | None, str]:
    """Reads the service-account JSON however it was pasted into .env: bare, in single or double
    quotes, with the inner quotes escaped (`\\"`), or followed by a comment. Returns (dict, "") or
    (None, reason). The key itself is never included in the reason."""
    text = raw.strip()
    candidates = [text]
    if "{" in text and "}" in text:
        core = text[text.index("{"): text.rindex("}") + 1]   # drops outer quotes and trailing comments
        candidates += [core, core.replace('\\"', '"')]
    first_error = ""
    for c in candidates:
        try:
            value = json.loads(c)
        except json.JSONDecodeError as e:
            first_error = first_error or str(e)
            continue
        if isinstance(value, dict):
            return value, ""
    return None, first_error or "not a JSON object"


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


# --- reading ---------------------------------------------------------------------------------
# Reads used to stream the whole `runs` collection (and each small collection) on every dashboard
# refresh, and any failure silently fell back to this machine's own runs. Two things follow from
# that, and both showed up as "my teammates' runs and names disappeared": (1) the free Firestore plan
# allows 50,000 reads a day for the whole project, which a few people clicking filters can use up,
# and (2) the page then said "Firebase is not connected" although it was. So reads are now:
#   - incremental: one full read, then only documents newer than the last one seen (a full re-read
#     every FULL_REFRESH_S to pick up deletions);
#   - remembered: the last good copy is kept in memory and in results/gui_remote_cache.json, and is
#     shown (marked as a saved copy, with the error) when Firestore cannot be read;
#   - explained: the last error per collection is available through sync_info().
CHECK_TTL_S = 20          # no new query to Firestore more often than this
FULL_REFRESH_S = 1800     # re-read everything this often
FULL_CAP = 5000           # newest documents kept per collection
_ORDER_FIELD = {"runs": "timestamp_utc"}   # every other collection uses "updated_at"

_snap: dict[str, dict] = {}
_errors: dict[str, str] = {}
_disk_loaded = False
_lock = threading.RLock()


def _cache_path() -> Path:
    import runner

    return runner.reliops_dir() / "results" / "gui_remote_cache.json"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_disk() -> None:
    global _disk_loaded
    if _disk_loaded:
        return
    _disk_loaded = True
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for name, snap in (data or {}).items():
        if isinstance(snap, dict) and isinstance(snap.get("docs"), dict):
            _snap.setdefault(name, {"docs": snap["docs"], "full_at": 0, "checked_at": 0,
                                    "synced_at": snap.get("synced_at"), "last": snap.get("last") or ""})


def _save_disk() -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {n: {"docs": s["docs"], "synced_at": s.get("synced_at"), "last": s.get("last") or ""}
                   for n, s in _snap.items()}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # the cache is a convenience; never fail a read because it cannot be saved


def explain_error(e: Exception | str) -> str:
    """A plain-language reason for a failed Firestore call (no credentials in it)."""
    text = str(e)
    low = (type(e).__name__ + " " + text).lower()
    if "quota" in low or "resourceexhausted" in low or "429" in low:
        return ("Firestore read quota used up for today (the free plan allows 50,000 reads a day for the whole "
                "project; it resets at midnight Pacific time). " + text[:160])
    if "permission" in low or "unauthenticated" in low or "403" in low or "401" in low:
        return "Firestore refused the credentials (permission denied): " + text[:160]
    if "unavailable" in low or "deadline" in low or "timed out" in low or "connection" in low:
        return "Firestore could not be reached (network): " + text[:160]
    return text[:200]


def _sync(collection: str, force: bool = False) -> dict | None:
    """The newest snapshot of one collection: live when Firestore answers, otherwise the last saved
    copy (or None if there never was one). Never raises; the reason for a failure is kept in _errors."""
    with _lock:
        _load_disk()
        snap = _snap.get(collection)
        if _client is None:
            _errors[collection] = "Firebase is not connected (no credentials found in .env)"
            return snap
        now = time.time()
        if snap and not force and snap.get("full_at") and now - snap.get("checked_at", 0) < CHECK_TTL_S:
            return snap
        field = _ORDER_FIELD.get(collection, "updated_at")
        try:
            col = _client.collection(collection)
            need_full = (not snap or not snap.get("full_at") or not snap.get("last")
                         or now - snap["full_at"] > FULL_REFRESH_S)
            if need_full:
                query = col.order_by(field, direction="DESCENDING") if collection == "runs" else col
                docs = {d.id: d.to_dict() for d in query.limit(FULL_CAP).stream()}
                changed = not snap or snap.get("docs") != docs
                snap = {"docs": docs, "full_at": now}
            else:
                new = {d.id: d.to_dict() for d in col.where(field, ">=", snap["last"]).stream()}
                changed = any(snap["docs"].get(k) != v for k, v in new.items())
                snap["docs"].update(new)
            snap["checked_at"] = now
            snap["synced_at"] = _now_iso()
            snap["last"] = max((str(v.get(field) or "") for v in snap["docs"].values()), default="")
            _snap[collection] = snap
            _errors.pop(collection, None)
            if changed:
                _save_disk()
        except Exception as e:  # noqa: BLE001 - kept and shown, not swallowed
            _errors[collection] = explain_error(e)
        return _snap.get(collection)


def _touch(collection: str) -> None:
    """After a write of ours: the next read asks Firestore for what is new, so our own rows show at once."""
    with _lock:
        if collection in _snap:
            _snap[collection]["checked_at"] = 0


def sync_info(collection: str = "runs") -> dict[str, Any]:
    """What the Results banner needs: is Firebase connected, is the copy shown live or saved, and why not."""
    with _lock:
        snap = _snap.get(collection)
        err = _errors.get(collection)
        return {"connected": _client is not None, "status": _status, "error": err,
                "live": bool(snap) and err is None and _client is not None,
                "synced_at": (snap or {}).get("synced_at"), "have_copy": bool(snap)}


def list_runs_ex(limit: int = 300) -> dict[str, Any]:
    """`{"runs": [...] | None, "live": bool, "error": str | None, "synced_at": iso | None}`.
    runs is the team's runs (live, or the last saved copy when Firestore cannot be read) and None
    only when there has never been any copy, in which case the caller uses this machine's own."""
    status()
    snap = _sync("runs")
    info = sync_info("runs")
    if not snap:
        return {"runs": None, **{k: info[k] for k in ("live", "error", "synced_at")}}
    docs = sorted(snap["docs"].values(), key=lambda r: str(r.get("timestamp_utc") or ""), reverse=True)
    return {"runs": docs[:limit], **{k: info[k] for k in ("live", "error", "synced_at")}}


def list_runs(limit: int = 300) -> list[dict[str, Any]] | None:
    """The team's runs, or None when there is no Firestore copy at all (older callers)."""
    return list_runs_ex(limit)["runs"]


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
        _touch("runs")
        return "written to Firestore runs/" + str(self_id)
    except Exception as e:  # noqa: BLE001
        return f"not written: {explain_error(e)} (local copy is still saved)"


def write_doc(collection: str, doc_id: str, doc: dict[str, Any]) -> str:
    """Generic mirror for the small shared collections (reviews, labels, rejudges, manifests).
    Never raises; a Firestore hiccup only means the record stays local-only."""
    if _client is None:
        return "not written: Firebase not connected (local copy is still saved)"
    try:
        _client.collection(collection).document(doc_id).set(doc)
        _touch(collection)
        return f"written to Firestore {collection}/{doc_id}"
    except Exception as e:  # noqa: BLE001
        return f"not written: {explain_error(e)} (local copy is still saved)"


def list_docs(collection: str, limit: int = 5000) -> list[dict[str, Any]] | None:
    """All docs of a small shared collection (live, or the last saved copy), or None when there is none."""
    status()
    snap = _sync(collection)
    if not snap:
        return None
    return list(snap["docs"].values())[:limit]


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
