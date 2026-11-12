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

### Pause and Continue

**Pause after current run** (the button in the run bar and the one at the top of the page) lets the run in progress finish and be recorded, then pauses the queue. Nothing is lost: every finished run is kept, and the Lab remembers which queue, which experiment name and who started it (`results/gui_queue_state.json`, on the machine that ran it). The run bar then shows what was paused and how many runs are left, with a **Continue** button. Continue starts the same queue under the same experiment name, never as a restart, so it runs only the cells that have no result yet, beginning with the unfinished ones in the step where it paused. The same button appears after a queue stopped itself (broken key, cluster not ready, several incomplete runs in a row: fix the cause first) and after a queue was cut off because the Lab or the machine went down. It is not offered while a job is running or after a queue finished. If you edit the queue while it is paused (add a step, change repeats), Continue uses the edited, saved version. Nothing is shared with teammates: only the machine that ran the queue can continue it.

### Choosing many agents at once

In **Add step** (and in **Copy for other agents…** on any step) the box **Add this step for many agents** lists ready-made variants: the controls (V0, a, B), one switch on the base (c1 to c8), the typed menu (t1, t2) and three pairs (p1 to p3). Tick as many as you like. Each becomes its own step with the problems, repeats, models, purpose and options from the same form; the variant's name is the config label, so Results keeps them apart. The agent and switches above the box are ignored while any variant is ticked. If "Force-build" is ticked, only the first new step rebuilds the image. c8 waits for the reviewed Kubernetes notes and is not ticked by "Tick all that can run". **Duplicate** still copies one step to edit by hand.

**Firestore reads and the free quota.** The free plan allows 50,000 reads a day for the whole team, and every query costs at least one read plus one per document it returns. The Lab keeps its use low: a one-document change stamp (`meta/changes`) tells it which collections changed, so an unchanged collection costs no query; queries are incremental; the full re-read happens once a day at most; a restart reuses the saved copy (`results/gui_remote_cache.json`); a failed read is not retried on every page load. Each machine also counts its reads per Pacific day (`results/gui_firestore_usage.json`, shown in the Results banner) and stops refreshing, showing the saved copy, once it reaches `RELIOPS_FIRESTORE_READ_BUDGET` (default 8000, about a fifth of the free quota, for a team of five; `0` removes the limit). If the team grows or one machine needs more, change it in `.env` and keep the total under 50,000. Every teammate needs the current Lab for the stamps; a teammate on an older Lab is still picked up, up to 15 minutes later, by the safety poll.

If Firebase is not connected, you see only this machine's runs. A laptop that is lost loses its transcripts unless the story was shared.

## Queue

Decide what to run, in order, then start it and walk away.

- **Add step**: one step is one configuration (agent, action space, model, judge, label) on a set of problems with a repeat count. Examples: ReAct v1 typed on the frozen 30, then typed + guarded shell, then one of them on the dev 12.
- **Problems**: choose a set (the pinned frozen 30, the pinned dev 12, a set you saved, or custom ids). The category chips select or clear a whole fault category, so you can try one category on its own. "Choose individual problems" opens the full list with a text filter. "Save selection as a set" keeps your own set.
- **Run purpose**: Automatic, Experiment (just look at the result) or Dev (analyse it, may tune on it). Automatic means a dev-set problem is dev and anything else is an experiment. A frozen campaign is the reported measurement.
- **Frozen campaign**: the step takes its settings from a manifest you froze in the Campaigns tab and cannot be edited.
- **Experiment name** (the box next to Run queue): what these runs belong to, in your own words ("Dev analysis, first try"). The queue is a reusable recipe; the experiment is one use of it, so you do not rename the queue each time. Every run is recorded under the name, and Results can filter by it. The same name continues where you left off; a new name starts fresh (the old runs stay but do not count for it). Run queue stays disabled until a name is typed.
- **Restart** (asks "are you sure?" first): runs the whole queue again from step 1 as a new round of the same experiment, recorded as "name (restart 2)", "name (restart 3)" and so on. Nothing is deleted; older rounds stay in Results under their own label. It is only enabled when the name already has runs.
- **Restart my share** (only on a queue split across machines, and only after you pick your share): starts just your share again under the same experiment name. The runs your share has recorded under this name stop counting (the queue, the board and Run queue ignore them), so its cells run again; the other shares and other experiments are not touched. Nothing is deleted: the old runs stay in Results, and a later export still lists them, so tell the analysis which round counts (the newest run per cell wins). It is refused while a job runs on this machine. Press Run queue afterwards. The mark is shared with the other machines through the store, so their board shows the share as restarted too.

