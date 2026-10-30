# ReliOps Lab: a tab-by-tab guide

Open the Lab (`./start.sh`), type your name in the header (it labels every run you start), and use the tabs from left to right. Dark mode is the half-moon button in the header. The pill next to it shows whether Firebase is connected; a coloured pill with a pulsing dot means a run is in progress, and clicking it takes you to the live output.

## Where everything is saved

| What | Where | Shared with the team? |
|---|---|---|
| Every run, in full: diagnosis text, judge verdict and checklist, SREGym's raw row, token counts, config, commit, which key slot, log file name | `results/gui_runs.jsonl` in the ReliOps clone (`~/reliops-lab-workdir/ReliOps/` by default) | Yes, mirrored to Firestore when connected |
| The same runs in the team's standard shape | `results/gui_runs.csv` | Only if someone commits it |
| Each run's raw output | `results/gui_logs/*.log` (git-ignored) | No, local |
| Step-by-step transcript (what the agent thought, did and saw) | the SREGym clone, `external/envs/sregym/results/...` (git-ignored) | The rendered story is copied to Firestore when a run finishes |
| Failure tags, hand labels, re-judge verdicts, frozen campaigns, queues, saved problem sets, run purpose marks, key health | `results/gui_reviews.jsonl`, `gui_labels.jsonl`, `gui_rejudge.jsonl`, `gui_manifests.jsonl`, `gui_queues.jsonl`, `gui_problem_sets.jsonl`, `gui_marks.jsonl`, `gui_key_status.jsonl` | Yes, mirrored to Firestore when connected |
| API keys | `.env` next to `backend/` (git-ignored) | Never. Only key names and owners are ever recorded, never the secret |

Because the diagnosis text and the judge's reasoning are stored with every run, scores can be re-judged later without running the agent again. Mitigation is graded by SREGym's own checker, not by the judge. Records are append-only: a correction is another line, and the latest one wins.

If Firebase is not connected, you see only this machine's runs. A laptop that is lost loses its transcripts unless the story was shared.

## Queue

Decide what to run, in order, then start it and walk away.

- **Add step**: one step is one configuration (agent, action space, model, judge, label) on a set of problems with a repeat count. Examples: ReAct v1 typed on the frozen 30, then typed + guarded shell, then one of them on the dev 12.
- **Problems**: choose a set (the pinned frozen 30, the pinned dev 12, a set you saved, or custom ids). The category chips select or clear a whole fault category, so you can try one category on its own. "Choose individual problems" opens the full list with a text filter. "Save selection as a set" keeps your own set.
- **Run purpose**: Automatic, Experiment (just look at the result) or Dev (analyse it, may tune on it). Automatic means a dev-set problem is dev and anything else is an experiment. A frozen campaign is the reported measurement.
- **Frozen campaign**: the step takes its settings from a manifest you froze in the Campaigns tab and cannot be edited.
- **Resume**: only cells with no result yet are run, so a stopped, crashed or interrupted queue continues where it left off. A run with no known outcome is "incomplete", is run again, and is never counted as a failure.
- **Start queue** runs on the server, not in the browser tab. Close the tab, lock the laptop screen, reopen later and the page re-attaches to the live output. One job runs at a time because SREGym drives a single cluster. **Stop** finishes the current run and then stops.
- **Save queue** keeps the ordered list under a name so a teammate can load it.

## Setup & quick run

The one-off page for setting up a machine and for a single quick run.

1. Machine check: Docker, kubectl, kind, uv and so on, with what to fix.
2. Environment: clones ReliOps and SREGym and builds the cluster tooling (Run setup). Run it once per machine.
3. Agent and model: which agent, which action space, the agent model and the judge model.
4. Problems and repeats.
5. Keys & Firebase: paste your own model key (saved to `.env`). **Key health** lists each key with a status: ok, resting (quota used up, back later) or dead. The Lab marks a key itself when a run shows the provider's error, and you can mark or clear one by hand.
6. Run, Run setup, Demo (a fake run to see the output, no cluster needed) and Stop, with the live log below.

Rotating several keys is **off** unless you add `RELIOPS_KEY_POOL=GEMINI_API_KEY,GEMINI_API_KEY_2` (and optionally `RELIOPS_KEY_OWNERS=omar,sara`) to `.env`. Each run then uses the next healthy key, dead and resting keys are skipped, the run records which slot and owner it used, and if every key is resting the queue waits and carries on by itself. If every key is dead it stops and tells you.

## Results

A dashboard computed from exactly the runs the filters leave in view.

- **Purpose chips**: All, Experiments, Dev set, Final. Look at one purpose at a time; the dashboard warns when a view mixes them.
- **Filters**: search, category, problem, agent, action space, model, config label, judge, run class, outcome, failure tag, app, person, frozen campaign, dates. Active filters show as removable chips. Your last filters are remembered in this browser.
- **People**: what each person ran. Click a name to filter to them.
- **What these numbers are based on**: run count and dates, purposes, models, agent code commits, who ran them, each judge's agreement with hand labels (Cohen's kappa), and every frozen campaign in view with its commit, judge, problem-set hash and how many cells are done.
- **Summary per configuration**: pass rate with a 95% Wilson interval and a problem-level bootstrap interval, incomplete runs, cost, failure tags.
- **By fault category** and **Problem × configuration**: where an agent is weak. Click a category or problem to filter to it.
- **Failure tags** and **Runs per day**.
- **Runs**: every run with its purpose, outcome, tag, and links to the stored diagnosis and judge text, the story and the raw log. Change one run's purpose with the dropdown, or tick several and use "Mark as". A mark always wins over the automatic purpose; "automatic" clears it.
- **Report (.md)** downloads the current view as Markdown with its basis, ready to adapt for a results section. **All runs (CSV)** exports everything.
- **More tools**: paired comparison of two configurations (exact McNemar test), the standing audit against the dumb-baseline floor, and a coverage table of what has been tried least.

The caveats box at the top lists what to check before quoting a number: mixed purposes, runs without an outcome, a judge that has not been checked against hand labels, an unfinished frozen campaign.

## Campaigns

A **frozen campaign** fixes what is measured: agent, model, action space, judge, label, problem set (by hash) and repeats, recorded with the agent code commit. Runs that do not match the manifest are refused. The grid shows every (problem, repeat) cell as pass, fail, incomplete or missing. A machine can take a slice (machine k of n) so several laptops split one campaign without overlap.

## Judge audit

Check the judge against people. Hand-label diagnoses blind (the judge's verdict is hidden), then see per-judge agreement and Cohen's kappa. You can re-judge stored diagnoses with another model and compare judges. About 30 labels are needed before a kappa means much.

## Problems

One card per problem (what the fault is, expected difficulty, notes) and the attempt log for it. The Results tab links here from a run.

## Pinned sets

`experiments/pins.json` in the ReliOps repo fixes the problem sets for the current phase by a hash of their ids: the frozen 30 and the dev 12. The Lab compares each CSV with its pin and warns "CHANGED since pinned" in the problem-set list if a file was edited. To change a pinned set, add a new entry with a written reason; never edit one in place.

## A safe first session

1. Run **Demo** to see the output, then a machine check.
2. Run a single problem with the dumb baseline, then the agent on one dev problem, and confirm the run appears in Results with its diagnosis text.
3. Only then queue a long run, and start with the dev 12 so any pipeline problem shows up on problems you are allowed to read.
