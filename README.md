# ReliOps Lab (v0, local)

A small GUI around [ReliOps](https://github.com/OmarElzero/ReliOps)'s SREGym setup: pick a test
environment, an agent, a model and a set of problems, add your own API key, click Run, and watch
the log stream live. Results are saved locally and, if you connect Firebase, mirrored to the same
`runs` collection the full console (`docs/plan/05-platform-spec.md` in the main repo) will use
later — this is that spec's v0, running on one laptop with no cloud project required.

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

## What v0 does **not** do yet (see `docs/plan/05-platform-spec.md` v1/v2 in the main repo)

- No multi-machine job queue — this is one browser talking to one local server on one laptop.
- No sealed-box key encryption — your key only ever touches your own `ReliOps/.env`, so this is
  fine for a single person running their own machine, not for handing a key to a sponsor VM.
- No campaign-level statistics (Wilson CIs, paired comparisons) — that's the results-explorer in
  v2; for now, read `ReliOps/results/gui_runs.jsonl` or SREGym's own CSVs directly.
