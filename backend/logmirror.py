"""Sharing the raw per-problem log of a run with the other machines.

Runs and stories were already mirrored to Firestore; the raw log (results/gui_logs/<name>) stayed on the machine that ran it, so an
export or a failure analysis had logs for one's own runs only. A log is the evidence behind "why did this run end incomplete"
and "what did the agent do", so it is shared too:

  * redacted first: API keys, tokens and `NAME_KEY=value` lines are masked before anything leaves the machine;
  * compressed (gzip, then base64) into one Firestore document, which is limited to 1 MiB; a log that still does not fit keeps its
    beginning and its end and says in the middle that it was cut;
  * one write per run, read only when somebody opens that log (and cached), so the daily read budget is not touched by browsing.

RELIOPS_LOG_MIRROR=0 in the Lab's .env turns it off."""

from __future__ import annotations

import base64
import gzip
import re

MAX_B64 = 700_000          # characters of base64 per document; Firestore's limit is 1 MiB for the whole document

_PATTERNS = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "[redacted google key]"),
    (re.compile(r"\bsk-(?:or-|proj-|ant-)?[A-Za-z0-9_\-]{20,}"), "[redacted key]"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "[redacted groq key]"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"), "[redacted github token]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/\-]{20,}=*"), r"\1[redacted]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[redacted private key]"),
    # NAME=value or NAME: value where the name says it is a secret. "tokens" (LLM token counts: total_tokens, max_tokens) is not a
    # secret and a short pure number is a count, so neither is masked.
    (re.compile(r"(?im)\b([A-Z0-9_]*(?:API_?KEY|SECRET|PASSWORD|PASSWD|TOKEN(?!S))[A-Z0-9_]*)(\s*[=:]\s*)(?!\[redacted)(?!\d{1,6}\b)(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"',;]+)"), r"\1\2[redacted]"),
]


def redact(text: str) -> str:
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text


def pack(text: str, max_b64: int = MAX_B64) -> dict:
    """{"gz": base64 of gzip, "bytes": original size in bytes, "cut": bool} for the redacted `text`."""
    text = redact(text or "")
    original = len(text.encode("utf-8", "replace"))
    cut = False
    body = text
    while True:
        enc = base64.b64encode(gzip.compress(body.encode("utf-8", "replace"), 6)).decode("ascii")
        if len(enc) <= max_b64 or len(body) < 2000:
            break
        keep = int(len(body) * 0.8) // 2           # keep the beginning and the end, drop the middle, until it fits
        body = text[:keep] + "\n\n----- [log cut in the middle to fit the shared store] -----\n\n" + text[-keep:]
        text = body
        cut = True
    return {"gz": enc, "bytes": original, "cut": cut}


def unpack(doc: dict) -> str | None:
    try:
        return gzip.decompress(base64.b64decode(doc["gz"])).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a damaged or foreign document is "no shared log", not an error page
        return None