### The Queue tab's five screens

The experiment name, your share and the Run / Pause / Restart buttons sit in one bar at the top and stay in the same place. Under it are five screens:

- **Overview**: live numbers for the whole queue under the experiment name: cells with a result, passed, failed, incomplete, left, the pace of the last 24 hours (attempts per hour, all machines), a time-left estimate, and how many shares are running. Below: a table of problems (done, pass rate, one square per step, and **who ran it**), a table of people (cells, pass, fail, what they are running now) and a chart of attempts per hour by person. The time-left estimate uses the pace actually recorded in the last 24 hours, stops included, so it is longer than a "non-stop" estimate; it is an estimate, not a promise. The page refreshes itself every minute and when you open the screen.
- **Steps**: one row per step (Compact) or the full cards with every problem and repeat (Detailed). The arrow at the end of a row opens that step's problem-by-repeat grid.
- **Shares & people**: the board of a queue split across machines, with **Assign** and **Redistribute…** (below).
- **Screening**: the report for a switch ladder (below).
- **Log**: the live output.

### Who runs which share, and moving work between shares

- **Assign**: write down who runs a share (pick known people, or type a name). It is a plan, not a lock: the board shows it, "This machine runs" warns when you pick a share assigned to someone else, and the Run dialog asks you to start it only if they agreed. If your name is assigned to one share, the picker tells you which.
- **Redistribute…**: take the work of a share that is not done yet (for example a machine that cannot run it) and decide where it goes. Step 1 ticks the shares that may take work (buttons: All, None, "Not started or finished only" to leave out a share somebody is running now, and "keep some here too" to level the share itself as well). Step 2 is one line per unfinished problem with the number of **runs** left (a problem is one run per enabled step, so a problem of an 18-step queue is 18 runs) and a dropdown: stay, or go to share k. **Auto-balance by runs** fills the dropdowns for you, biggest problems first, each to the share with the fewest runs left; change any line by hand afterwards. Step 3 shows runs left per share before and after, live. Apply saves it. Finished problems cannot move and stay where they ran, so who ran what does not change.
- Both change the saved queue's shares, and the change is shared with the team through the store. A machine that is already running keeps the shares it started with until it presses **Continue** or **Run queue** again; the dialog warns when the share you touch is running. Type the experiment name first (what is left is counted under it) and save the queue first.

### Quick runs and the queue

A run started from **Setup & quick run** is recorded under its experiment name like any run, but it carries `origin: "quick"` and is never counted as a cell of a queue, even with the same settings and the same name. Runs started from the Queue tab carry `origin: "queue"`. Runs recorded before this change have no origin and still count.
- **Resume**: only cells with no result yet (under the current experiment name) are run, so a stopped, crashed or interrupted queue continues where it left off. A run with no known outcome is "incomplete", is run again, and is never counted as a failure.
- **Start queue** runs on the server, not in the browser tab. Close the tab, lock the laptop screen, reopen later and the page re-attaches to the live output. One job runs at a time because SREGym drives a single cluster. **Stop** finishes the current run and then stops.
- **Save queue** keeps the ordered list under a name so a teammate can load it.

### One queue, several machines

A queue can be shared by the whole team and split across machines, instead of one queue per machine.

