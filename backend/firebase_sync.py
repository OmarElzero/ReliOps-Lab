"""Optional Firestore sync, matching the `runs/{id}` collection in
docs/plan/05-platform-spec.md §4 of the ReliOps repo (jobId/campaignId are left null here since
this GUI runs ad-hoc tests, not full campaigns yet — same collection, so results from this tool
and from the future job-queue runner end up side by side without a migration).

Connecting is opt-in and local: drop a Firebase service-account JSON (Firebase console → Project
Settings → Service accounts → Generate new private key) at the path this module is given, or set
GOOGLE_APPLICATION_CREDENTIALS. Nothing is sent anywhere unless that file is present — with no
file, every run is still saved locally (see runner.py), just not mirrored to Firestore. The key
file itself is never read into a log, never printed, and must stay out of git (see .gitignore).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_client = None
_status = "not configured"


def _try_connect(service_account_path: str | None) -> None:
    global _client, _status
    path = service_account_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path or not Path(path).is_file():
        _status = "not configured — no service-account file given"
        _client = None
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            cred = credentials.Certificate(path)
            firebase_admin.initialize_app(cred)
        _client = firestore.client()
        _status = "connected"
    except Exception as e:  # noqa: BLE001 — surfaced to the GUI's status line, not swallowed
        _client = None
        _status = f"connection failed: {e}"


def configure(service_account_path: str) -> str:
    _try_connect(service_account_path)
    return _status


def status() -> str:
    if _client is None:
        _try_connect(None)
    return _status


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
