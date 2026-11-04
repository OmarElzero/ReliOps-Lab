#!/usr/bin/env python3
"""Why is Firebase "not configured" on this machine? Prints what the Lab can see, never a secret.

Standalone (standard library only), so it runs even when the Lab itself is out of date:

    python3 env_doctor.py                 # looks in the usual places
    python3 env_doctor.py /path/to/Lab    # or name the Lab folder (the one that has backend/ and frontend/)

It shows: which Lab folder is actually RUNNING, every .env-like file near it (a Windows editor often saves `.env.txt`),
the variable NAMES in the file the Lab reads with only the length of each value, and whether a service account can be
found in it and, if not, which field is missing. Key text is never printed.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

NAME_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
FIREBASE_VARS = ("GOOGLE_APPLICATION_CREDENTIALS", "FIREBASE_SERVICE_ACCOUNT_JSON", "SERVICE_ACCOUNT_KEY")
REQUIRED = ("project_id", "private_key", "client_email")


def running_lab_folders() -> list[Path]:
    """Working folders of any running uvicorn / backend.app process (Linux /proc)."""
    out = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmd = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            if "uvicorn" in cmd and "app" in cmd:
                out.append(Path(os.readlink(p / "cwd")))
        except OSError:
            continue
    return out


def lab_root(d: Path) -> Path | None:
    for c in (d, d.parent):
        if (c / "backend" / "runner.py").is_file():
            return c
    return None


def blocks(text: str) -> dict[str, str]:
    """name -> everything up to the next NAME= line (a pasted JSON spans lines)."""
    found: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = NAME_LINE.match(line) if not line.startswith((" ", "\t")) else None
        if m:
            current = m.group(1)
            found[current] = [m.group(2)]
        elif current is not None:
            found[current].append(line)
    return {k: "\n".join(v) for k, v in found.items()}


def check_service_account(text: str) -> str:
    """Says whether this text holds a usable service account, naming missing fields only."""
    core = text[text.find("{"): text.rfind("}") + 1] if "{" in text and "}" in text else ""
    for cand in (core, core.replace('\\"', '"')):
        try:
            value = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            missing = [f for f in REQUIRED if not value.get(f)]
            return "valid JSON" + (f", but missing: {', '.join(missing)}" if missing else ", all required fields present")
    has = {
        "project_id": bool(re.search(r"project_id\W+\S", text)),
        "private_key": "PRIVATE KEY-----" in text and "BEGIN" in text and "END" in text,
        "client_email": bool(re.search(r"client_email\W+\S+@", text)),
    }
    missing = [k for k, v in has.items() if not v]
    return "not valid JSON; " + ("fields look present (the new Lab can read this shape)" if not missing else "missing: " + ", ".join(missing))


def show_env_files(root: Path) -> Path:
    env = root / ".env"
    print(f"\nLab folder: {root}")
    print(f"The Lab reads exactly this file: {env}  ({'exists, ' + str(env.stat().st_size) + ' bytes' if env.is_file() else 'DOES NOT EXIST'})")
    for d in (root, root / "backend"):
        for f in sorted(set(d.glob(".env*")) | set(d.glob("*.env"))):
            if f != env and f.is_file():
                print(f"  other file here: {f}  ({f.stat().st_size} bytes)  <- NOT read by the Lab")
    for f in sorted(root.glob("*service*account*.json")) + sorted(root.glob("firebase*.json")):
        print(f"  key file here: {f}")
    return env


def main() -> int:
    args = [Path(a).expanduser() for a in sys.argv[1:]]
    home = Path.home()
    running = running_lab_folders()
    print("Running Lab process(es), by working folder:", ", ".join(str(r) for r in running) or "none found (is the Lab running?)")
    roots: list[Path] = []
    for d in args + running + [Path.cwd(), home / "Gp" / "ReliOps-Lab", home / "reliops-lab", home / "ReliOps-Lab", home / "Gp" / "reliops-lab"]:
        r = lab_root(d) if d.is_dir() else None
        if r and r not in roots:
            roots.append(r)
    if not roots:
        print("No Lab folder found. Run this from inside the Lab folder, or pass its path.")
        return 2
    if len(roots) > 1:
        print(f"\nNOTE: {len(roots)} Lab folders exist. Only the one that is RUNNING matters; edit ITS .env.")
    for root in roots:
        env = show_env_files(root)
        if not env.is_file():
            continue
        text = env.read_text(encoding="utf-8", errors="replace")
        names = blocks(text)
        print("  variable names found (value length in characters):")
        for k, v in names.items():
            n = len(v.strip())
            note = "   <- far too long for a key: something (often a pasted JSON) got glued onto this line" if n > 120 and k not in FIREBASE_VARS else ""
            print(f"    {k}  ({n}){note}")
        if not names:
            print("    none: the file has no NAME=value line")
        print("  Firebase:")
        gac = names.get("GOOGLE_APPLICATION_CREDENTIALS")
        if gac:
            path = gac.strip().strip("\"'")
            print(f"    GOOGLE_APPLICATION_CREDENTIALS = {path}  -> {'file exists' if Path(path).is_file() else 'NOT a file on this machine (Windows paths are converted by the Lab, but the file must exist)'}")
        for var in FIREBASE_VARS[1:]:
            if var in names:
                print(f"    {var}: {check_service_account(names[var])}")
        if not any(v in names for v in FIREBASE_VARS):
            if "PRIVATE KEY" in text or "client_email" in text:
                print(f"    a service account is in the file but under no variable name: {check_service_account(text)}")
                print("    (the Lab version with the 4 Nov fix reads this; an older one does not: pull the Lab)")
            else:
                print("    nothing about Firebase in this file: no GOOGLE_APPLICATION_CREDENTIALS, SERVICE_ACCOUNT_KEY or FIREBASE_SERVICE_ACCOUNT_JSON")
        for f in (root / "firebase-service-account.json",):
            print(f"    default key file {f.name}: {'present' if f.is_file() else 'absent'}")
        try:
            import firebase_admin  # noqa: F401
            print("    firebase_admin: installed")
        except ImportError:
            print("    firebase_admin: NOT installed in this Python (run the Lab's start script so it installs requirements)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