- **Machines** (next to the queue name): how many machines share the queue, for example 5. Everyone opens the same saved queue and sees every step and every cell.
- **Split**: *by problem* gives each machine whole problems (all their repeats); *by problem and repeat* gives each machine (problem, repeat) pairs, which evens out the load when the problems do not divide by the number of machines (12 problems x 3 repeats on 5 machines: 8, 7, 7, 7 and 7 pairs, instead of 3, 3, 2, 2 and 2 problems). Either way every step of a pair runs on the same machine, so the configurations are compared on the same cluster. The split is fixed by the queue, never by what is left, so two machines on different shares never run the same cell. A queue made by `scripts/make_ladder_queues.py` may carry hand-made shares; problems added later go to the smallest share.
- **This machine runs** (in the run bar): pick your share, k of N. It is remembered on this machine for this queue. Run queue, Continue and the counts then cover only your share; the grid dims the other shares, and "Whole queue" shows the total left across all machines. Agree who takes which share first. If someone else recorded runs in your share in the last three hours, the run bar warns you.
- **Queue board** (under the run bar, open by default): one row per share with who took it (name and machine), its status, done, pass / fail, left and its problems. A machine takes a share the moment it starts the queue on it, so the board shows it before the first run of that share is recorded. Status is one of: *running now* (with the problem and when the machine was last heard from), *paused*, *stopped* (the queue stopped itself, with the reason, or was cut off), *queue ended with N left*, *finished* (every cell has a result), *no sign of life* (it said running but nothing was heard for 40 minutes, so the Lab or the machine may have stopped) and *not taken yet*. The board reads the shared results, so it fills in once Firebase is connected, and it refreshes by itself every minute while the Queue tab is open. A claim is information, not a lock: if someone else is running your share right now, the note under the share picker and the start dialog both say so, and you can still start if you agreed it. The claim is kept per experiment name, so a Restart begins with an empty board. **Type the experiment name first:** progress, claims and the board are counted per name, so until a name is typed the board shows a prompt (with the three newest names to click) and every cell counts as to do; it never shows other experiments' runs as this one's. The board is its own block under the run bar, not part of it; its columns and row heights stay fixed (the full status and problem list are in the tooltip), and it keeps the last good numbers when a refresh fails.
- **Run order**: *step by step* runs every cell of step 1, then step 2, and so on. *Problem by problem* runs, for each (problem, repeat), every step back to back (each pair starting one step later than the last, so no step is always first). Use it when the steps are configurations you want to compare: they then run close together on the same machine, so a slow day of the model service falls on all of them alike instead of on one step. The agent image is built once, on the first run.
- Everyone uses the **same experiment name**, so all shares fill the same grid.

Frozen campaigns already had a "k of n" slice in the Campaigns tab; it is now counted over the campaign's whole grid, so a slice no longer shifts (and overlaps another) while other machines finish cells.

## Tasks

The Tasks tab tells each person what they have to do now, so the team learns the next piece of work from the Lab itself. I write the tasks; everyone reads them.

- **A task** has a title, details, a phase (for example "Round 2 - main stage"), who it is for (names; empty means everyone), and optionally a saved queue and an experiment name. **Open queue** on a task opens that queue with that experiment name already typed in, so nobody has to copy names around. Example: "Run STRATUS on the 12 dev problems", phase "Round 2 - main stage", for Noura and Ahmed, queue "Ladder R2 - main", experiment "Dev - STRATUS - dev12".
- **Reading**: type your name in the box at the top right. **My tasks** shows what is for you (a number on the tab says how many are still open); **Everyone's** shows the whole list, grouped by phase. A task for several people shows each person's status next to their name.
- **Marking progress**: on a task that is for you, press To do, Doing, Done or Blocked, with an optional note (what you did, what blocks you). Only people named on the task can mark it; a task with no names is open to everyone. Your status is saved under your name and shared with the other machines.
- **Adding, editing, deleting** needs the owner password: put `LAB_OWNER_PASSWORD=your-password` in the Lab's own `.env` (next to `backend/` and `frontend/`; git-ignored), then press **Unlock to add tasks** on the Tasks tab and type it. No restart is needed. Without that line the Tasks tab is read-only on that machine, which is the normal state for everyone except the project owner. A wrong password waits a second before answering.
- **What this protects, honestly**: the check is on the Lab's server, like the delete password, so a teammate cannot add tasks from a normal Lab without the password. Tasks are stored in the same shared store as every other record, though, so someone who deliberately sets their own `LAB_OWNER_PASSWORD` could add one. It keeps accidents and wrong clicks out; it is not security against a teammate who wants to get round it.
- **Phases**: the phase is just text. Use the same words on tasks that belong together and they are grouped under one heading, in the order they were created.

## Setup & quick run

The one-off page for setting up a machine and for a single quick run.

In **2 · Agent & model**, the **Variant** list holds the same ready-made variants as the queue (V0, B, R2 NULL, R2 B2, R2 ALL, the one-on and one-off R2 rows and so on). Picking one sets the agent, the config label and the agent's switches for the run, and the line under the list shows which switches are on. Variants that wait for the reviewed Kubernetes notes are greyed out. Changing the Agent list by hand goes back to *Custom*, which runs the agent with no switches. Example: to try one problem on the round 2 base, choose **R2 B2**, give the run an experiment name, choose the problem (Custom problem box) and press Run.

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

