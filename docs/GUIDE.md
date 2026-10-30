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
- **Experiment name** (the box next to Run queue): what these runs belong to, in your own words ("Dev analysis, first try"). The queue is a reusable recipe; the experiment is one use of it, so you do not rename the queue each time. Every run is recorded under the name, and Results can filter by it. The same name continues where you left off; a new name starts fresh (the old runs stay but do not count for it). Run queue stays disabled until a name is typed.
- **Restart** (asks "are you sure?" first): runs the whole queue again from step 1 as a new round of the same experiment, recorded as "name (restart 2)", "name (restart 3)" and so on. Nothing is deleted; older rounds stay in Results under their own label. It is only enabled when the name already has runs.
- **Resume**: only cells with no result yet (under the current experiment name) are run, so a stopped, crashed or interrupted queue continues where it left off. A run with no known outcome is "incomplete", is run again, and is never counted as a failure.
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

The Lab's `.env` reader follows the usual rules: a quoted value ends at its closing quote, so `GEMINI_API_KEY="AIza..." # note` is read as the key alone; comment and blank lines between entries are ignored; `export KEY=value` works. Each key row in Key health shows where its value comes from (Lab .env, the ReliOps clone's .env, or your shell) and only its first 4 characters and its length. Google issues more than one key format (`AIza...` of 39 characters, and longer `AQ....` keys), so the Lab does not judge a key by its length or prefix; it only warns when the value contains a space, a quote or a `#`, which means the `.env` line carries extra text. A dead mark is dropped by itself when the key's value changes.

Rotating several keys is **off** until you turn it on in **Setup & quick run → Key health and rotation**: tick two or more Gemini keys (`GEMINI_API_KEY`, `GEMINI_API_KEY_2`, ...), give each an owner name if you like, and press **Save rotation** (it writes `RELIOPS_KEY_POOL` and `RELIOPS_KEY_OWNERS` in the Lab's `.env`; you can also write them by hand). Each run then uses the next healthy key, dead and resting keys are skipped, the run records which slot and owner it used, and if every key is resting the queue waits and carries on by itself. If every key is dead it stops and tells you.

**Dead key alert.** When a run shows that a key was rejected, a red bar appears at the top of every tab (and the browser tab title gets a warning) naming the key and its owner, with **Remove from rotation** and **It works: clear the mark**. A key that is out of quota shows as a quieter "resting until" line. Replacing the key's value in `.env` clears the mark by itself.

### Guards against a broken setup

- **Stops after repeated empty runs.** If 3 runs in a row end incomplete (no result row, or SREGym marks the run incomplete), the queue stops and says why, instead of spending the night on empty runs. Finished runs are kept and only missing cells run on the next start. Change the limit with `RELIOPS_MAX_INCOMPLETE=5` in `.env`; `0` turns the stop off. Incomplete runs are never counted as failures in Results.
- **Checks before the first run.** The log starts with a `[check]` line if `AGENT_API_BASE` is set (SREGym then lets the agent reach only that host, so a Gemini model sent to a gateway usually fails with 401 "Invalid credential"), or if the agent image looks older than `agent/react_v1/driver.py` (run `scripts/sync_agent.sh` and tick Force-build once).
- **A step that stops takes the whole queue with it.** When a step stops itself (3 incomplete in a row, a broken cluster, every key dead), the later steps are not started, because they would hit the same fault. The log says how many were left.
- **Outside faults are tried again inside the same pass.** If a run dies because a key was rejected or out of quota (and the pool has another key), or because the model sent empty replies, the same cell runs again at once, up to 2 times (`RELIOPS_RETRY_EXTERNAL=0` turns it off). These retries are not counted toward the stop above. Cluster faults and unknown causes are never retried. Every attempt stays in the record; the latest one counts for the cell.
- **Mark a run as an external fault.** In a run's review form pick the tag `external-fault` (and write why in the note). The run stays in the record but is not counted as a pass or a fail, and its cell is run again the next time the queue starts with the same experiment name.
- **Cluster not ready stops the queue at once.** If a run ends before the agent started because nodes or `kube-system` pods never became Ready, or cleanup did not finish, the queue stops at that run instead of waiting for the third. A broken cluster does not heal between runs, and each attempt can cost about half an hour. (`RELIOPS_MAX_INCOMPLETE=0` turns this off too.) Before every run the log also shows a `[check]` line naming any node that is not Ready; the run still goes ahead, because SREGym revives a stopped kubelet at the start of each problem.
- **Why a run is incomplete.** The Results table has a **Why incomplete** column. New runs store the reason; older runs get it read from their own log when that log is on this machine. It reads SREGym's own wording (nodes or pods not Ready, cleanup not finished, key rejected or out of quota, judge check failed); anything else says whether the agent ever started.
- **Rejected credentials.** A 401 / AuthenticationError marks the key dead when the request went straight to the provider. When `AGENT_API_BASE` is set it does not, because the gateway rejected the key, not Google.

### Cluster health

Setup & quick run has a **Cluster health** card. **Check now** reads the local kind cluster (nodes, `kube-system` pods that are not Ready or stuck in Terminating, the node containers, free memory and disk) and says in plain words what is wrong. It also refreshes whenever you open the tab. Checking only reads.

Three repair buttons sit under it. Each one shows the exact commands and asks before it does anything, runs as the current job (its output streams in the Queue tab), and is refused while a run or queue is going:

- **Restart the kind nodes**: `docker restart` on the node containers, then waits up to 4 minutes for every node to report Ready. Nothing is deleted. Try this first.
- **Clear stuck system pods**: force-deletes pods that have been in Terminating for over a minute in `kube-system` only; their controllers create new ones.
- **Rebuild the cluster**: `kind delete cluster`, then the same setup the Run setup button runs (about 10 minutes). Everything in the cluster is lost. Use it when restarting does not help. Not tested against the cached baseline state SREGym keeps outside the cluster.

There is no free-text command box on purpose: the page sends only the name of a repair, and the server builds the commands from names it reads itself. A box that ran typed commands would let any web page that reaches `127.0.0.1` run commands on this machine.

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
