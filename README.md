# ReliOps Lab

[![Status](https://img.shields.io/badge/status-v0%20%E2%80%94%20active%20development-orange)](#what-v0-does-not-do-yet-see-docsplan05-platform-specmd-v1v2-in-the-main-repo)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](requirements.txt)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Part of ReliOps](https://img.shields.io/badge/part%20of-ReliOps-15675A)](https://github.com/OmarElzero/ReliOps)

A small local GUI around [ReliOps](https://github.com/OmarElzero/ReliOps)'s SREGym setup: pick a
test environment, an agent, a model and a set of problems, add your own API key, click **Run**,
and watch the log stream live, right in the browser — no terminal juggling, no remembering which
flags `quickstart.sh` wants today. Results are saved locally and, if you connect Firebase, mirrored
to the same `runs` collection the full console (`docs/plan/05-platform-spec.md` in the main repo)
will use later — this is that spec's v0, running on one laptop with no cloud project required.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Run it](#run-it)
- [How it fits with the main repo](#how-it-fits-with-the-main-repo)
- [What v0 does *not* do yet](#what-v0-does-not-do-yet-see-docsplan05-platform-specmd-v1v2-in-the-main-repo)
- [Contributing](#contributing)
- [License](#license)

## Features

- **One form, one run.** Pick environment → agent → model → problem set, paste a key, hit Run.
- **Live log streaming** over SSE — no refreshing a page or tailing a file to see what the agent
  is doing right now.
- **Demo mode** — see the whole flow play back at a realistic pace with no cluster, no key, and no
  risk of touching real infrastructure. Good for a first look or a sponsor demo.
- **Problem sets are discovered automatically** from any `ReliOps/experiments/*.csv` with a
  `problem_id` column — add a new set in the main repo, it just shows up here.
- **Optional, local-only Firebase sync** — nothing leaves your machine until you give it a
  service-account path yourself.

## Requirements

- Python 3.11+, Docker running, `kubectl`/`helm`/`uv` (the GUI's Setup button installs `kind` for
  you; see `ReliOps/docs/MANUAL-SETUP.md` §0–2 for the rest).
- Your own model API key (one key per person — never share/rotate one key across the team, see
  `ReliOps/docs/plan/01-review-and-answers.md` §3.4).
- Optional: a Firebase service-account JSON (Firebase console → Project settings → Service
  accounts → Generate new private key) if you want results mirrored to Firestore.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd backend && uvicorn app:app --port 8765 --reload
```

Open http://localhost:8765. First time: click **Run setup** (clones ReliOps + SREGym, applies the
3 required patches, builds the kind cluster — ~10 min the first time). Then pick an agent, a
model, a problem set, save your key, and click **Run test(s)**.

No cluster yet and just want to see how it looks? Click **Demo mode** — it plays back a fake run
at a realistic pace, clearly labelled, and never touches a real cluster.

## How it fits with the main repo

This tool drives a clone of `OmarElzero/ReliOps` that it keeps at `~/reliops-lab-workdir/ReliOps`
(override with `RELIOPS_LAB_WORKDIR`). It does not duplicate SREGym or the agent code — it just
runs the same commands `ReliOps/scripts/quickstart.sh` and `ReliOps/docs/MANUAL-SETUP.md` describe,
from a form instead of a terminal, and streams the output back.

**Problem sets** are read live from `ReliOps/experiments/*.csv` — any CSV with a `problem_id`
column shows up as a selectable set automatically, so a new set the team adds there (e.g. a
stress set from `docs/plan/04-harder-envs.md`) appears with no change to this tool.

**Firebase** is entirely optional and local: nothing is sent anywhere until you paste a path to a
service-account file into the GUI. Without one, every run is still written to
`ReliOps/results/gui_runs.jsonl` on your machine.

### Shared results across the team

To see everyone's runs in one place, point everyone's "Firebase service-account JSON path" at the
**same** Firebase project — one person creates the project and generates the key once, then shares
that one JSON file with the team through a safe private channel (never git; it's already covered by
`.gitignore`). Each run also tags itself with whatever you type into **"Your name"** in the header
(saved in your browser only), so the team can tell whose run is whose in the `runs` collection.

**What that one person does, once:**
1. [console.firebase.google.com](https://console.firebase.google.com) → **Add project** (the free
   Spark plan is enough — this is a few hundred small document writes, not production traffic).
2. In that project: **Build → Firestore Database → Create database** — pick **Native mode** and any
   region. (This step is easy to miss: the project existing is not enough, Firestore itself has to
   be created inside it or every write from this tool will fail.)
3. **Project settings (⚙) → Service accounts → Generate new private key.** This downloads one JSON
   file — that's the file everyone shares.
4. Send that file to the team over a private channel — DM, not a public channel or git.

**What every other person does, once, after receiving the file:**
1. Save it at **`~/reliops-lab-workdir/firebase-service-account.json`** — that's the same folder
   this tool already keeps its ReliOps clone in (`~/reliops-lab-workdir/`, see below), it's outside
   any git repo, and it's the path the GUI's Firebase field is pre-filled with, so there's nothing
   to figure out or type by hand.
2. Open the GUI, click **Connect** next to that pre-filled path (change it only if you saved the
   file somewhere else), type your name at the top, done.

**Read this before relying on it for anything sensitive:** a Firebase service-account key is an
*admin* credential — it has full read/write/delete access to the whole project, for anyone who has
the file. "Your name" is a self-reported label for convenience, not an authentication system or an
access control: it does not restrict what anyone can do, and nothing stops a person from typing a
different name. This is the deliberate trade-off for v0 — fine for a small team that already trusts
each other with the same data, the same way a shared Google Sheet works. Real per-person identity
and permissions (`members/{uid}` with admin/member/viewer roles, enforced by Firebase
Authentication + Firestore Security Rules) are already designed in
`docs/plan/05-platform-spec.md` for the full v1/v2 platform — that's a bigger build than this local
GUI, not a setting to flip here.

## What v0 does **not** do yet (see `docs/plan/05-platform-spec.md` v1/v2 in the main repo)

- No multi-machine job queue — this is one browser talking to one local server on one laptop.
- No sealed-box key encryption — your key only ever touches your own `ReliOps/.env`, so this is
  fine for a single person running their own machine, not for handing a key to a sponsor VM.
- No campaign-level statistics (Wilson CIs, paired comparisons) — that's the results-explorer in
  v2; for now, read `ReliOps/results/gui_runs.jsonl` or SREGym's own CSVs directly.

## Project structure

```
reliops-lab/
├── backend/
│   ├── app.py            # FastAPI routes + SSE streaming
│   ├── runner.py         # drives the ReliOps clone: setup, demo, campaign runs
│   └── firebase_sync.py  # optional, local-only Firestore mirroring
├── frontend/
│   ├── index.html
│   ├── app.js            # form state, SSE client, log rendering
│   └── style.css
├── requirements.txt
├── LICENSE
└── CONTRIBUTING.md
```

## Contributing

Small team, small codebase, no build step — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup,
code style, and what a good PR looks like.

## License

[MIT](LICENSE) — see the file for the full text.