- **The agent cannot start (our agents only).** Before the first run the Lab checks that SREGym's `agents.yaml` has an entry for
  `reliops_v1` / `reliops_v2` and that `clients/<agent>/driver.py` exists in the SREGym clone. If not, nothing is started and the
  message names the fix (`python3 scripts/apply_sregym_patches.py` and `bash scripts/sync_agent.sh`). If the agent still dies at
  start (SREGym's "not found in registry", or "No module named 'clients....'" from an image built without our code), the queue
  stops at the first such run instead of after three, and the reason says what to do. Other unrecognised endings now quote the last
  error line of the log and the agent's exit code instead of only "ended without a result".

- **Stops after repeated empty runs.** If 3 runs in a row end incomplete (no result row, or SREGym marks the run incomplete), the queue stops and says why, instead of spending the night on empty runs. Finished runs are kept and only missing cells run on the next start. Change the limit with `RELIOPS_MAX_INCOMPLETE=5` in `.env`; `0` turns the stop off. Incomplete runs are never counted as failures in Results.
- **Checks before the first run.** The log starts with a `[check]` line if `AGENT_API_BASE` is set (SREGym then lets the agent reach only that host, so a Gemini model sent to a gateway usually fails with 401 "Invalid credential"), or if the agent image looks older than `agent/react_v1/driver.py` (run `scripts/sync_agent.sh` and tick Force-build once).
- **A step that stops takes the whole queue with it.** When a step stops itself (3 incomplete in a row, a broken cluster, every key dead), the later steps are not started, because they would hit the same fault. The log says how many were left.
- **Outside faults are tried again inside the same pass.** If a run dies because a key was rejected or out of quota (and the pool has another key), or because the model sent empty replies, the same cell runs again at once, up to 2 times (`RELIOPS_RETRY_EXTERNAL=0` turns it off). These retries are not counted toward the stop above. Cluster faults and unknown causes are never retried. Every attempt stays in the record; the latest one counts for the cell.
- **Mark a run as an external fault.** In a run's review form pick the tag `external-fault` (and write why in the note). The run stays in the record but is not counted as a pass or a fail, and its cell is run again the next time the queue starts with the same experiment name. Use it only for a cause you can point to in the run's log that has nothing to do with how the agent behaved (a rejected key, a node that went down, a result recorded against the wrong run), decided the same way for runs that look like passes and runs that look like failures. Never use it because a failure looks unfair: redoing only the failures raises the pass rate without the agent getting better. Write the log line that proves it in the note.
- **Cluster not ready stops the queue at once.** If a run ends before the agent started because nodes or `kube-system` pods never became Ready, or cleanup did not finish, the queue stops at that run instead of waiting for the third. A broken cluster does not heal between runs, and each attempt can cost about half an hour. (`RELIOPS_MAX_INCOMPLETE=0` turns this off too.) Before every run the log also shows a `[check]` line naming any node that is not Ready; the run still goes ahead, because SREGym revives a stopped kubelet at the start of each problem.
- **Split by repeat really mixes now (10 Nov 2026).** A new queue split by repeat gets its shares written down when it is saved, dealt so that the repeats of one problem land on different machines. Before, 12 problems on 4 machines (or 2, 3, 6) gave each machine whole problems, the same as a split by problem. Queues that already exist keep their shares.
- **Nodes are checked before every run (9 Nov 2026).** If a kind node is not Ready, the Lab waits 60 seconds, then restarts containerd and kubelet inside that node once (the fix for 'container runtime is down' while containerd is alive), waits up to 150 seconds more, and stops the queue with the node names if it is still down. Without this, SREGym waits 2 x 120 s for the nodes plus 600 s for the pods and retries up to 3 times, about 30 minutes for no result. Finished runs are kept; press Continue after fixing. Set `RELIOPS_NODE_GATE=0` in `.env` to go back to the old warning-only check.
- **Why a run is incomplete.** The Results table has a **Why incomplete** column. New runs store the reason; older runs get it read from their own log when that log is on this machine. It reads SREGym's own wording (nodes or pods not Ready, cleanup not finished, key rejected or out of quota, judge check failed); anything else says whether the agent ever started.
- **Rejected credentials.** A 401 / AuthenticationError marks the key dead when the request went straight to the provider. When `AGENT_API_BASE` is set it does not, because the gateway rejected the key, not Google.

### Cluster health

Setup & quick run has a **Cluster health** card. **Check now** reads the local kind cluster (nodes, `kube-system` pods that are not Ready or stuck in Terminating, the node containers, free memory and disk) and says in plain words what is wrong. It also refreshes whenever you open the tab. Checking only reads.

Three repair buttons sit under it. Each one shows the exact commands and asks before it does anything, runs as the current job (its output streams in the Queue tab), and is refused while a run or queue is going:

- **Restart the kind nodes**: `docker restart` on the node containers, then waits up to 4 minutes for every node to report Ready. Nothing is deleted. Try this first.
- **Clear stuck system pods**: force-deletes pods that have been in Terminating for over a minute in `kube-system` only; their controllers create new ones.
- **Rebuild the cluster**: `kind delete cluster`, then the same setup the Run setup button runs (about 10 minutes). Everything in the cluster is lost. Use it when restarting does not help. Not tested against the cached baseline state SREGym keeps outside the cluster.

There is no free-text command box on purpose: the page sends only the name of a repair, and the server builds the commands from names it reads itself. A box that ran typed commands would let any web page that reaches `127.0.0.1` run commands on this machine.

## Shared results page (read-only)

A static page that shows the experiment results and the queue's progress to people who do not have the Lab open
(teammates, supervisors, sponsors). It cannot start, stop or change anything.

- **Preview on this machine:** open `http://localhost:8765/share/`. It is computed on request from the same runs the Results tab
  uses, so it works without any setup.
- **What it shows:** the live state (running, paused, halted, finished; experiment, step, current problem, time on this run,
  runs left, estimated time left), a per-variant table (end to end, mitigation, diagnosis with 95 percent Wilson intervals,
  median calls and seconds, mean tokens), a problem-by-variant grid, the latest runs, and why runs did not finish.
- **What is published:** per run the problem, category, agent, config label, switches, experiment, outcomes, judge scores, calls,
  tokens, seconds, failure tag, incomplete reason and who ran it. Never keys, the `.env`, diagnosis text, judge reasoning,
  logs, stories, file paths or e-mail addresses. Runs of the frozen set (purpose `final`) are left out unless
  `RELIOPS_PUBLISH_FINAL=on`, so the page cannot be used to read frozen-set failures before the freeze.
  `RELIOPS_PUBLISH_NAMES=off` leaves out the `run_by` names.
- **Hosting it for others:** create an empty repository for the page (for example `ReliOps-Results`, public is fine because it holds
  only the files above), switch on GitHub Pages for its `gh-pages` branch, then put in the Lab `.env`:

      RELIOPS_PUBLISH=on
      RELIOPS_PUBLISH_GIT=https://github.com/<you>/ReliOps-Results.git

  The Lab then writes the files to `results/site/` whenever a run is recorded or the queue changes state, and pushes them
  (one replaced commit on `gh-pages`, using the git credentials already on the machine) at most every
  `RELIOPS_PUBLISH_EVERY_S` seconds (default 60). The elapsed time of the current run
  ticks in the browser between updates. `GET /api/publish/status` shows the last push, any error, and a note when the Lab had to
  repair itself. If the page goes stale, open that address first: `last_error` says why. A damaged local publishing repository (an empty
  object file after a hard stop) is recreated automatically; by hand, delete `results/site/.git` inside the Lab's ReliOps folder and the
  next push rebuilds it. Nothing is lost, because the data branch holds one commit that every push replaces.
- **Hosting it on Vercel (phone-friendly, free):** the page is static and is deployed once; only the two data files change. The Lab
  publishes them to the `results-data` branch of a **separate, public repository** (`ReliOps-Lab-data`), and `site/config.js` tells the
  page to read them from `raw.githubusercontent.com`. The data must not live in the repository Vercel builds: Vercel treats every branch
  push as a deployment, the data changes about every minute, and the Hobby plan allows 100 deployments a day (a data branch in this
  repository produced a failed preview for every data push). Create `ReliOps-Lab-data`
  empty and public (raw links do not work for a private repository), then in the Lab's `.env`:

      RELIOPS_PUBLISH=on
      RELIOPS_PUBLISH_GIT=https://github.com/OmarElzero/ReliOps-Lab-data.git

  In Vercel: Add New Project, import **this** repository, set Root Directory to `site`, Framework Preset `Other`, Deploy. Do not connect
  the data repository to Vercel. The Lab must be running for the page to update; when it is not, the page keeps the last data and says
  how old it is. With another repository or branch, edit the address in `site/config.js`.
- Data from teammates appears when the Lab reads it from Firestore (it uses the same saved copy and read budget as the Results tab).

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

## The experiment name, deleting queues and Restart

Results belong to the **experiment name**, not to the queue. Deleting a queue, or making the same queue again, never removes results: type the same name and every cell that already has a result under it is skipped. A queue remembers only the name typed for it on this browser; a queue opened for the first time starts with an empty name and Run queue stays off until you type one. Under the name the Lab says how many runs are recorded under it, by whom, when, and with which agent commit; if that commit is not the one this machine runs, it tells you to type a new name when this is a new attempt.

**Restart** starts a new round (`name (restart 2)`) for the whole queue, for every share and every machine, not only yours. The other machines join the newest round when they press Run queue. On a shared queue only one person presses it; if the newest round already has runs, the confirmation tells you someone has restarted and to use Run queue instead.

**Restart my share** is the smaller tool: it sets aside only your share's earlier runs under this name (a marker is stored; nothing is deleted) and leaves the other shares alone. Use it when one machine's share went wrong (a broken setup, a bad key) and only that share should be redone. Ask anyone running the same share to pause first. The marker uses the clock of the machine that pressed the button, so keep machine clocks right.

## Campaigns

**Queue results** (top of the tab) gives one line per experiment, which is the name typed when a queue starts. Old experiments are never hidden or removed (deleting a queue does not touch its results); they are grouped by when they last ran (Today, Yesterday, Last 7 days, Last 30 days, Older), and you can order them (newest, oldest, name, most runs), show one person's only, or show only the ones that need attention (failures or cells without a result). Opened experiments stay open when you filter or change tab, and your choices are remembered on this machine. Open the arrow to see each configuration, then each problem: solved, partly solved or not solved, a mark for every repeat (hover for who ran it, when, and any reason it has no result), the time and the tokens. Problems that need a look come first. The rules are the queue's own: the latest run of a (problem, repeat) cell counts, a run with no outcome is shown as "no result" and never as a failure, and a "(restart 2)" round belongs to the same experiment. It reads the shared results when they are connected, so the whole team's runs for one experiment name appear together. Time is the whole run (from the start of the problem to the recorded result). Runs recorded before this existed only have SREGym's diagnosis and mitigation timers; their time is labelled "agent time".

A **frozen campaign** fixes what is measured: agent, model, action space, judge, label, problem set (by hash) and repeats, recorded with the agent code commit. Runs that do not match the manifest are refused. The grid shows every (problem, repeat) cell as pass, fail, incomplete or missing. A machine can take a slice (machine k of n) so several laptops split one campaign without overlap.

## Judge audit

Check the judge against people. Hand-label diagnoses blind (the judge's verdict is hidden), then see per-judge agreement and Cohen's kappa. You can re-judge stored diagnoses with another model and compare judges. About 30 labels are needed before a kappa means much.

### Assigned audit (blind sample, named labelers)

The top of the Judge audit tab holds a frozen, blind sample of judged diagnoses, split across named people.

- **Owner draws it** (needs `LAB_OWNER_PASSWORD`, same as the Tasks tab): open "Owner: draw the sample", unlock, type the audit name, the labelers' names and the groups, then Preview and Draw and assign. A group line is `prefix | split or pooled | runs`: the prefix matches the start of a configuration label, `split` makes one stratum per configuration, `pooled` makes one stratum from all of them. The default `R3 | split | 12` and `R2 | pooled | 12` gives 12 runs per round 3 configuration plus 12 pooled from round 2. Half of every stratum is runs the judge passed and half it failed, at most 2 runs per problem per stratum. **Whole problems per person** (the default) gives each labeler whole problems, so nobody has to learn all of them; each person also labels a few of the previous person's problems (the overlap setting), which is what measures how far the labelers agree. "Spread" deals runs round-robin instead.
- **Owner changes it afterwards** ("Manage this audit"): Redistribute re-deals the items nobody has labelled among the people you list (whole problems stay together, every label already given is kept); Add runs draws more by the same rules, never a run already in the sample; Remove drops unlabelled items; Delete hides the audit (its labels stay in the store). Every change is written to the audit's history. A name with labels or a deleted name cannot be drawn again.
- **Everyone else**: open the tab, pick the audit and your name. Your problems are listed with how many runs of each you hold: read a problem's card once ("Show the full problem card" opens it right there, or open the Problems tab and come back; your answers stay), then label all its runs. D1 localization, D2 characterization and D3 scope are Yes or No against the card text shown with the diagnosis; overall is Correct when D1 and D2 are both Yes. Add a confidence and, when the answer is Incorrect or you are unsure, one sentence of reason. You never see the configuration, the judge's verdict or anyone else's answer. The judge only saw the diagnosis text, so judge that first; if it is ambiguous you can open "Show the agent's story" (the step-by-step transcript), and the Lab records that you did, so the score reports how many labels used it. Run logs are not offered here because they print the judge's verdict. When every item you hold has an answer, press "Finish and see the judge's verdicts": your answers are locked, and each item then shows the judge's verdict and reasoning next to yours, with the differences first (nobody else's answers are shown). Items added to you later can still be labelled. Work alone and do not discuss items until everyone has finished.
- **Owner reads the score**: judge against the human consensus (agreement with a 95% Wilson interval, kappa, judge-pass-but-human-wrong and judge-fail-but-human-right counts, by stratum), labeler kappa on the shared runs, progress per person, and a list of the runs where judge and human differ. The score is owner-only because it shows the judge's verdicts.

**Second judge on the sample** (owner panel): type another judge model as SREGym's judge takes it (for example `openai/<model>`) and press "Re-judge this sample". It re-grades the same diagnoses with SREGym's own judge and reference answer, only the model changes; it runs in the background on that machine inside SREGym's environment, so the model's API key must be set there. Closing or refreshing the page does not stop it, each verdict is saved as it arrives, and pressing the button again resumes with the runs that have no verdict yet (errors are retried). The full output is also written to `results/gui_logs/`. The score then shows how often the two judges agree, overall and per stratum, the runs where they differ, and the agreement with the human labels if there are any. Two judges agreeing is not proof that both are right; it complements a human audit.

The small "Quick hand labels" box below it is the older, unassigned queue; it is not part of the assigned audit.

## Problems

One card per problem (what the fault is, expected difficulty, notes) and the attempt log for it. The Results tab links here from a run.

## Pinned sets

`experiments/pins.json` in the ReliOps repo fixes the problem sets for the current phase by a hash of their ids: the frozen 30 and the dev 12. The Lab compares each CSV with its pin and warns "CHANGED since pinned" in the problem-set list if a file was edited. To change a pinned set, add a new entry with a written reason; never edit one in place.

## A safe first session

1. Run **Demo** to see the output, then a machine check.
2. Run a single problem with the dumb baseline, then the agent on one dev problem, and confirm the run appears in Results with its diagnosis text.
3. Only then queue a long run, and start with the dev 12 so any pipeline problem shows up on problems you are allowed to read.

## Agent switches (ReliOps v2)

In a queue step, choose an agent named "ReliOps v2" and tick the switches the variant should have; the config label names the
variant (for example `B+SURVEY`). Every switch you did not tick is passed to the agent as off, whatever the `.env` says, and each
run records the switches that were on (Results can filter on them; the experiment zip lists them). Choosing switches with any
other agent is refused. With none ticked, v2 behaves like v1. The switches and what they do are in
`docs/plan/12-agent-ladder.md` of the ReliOps repo. Run `bash scripts/sync_agent.sh` and tick "Force-build the agent image" once
after updating the repo so the container has the v2 code.

## Tokens

Token counts come from the agent's transcript. Runs recorded before 31 Oct stored 0 (the wrong field names were read); when the
transcript is still on the machine, Results now shows the real counts for those runs. Stored values are never changed.

### Machine versions and stale agent code

Every run records the tools of the machine that ran it (`machine_versions`: kind, kubectl, the cluster's Kubernetes version,
Docker, Helm), and the queue's "Machine shares" panel warns when runs of one experiment come from machines with different
versions. Before the first run the Lab also checks that the agent code on this machine knows every switch the step turns on.
If it does not (for example the ReliOps clone is older than the switch), the queue stops and says what to run: Run setup, then
`bash scripts/sync_agent.sh`, then tick "Force-build the agent image". An agent silently ignores a switch it does not know,
so without this check a run would record the switch and not apply it.

### Screening (the switch ladder)

The **Screening** screen of the Queue tab turns the runs of a switch ladder into the numbers the screening rule needs (docs/plan/15 sections 7 and 8, rule in docs/plan/16 section 3), so nobody counts by hand. Type the experiment name at the top of the Queue tab first: the report is made from the runs recorded under that name (newest round only, "Restart my share" applied, quick runs never counted).

- **Configurations**: for each of the 18 ladder configurations, the cells with a clean result, passes, pass@1 with a 95% Wilson interval, how many problems pass in all their repeats, and the cells still missing with the worst case they could hide.
- **Contrasts**: 16 paired comparisons on the same (problem, repeat) pairs: 8 *one-on* (base + one switch against the base) and 8 *one-off* (all switches against all switches minus one). Each is an exact two-sided sign test on the pairs where the outcome flipped, with a Holm correction over all 16. With these 16 tests, 6 flips in one direction and none the other way give p = 0.031 before correction; after Holm you need 10 flips.
- **Verdict per switch**: *no data* (no pair in common yet), *no evidence* (no contrast passes the exact test; this is not "no effect"), *candidate* helps or hurts (a contrast passes the test without correction and the one-on and one-off effects point the same way), *finding* helps or hurts (the same, and it also passes with Holm), or *mixed* (a contrast passes but the two do not point the same way, so the rule does not count it). The candidate configuration for the next stage is the one doc 15 section 7 builds from the signs of the effects.
- **Missing is not failed**: a run without a clean result (no outcome, or a person tagged it as an external fault) is set aside and the cell is reported as missing. A warning appears when fewer than 80% of the cells have a result.
- **Controls**: *Base* and *All switches* are the config labels (defaults `R2 B2` and `R2 ALL`). *Set aside commits* lists commits whose runs should not count (for example runs made with older agent code under the same experiment name). *Keep problems the null agent passes* turns off the default of leaving out problems the null agent already solves. Downloads give the two tables as CSV and the report as Markdown.
- It is a screening on development data, not a claim: gains are reported only from the frozen 30.

### Sharing run logs between machines

Every run writes a raw log on the machine that ran it. Since logs are what you read to find out *why* a run failed, each new run now also shares its log with the other machines (Firestore collection `run_logs`, one document per run). The log is masked first (values of API keys, tokens and passwords are replaced; ordinary numbers such as `tokens=1234` are kept), compressed, and stored only if it fits in one document (about 700 thousand characters after packing); a longer one stays local and the run is marked as not shared. A teammate opening the Results tab sees your log as if it were theirs; it is fetched when opened and cached, one document read per log. The experiment zip also falls back to the shared log when a log is not on the machine that builds the zip.

- **Share my earlier logs** (Queue tab, Log screen) shares the logs of runs made on this machine before this feature existed. It only sends runs made here.
- **Turn it off**: `RELIOPS_LOG_MIRROR=0` in the Lab's `.env`. The local log is never touched either way.
- Masking is pattern-based, so it is a safety net, not a guarantee: do not paste a key into a prompt or a problem description and rely on the mask to hide it.

### Check the sync between two machines (10 minutes)

The automated tests use a fake Firestore. To check the real one once, with Omar's machine (A) and one teammate's (B), both on the latest `main`, both with Firebase connected, and the queue paused on both:

1. On A, save a queue with 2 or more shares. On B, open the Queue tab: the queue is listed. If it is not, check the Firebase pill in the page header on both machines (it should say connected) and reload.
2. On A, **Assign** share 2 to B's person. On B, type that name in the header: the board shows the assignment.
3. On A, type an experiment name, then **Redistribute…** one unfinished problem to another share and Apply. On B, reload: the problem sits in the new share.
4. On A, unlock the Tasks tab, add a task for B's person with this queue and the experiment name. On B, the Tasks tab shows it with a number on the tab; press Doing with a note. On A, **Everyone's** shows B's status within about 90 seconds.
5. On B, run one cheap problem. On A, the Overview counts it under B's name, and opening that run in Results shows its log (it comes from the shared copy). The run's `machine_versions` appears in the machine-versions panel.
6. If any step fails, note which step, the Firebase pill text on both machines and the Lab's console output; do not retry more than once.

## Deleting a saved queue

Delete asks for a password. Put `LAB_DELETE_PASSWORD=your-password` in the Lab's own `.env` (next to `backend/` and `frontend/`; the file is git-ignored). Nothing needs restarting: the server reads it on every delete. Without that line nobody can delete a queue on that machine, so a teammate who pulled the code but has no password cannot remove a queue by mistake. The check is on the server, a wrong password waits a second before answering, and a deletion is shared with the other machines like any other queue change, so keep the password on the machines that should be able to delete. Results already recorded are never deleted with a queue.

## Run setup says "could not read Username for 'https://github.com'"

Git on that machine has no GitHub sign-in (the ReliOps repo is private). Run setup now **stops** with the steps to fix it and says which commit the clone is still on,
instead of carrying on with old agent code that would not match the other machines. Once per machine: be added as a collaborator on the ReliOps repo, then run
`gh auth login` (or save a personal access token with `git config --global credential.helper store` and one manual `git pull`), and press Run setup again.
If git fails for another reason (no network), Setup warns with the commit it is on and continues.
