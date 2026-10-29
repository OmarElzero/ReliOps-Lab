# ReliOps Lab

[![Status](https://img.shields.io/badge/status-v0%20%E2%80%94%20active%20development-orange)](#what-v0-does-not-do-yet-see-docsplan05-platform-specmd-v1v2-in-the-main-repo)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](requirements.txt)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Part of ReliOps](https://img.shields.io/badge/part%20of-ReliOps-15675A)](https://github.com/OmarElzero/ReliOps)

A small local GUI around [ReliOps](https://github.com/OmarElzero/ReliOps)'s SREGym setup: pick a
test environment, an agent, a model and a set of problems, add your own API key, click **Run**,
and watch the log stream live, right in the browser — no terminal juggling, no remembering which
flags `quickstart.sh` wants today. Results are saved locally and, if this project's own `.env` has
Firebase credentials in it, mirrored to the same `runs` collection the full console
(`docs/plan/05-platform-spec.md` in the main repo) will use later — this is that spec's v0, running
on one laptop with no cloud project required.

**This tool isn't a copy of ReliOps — it drives one.** ReliOps Lab is just this GUI (`backend/` +
`frontend/`); it has none of the actual benchmark, agent or SREGym code in it. The first time you
click **Run setup**, it clones `OmarElzero/ReliOps` for you into `~/reliops-lab-workdir/ReliOps`
(a separate folder, not this one) and runs everything from there. So this tool can't do anything
useful standing completely alone — it needs that clone — but you never have to set the clone up
yourself; Setup does it. The one thing to know because of this: **this project's own `.env`**
(next to `backend/` and `frontend/` — the one you have open) **is where your keys go**, not any
`.env` inside that separate clone; see "Keys" below for why.

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
- **Results with statistics (v1).** The Results tab shows a pass rate per configuration (agent +
  model + action space + config label + judge + run class) with a Wilson 95% interval and a
  problem-level bootstrap interval, and a paired comparison of any two configurations (exact McNemar
  on matched problem/repeat pairs). Runs that crashed or never finished are shown as *incomplete*,
  never counted as failures. Exploration and frozen runs are never mixed.
- **Stores the text, not just the scores (v1).** Each run keeps the diagnosis the agent submitted,
  the judge's reasoning, checklist and D1-D3 scores, the judge model, a free-text config label, and
  the path to the agent transcript, so a run can be re-judged later by a stronger judge.
- **Story view (v1).** For `reliops_v1` runs, each result has a step-by-step story: what the agent
  thought, did and saw, interleaved with a cluster timeline recorded during the run (needs the main
  repo's `scripts/replay_trace.py` and `scripts/cluster_recorder.py`; `git pull` the ReliOps clone).
  Stories live on the machine that ran the test.
- **Optional, local-only Firebase sync** — nothing leaves your machine unless this project's own
  `.env` has credentials in it; the status pill at the top reflects `.env` automatically, no
  "Connect" step.

## Requirements

- Python 3.11+, Docker running, `kubectl`/`helm`/`uv` (the GUI's Setup button installs `kind` for
  you; see `ReliOps/docs/MANUAL-SETUP.md` §0–2 for the rest).
- **GitHub access to `OmarElzero/ReliOps`.** That repo is currently **private**, so **Run setup**'s
  clone needs two things, each a one-time setup per machine, not per run:
  1. Be added as a **Collaborator** on it (ask Omar: `github.com/OmarElzero/ReliOps/settings/access`).
  2. Git credentials that don't need an interactive prompt (this GUI can't show you one). Easiest:
     install the [GitHub CLI](https://cli.github.com) and run `gh auth login` once. If you're on
     WSL, run it **inside WSL** — Windows' own git credentials don't carry over, since WSL has its
     own separate git install. (Alternative without installing anything new: a Personal Access
     Token from `github.com/settings/tokens` + `git config --global credential.helper store`, then
     one manual `git clone` by hand to get prompted for it once.)
  If Setup's clone step fails with an auth-looking error, the log now explains this same thing —
  this bullet exists so you see it *before* hitting that error.
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
3 required patches, builds the kind cluster — ~10 min the first time; make sure the GitHub access
in "Requirements" above is sorted before this step, or the clone will fail). Then pick an agent, a
model, a problem set, save your key, and click **Run test(s)**.

No cluster yet and just want to see how it looks? Click **Demo mode** — it plays back a fake run
at a realistic pace, clearly labelled, and never touches a real cluster.

## How it fits with the main repo

This tool drives a clone of `OmarElzero/ReliOps` that it keeps at `~/reliops-lab-workdir/ReliOps`
(override with `RELIOPS_LAB_WORKDIR`) — a separate folder from this project, created automatically
the first time you click **Run setup**. It does not duplicate SREGym or the agent code — it just
runs the same commands `ReliOps/scripts/quickstart.sh` and `ReliOps/docs/MANUAL-SETUP.md` describe,
from a form instead of a terminal, and streams the output back.

**Two folders, one `.env`.** Because of the above, there are technically two places a `.env` could
live: this project's own root, and the separate cloned-repo folder. Only **this project's own
`.env`** matters — it's what the "Keys & Firebase" form reads and writes, and what the status pill
reflects. You never need to touch the clone's `.env` directly: `scripts/quickstart.sh` reads its
own copy of that file off disk as part of how the main repo works stand-alone (see
`ReliOps/docs/MANUAL-SETUP.md`), so this tool mirrors your saved model keys into it automatically
whenever you save a key or run Setup — one save here is enough.

**Problem sets** are read live from `ReliOps/experiments/*.csv` — any CSV with a `problem_id`
column shows up as a selectable set automatically, so a new set the team adds there (e.g. a
stress set from `docs/plan/04-harder-envs.md`) appears with no change to this tool.

**Firebase** is entirely optional and local: nothing is sent anywhere until this project's own
`.env` has real credentials in it (there's no GUI field for this on purpose — see "Shared results"
below). Without one, every run is still written to `ReliOps/results/gui_runs.jsonl` on your
machine, and shows up in the GUI's own **Results** tab either way (team-wide via Firestore when
connected, this machine only when not).

### Shared results across the team

To see everyone's runs in one place, point everyone's **own project `.env`** (next to `backend/`
— not the cloned ReliOps folder, see "How it fits with the main repo" above) at the **same**
Firebase project — one person creates the project and generates the key once, then shares that one
JSON file with the team through a safe private channel (never git; it's already covered by
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
1. Save it anywhere, then add one line to **this project's own `.env`** (create the file next to
   `backend/` and `frontend/` if it doesn't exist yet — same file the model-key Save button
   writes to): `GOOGLE_APPLICATION_CREDENTIALS=/path/to/that/file.json` — or skip the file
   entirely and paste the JSON itself into `.env` as `SERVICE_ACCOUNT_KEY={...}`. Either way,
   saving it as `firebase-service-account.json` right next to this `.env` needs no `.env` line at
   all — that's the one path this tool checks automatically if nothing else is set.
2. Open the GUI and type your name at the top. The status pill picks up `.env` on its own within
   a few seconds — nothing else to click.

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
- No sealed-box key encryption — your key only ever touches your own local `.env`, so this is
  fine for a single person running their own machine, not for handing a key to a sponsor VM.
- Statistics are the basics only (Wilson, problem bootstrap, exact McNemar). No multiple-comparison
  correction, no per-fault-class breakdown, no export of figures yet.
- **One test environment only (SREGym), hardcoded.** `runner.stream_campaign()` shells out straight
  to SREGym's own CLI (`uv run main.py --agent … --problem …`) — there's no abstraction between
  "this tool" and "SREGym specifically" yet. "Any agent" currently only means any agent SREGym's own
  `agents.yaml` already knows how to launch (reliops_v1/v2, STRATUS, mini-sre, Gemini CLI, OpenCode,
  autosubmit) — not a truly pluggable agent either. Adding a second environment (AIOpsLab is next,
  per `docs/envs/which-to-use.md`) means pulling the SREGym-specific bits out behind the
  `EnvAdapter`/`AgentAdapter` contract **already specced** in `docs/plan/05-platform-spec.md` §3.3
  (`setup` / `list_problems` / `run` / `parse`) — reuse that contract instead of inventing a second
  one, so this local tool and the eventual Firebase platform (`05-platform-spec.md` v0–v3) share one
  adapter model instead of diverging. Scoped as a future stage, not started — see that doc for the
  interface to implement against.

## Project structure

```
reliops-lab/
├── backend/
│   ├── app.py            # FastAPI routes + SSE streaming
│   ├── runner.py         # drives the ReliOps clone: setup, demo, campaign runs, result storage
│   ├── stats.py          # Wilson, problem bootstrap, exact McNemar (standard library only)
│   ├── test_stats.py     # python3 -I backend/test_stats.py
│   ├── test_runner_storage.py  # python3 -I backend/test_runner_storage.py
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
