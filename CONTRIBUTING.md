# Contributing to ReliOps Lab

ReliOps Lab is the local GUI runner for [ReliOps](https://github.com/OmarElzero/ReliOps), a
graduation research project at Cairo University's Faculty of Computing & AI. It's a small team
(five people) and a small codebase (two FastAPI files, a runner module, a vanilla-JS front end) —
this file keeps that simple on purpose.

## Before you start

- Read the main repo's `docs/plan/05-platform-spec.md` first — it's the full spec this tool is the
  v0 of (one laptop, no job queue, no sealed-box keys yet). Know what's intentionally *not* here
  before "fixing" it.
- Open an issue (or a Jira `RO-<n>` ticket once the team's board exists) before a large change —
  for a small fix or a clear bug, a PR on its own is fine.

## Setup

```bash
git clone https://github.com/OmarElzero/reliops-lab
cd reliops-lab
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd backend && uvicorn app:app --port 8765 --reload
```

Open http://localhost:8765. No Kubernetes cluster yet? Click **Demo mode** — it plays back a fake
run so you can work on the front end without the full SREGym setup.

## Code style

- **Backend (`backend/*.py`):** plain FastAPI + Pydantic, Python 3.11+, type hints on public
  functions, `from __future__ import annotations` at the top of new modules. No framework beyond
  what's already imported (FastAPI, Pydantic, `firebase-admin`) without discussing it first — this
  stays a one-laptop tool, not a platform.
- **Frontend (`frontend/*`):** vanilla HTML/CSS/JS, no build step, no framework. If a change needs
  a bundler or a JS framework, that's a v1/v2 conversation (`docs/plan/05-platform-spec.md`), not a
  PR here.
- **No secrets, ever.** Model API keys and Firebase service-account JSON never get committed — they
  stay in the user's own `ReliOps/.env` or wherever the GUI's key form writes them. If you're
  touching `backend/runner.py` or `backend/firebase_sync.py`, double-check nothing you add logs or
  persists a raw key.

## Commit messages and branches

- Branch names: `short-description` or, once the team's Jira board exists, `RO-<n>-short-description`.
- Commits: a one-line imperative summary (`fix: handle empty problem set in /api/run`), a body only
  if the "why" isn't obvious from the diff.
- Keep PRs small and focused — one behavior change per PR, so it's reviewable in one pass.

## What a PR needs

- **What changed and why**, in a sentence or two.
- **How you tested it** — "ran `uvicorn app:app --reload`, clicked through Demo mode" is a
  perfectly good answer for a front-end change; a backend change touching `runner.py` should
  include at least one real run's output (or demo mode, if no cluster is available).
- No new dependency without a one-line reason in the PR description — `requirements.txt` stays
  small by design.

## Reporting a bug

Open an issue with: what you did, what you expected, what happened instead, and (if it's a run
that failed) the relevant lines from the SSE log stream or `ReliOps/results/gui_runs.jsonl`. A
screenshot helps for anything in the GUI itself.
