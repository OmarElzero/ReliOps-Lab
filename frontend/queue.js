// ReliOps Lab — Queue screen: line up runs (steps), run them one after another on the server, watch them.
// Loaded after app.js and v2.js; uses their globals (CONFIG, jsonGet, jsonPost, who, showTab, TAB_LOADERS).

(() => {
  const $ = (id) => document.getElementById(id);
  const pref = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked: fine */ } },
  };
  function h(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    kids.flat().forEach((c) => { if (c != null && c !== false) n.append(c.nodeType ? c : document.createTextNode(String(c))); });
    return n;
  }
  const uid = () => Math.random().toString(16).slice(2, 10);
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec));
    const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
    if (hh) return `${hh} h ${String(mm).padStart(2, "0")} min`;
    if (mm) return `${mm}:${String(ss).padStart(2, "0")} min`;
    return `${ss} s`;
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---- theme ----------------------------------------------------------------------------------
  function applyTheme(t) { if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
  applyTheme(pref.get("lab_theme", ""));
  $("theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    applyTheme(next); pref.set("lab_theme", next);
  });

  // ---- state ----------------------------------------------------------------------------------
  const blankQueue = () => ({ queue_id: null, name: "", steps: [], machines: 1, shares: null, split: "problem", order: "step" });
  let Q = blankQueue();
  let saved = [];
  let manifests = [];
  let dirty = false;
  let preview = null;          // server counts for the queue as drafted
  let job = null;              // last /api/state job
  let es = null;               // live log stream
  let lastId = 0;              // absolute index of the last log line shown
  let lastRecorded = -1;
  let previewTimer = null;
  let editing = null;          // index of the step being edited, or null for a new one
  let stopping = false;        // a stop was requested for the job in progress
  let lastSig = "";            // what the step list was last drawn for; avoids redrawing under the mouse

  const jobBusy = () => !!(job && job.state === "running");
  const queueRunning = () => jobBusy() && job.kind === "queue" && Q.queue_id && job.meta && job.meta.queue_id === Q.queue_id;
  const locked = () => jobBusy();   // one job at a time: editing is fine, starting is not; keep editing simple
  const minPerRun = () => Math.max(1, Number($("q-min") ? $("q-min").value : 20) || 20);

  // ---- saved queues ---------------------------------------------------------------------------
  async function loadSaved() {
    try { saved = (await jsonGet("/api/queues")).queues || []; } catch (e) { saved = []; }
    renderOpen();
  }
  async function loadManifests() {
    try { manifests = (await jsonGet("/api/manifests")).manifests || []; } catch (e) { manifests = []; }
  }
  function renderOpen() {
    const sel = $("q-open");
    sel.innerHTML = "";
    sel.append(h("option", { value: "", text: saved.length ? "Open a saved queue…" : "No saved queues yet" }));
    saved.forEach((q) => sel.append(h("option", { value: q.queue_id, text: `${q.name} (${plural((q.steps || []).length, "step")})` })));
    sel.value = Q.queue_id && saved.some((q) => q.queue_id === Q.queue_id) ? Q.queue_id : "";
    sel.disabled = !saved.length;
    $("q-delete").disabled = !Q.queue_id || !saved.some((q) => q.queue_id === Q.queue_id);
  }
  // ---- experiment name: what the runs belong to, typed in the run bar, remembered per queue -----------------
  const expKey = () => "lab_exp_" + (Q && Q.queue_id ? Q.queue_id : "new");
  const expName = () => ($("q-exp") ? $("q-exp").value.trim() : "");
  const roundOf = (label) => { const m = /^(.*?)(?: \(restart (\d+)\))?$/.exec(label || ""); return { base: (m && m[1]) || "", n: m && m[2] ? Number(m[2]) : 1 }; };
  // A queue remembers only the name typed for it. It never borrows the last name typed on another queue: a shared queue opened
  // fresh used to arrive with an old name already filled in, and every cell then looked done.
  function loadExpName() { $("q-exp").value = pref.get(expKey(), ""); }
  function renderExperiments() {
    const dl = $("q-exp-list");
    dl.innerHTML = "";
    ((preview && preview.experiments) || []).forEach((e) => dl.append(h("option", { value: e.name, label: `${plural(e.runs, "run")} recorded` })));
    const note = $("q-exp-note");
    const name = expName();
    if (!name) { note.textContent = "Give this experiment a name; every run is recorded under it."; return; }
    const known = ((preview && preview.experiments) || []).find((e) => e.name === name);
    const r = roundOf(preview && preview.experiment_label);
    const li = (preview && preview.label_info) || { runs: 0, people: [], commits: [] };
    const differentCode = li.runs && li.commits && li.commits.length && preview.this_commit && !li.commits.includes(preview.this_commit);
    note.className = "hint" + (differentCode ? " warn-text" : "");
    note.textContent = !known ? "New name: nothing recorded under it yet, so every cell is to do."
      : `${plural(li.runs || known.runs, "run")} already recorded under this name${li.people && li.people.length ? " by " + li.people.join(", ") : ""}`
        + `${li.first ? " (" + fmtWhen(li.first) + (li.last && li.last !== li.first ? " to " + fmtWhen(li.last) : "") + ")" : ""}${r.n > 1 ? `, counting round ${r.n}` : ""}. `
        + (differentCode ? `They were recorded with agent code ${li.commits.join(", ")}; yours is ${preview.this_commit}. If this is a new attempt, type a new name. ` : "")
        + "Same name continues from there; a new name starts fresh. Deleting a queue never deletes these results.";
  }

  function openQueue(q) {
    Q = q ? JSON.parse(JSON.stringify({ queue_id: q.queue_id, name: q.name, steps: q.steps || [],
                                        machines: q.machines || 1, shares: q.shares || null, split: q.split || "problem", order: q.order || "step" })) : blankQueue();
    dirty = false;
    pref.set("lab_queue_id", Q.queue_id || "");
    $("q-name").value = Q.name;
    $("q-machines").value = String(Q.machines || 1);
    $("q-split").value = Q.split || "problem";
    $("q-order").value = Q.order || "step";
    loadExpName();
    renderAll(); refreshPreview(true); renderOpen();
  }
  function markDirty() { dirty = true; renderHead(); renderTotals(); refreshPreview(); }

  async function saveQueue(quiet) {
    Q.name = $("q-name").value.trim();
    if (!Q.name) { $("q-name").focus(); setStatus("Give the queue a name first.", "warn"); return false; }
    const { ok, data } = await jsonPost("/api/queue", { queue_id: Q.queue_id, name: Q.name, steps: Q.steps, created_by: who(),
                                                        machines: Q.machines || 1, shares: Q.shares, split: Q.split || "problem", order: Q.order || "step" });
    if (!ok) { setStatus(data.error || "Could not save the queue.", "warn"); return false; }
    Q.queue_id = data.queue.queue_id;
    dirty = false;
    pref.set("lab_queue_id", Q.queue_id);
    await loadSaved();
    renderHead();
    if (!quiet) setStatus("Saved.", "saved");
    return true;
  }
  function setStatus(text, kind) {
    const c = $("q-state");
    c.hidden = !text; c.textContent = text || ""; c.className = "chip" + (kind ? " " + kind : "");
    if (kind === "saved") setTimeout(renderHead, 2500);
  }

  // ---- preview (cell counts from recorded runs) ---------------------------------------------------
  // Answers can arrive late and out of order (a slow one used to overwrite a newer one), and a failed request used to blank the
  // numbers and the board until the next success. Now: only the newest request may draw, a failure keeps the last good numbers
  // (three in a row clear them), and an answer identical to what is on screen redraws nothing.
  let previewSeq = 0, previewFails = 0, previewSig = "";
  function refreshPreview(now) {
    clearTimeout(previewTimer);
    if (!Q.steps.length) { previewSeq++; preview = null; previewSig = ""; renderSteps(); renderTotals(); renderOverview(); return; }
    previewTimer = setTimeout(async () => {
      const seq = ++previewSeq;
      const { ok, data } = await jsonPost("/api/queue/preview", { queue_id: Q.queue_id, name: Q.name, steps: Q.steps, experiment: expName(),
                                                                  machines: Q.machines || 1, shares: Q.shares, split: Q.split || "problem", order: Q.order || "step", share: myShare() || null });
      if (seq !== previewSeq) return;                       // a newer request exists; this answer is old
      if (!ok) {
        if (++previewFails < 3) return;                     // keep the last good numbers
        preview = null; previewSig = "";
      } else {
        previewFails = 0;
        const sig = JSON.stringify(data) + "|" + Math.floor(Date.now() / 60000) + "|" + expName() + "|" + (Q.machines || 1) + "|" + (Q.split || "") + "|" + myShare();
        if (sig === previewSig && preview) return;          // nothing changed (the minute is in the key so "5 min ago" still moves)
        previewSig = sig;
        preview = data;
      }
      renderSteps(); renderTotals(); renderExperiments(); renderShares(); renderOverview();
    }, now ? 0 : 350);
  }
  const stepPreview = (s) => (preview && preview.steps.find((p) => p.step_id === s.step_id)) || null;

  // ---- machine shares: one queue split across several machines ---------------------------------------
  // Everyone sees the whole queue. Each machine picks its share (remembered on this machine, per queue) and runs only
  // the problems of that share; 0 = this machine runs everything.
  const shareKey = () => "lab_share_" + (Q && Q.queue_id ? Q.queue_id : "new");
  function myShare() {
    const n = Q.machines || 1;
    const k = Number(pref.get(shareKey(), "0")) || 0;
    return n > 1 && k >= 1 && k <= n ? k : 0;
  }
  const ago = (iso) => {
    const t = Date.parse(iso || ""); if (!t) return "";
    const m = Math.round((Date.now() - t) / 60000);
    return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 48 * 60 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
  };
  // Someone else is running my share right now (the claim says running and was seen recently), on another machine.
  function busyElsewhere(sh) {
    const c = sh && sh.claim;
    if (!c || sh.state !== "running") return null;
    return c.machine && preview && c.machine === preview.this_machine ? null : c;
  }
  function stateText(sh) {
    const c = sh.claim || {}, since = (iso) => ago(iso);
    switch (sh.state) {
      case "finished": return "✓ finished";
      case "running": return `running now${c.now_problem ? ": " + c.now_problem : ""} · seen ${since(c.updated_at)}`;
      case "silent": return `no sign of life since ${since(c.updated_at)}; it may have stopped`;
      case "paused": return `paused ${since(c.updated_at)}`;
      case "stopped": return `stopped ${since(c.updated_at)}${c.reason ? " (" + c.reason + ")" : ""}`;
      case "ended": return `queue ended ${since(c.updated_at)} with ${sh.left} left`;
      case "worked": return `last run ${since(sh.last)}`;
      default: return "not taken yet";
    }
  }
  function renderShares() {
    const n = Q.machines || 1, box = $("q-share-box"), panel = $("q-shares");
    box.hidden = n <= 1; panel.hidden = n <= 1; $("q-split").hidden = n <= 1; $("q-shares-none").hidden = n > 1;
    if (n <= 1) return;
    const shares = (preview && preview.shares) || [];
    const sel = $("q-share"), k = myShare();
    // update the options in place: rebuilding the <select> closes an open drop-down and moves the focus
    const unitWord = Q.split === "repeat" ? "pair" : "problem";
    const texts = ["Every problem (no split)"];
    for (let i = 1; i <= n; i++) {
      const sh = shares[i - 1];
      texts.push(`Share ${i} of ${n}` + (sh ? ` · ${plural(sh.problems.length, unitWord)}, ${sh.left} left` : ""));
    }
    while (sel.options.length > texts.length) sel.remove(sel.options.length - 1);
    texts.forEach((t, i) => {
      let o = sel.options[i];
      if (!o) { o = document.createElement("option"); sel.append(o); }
      if (o.value !== String(i)) o.value = String(i);
      if (o.text !== t) o.text = t;
    });
    if (sel.value !== String(k)) sel.value = String(k);
    const note = $("q-share-note");
    const mine = k && shares[k - 1];
    const busy = busyElsewhere(mine);
    const others = mine ? mine.people.filter((x) => x.name !== who() && Date.now() - Date.parse(x.last || 0) < 3 * 3600 * 1000) : [];
    const asg = (mine && mine.assigned) || [];
    const notMine = k && asg.length && !asg.some((x) => x.toLowerCase() === who().toLowerCase());
    const myAsg = shares.filter((sh) => (sh.assigned || []).some((x) => who() && x.toLowerCase() === who().toLowerCase()));
    note.className = "hint" + (busy || others.length || notMine ? " warn-text" : "");
    note.textContent = k && notMine
      ? `Share ${k} is assigned to ${asg.join(", ")}, not to ${who() || "you"}. Pick your own share or ask the owner to change the assignment.`
      : !k && myAsg.length === 1 ? `You are assigned to share ${myAsg[0].share}. Pick it here.`
      : !k
      ? "Pick your share so the machines never run the same cells. Agree who takes which share first."
      : busy
        ? `${busy.run_by || "Someone"} (${busy.machine || "another machine"}) is running share ${k} right now, last seen ${ago(busy.updated_at)}. Two machines on one share run the same cells twice; pick another share or ask them.`
        : others.length
          ? `${others.map((x) => x.name).join(", ")} recorded runs in share ${k} ${ago(others[0].last)}. Two machines on one share run the same cells twice; check before you start.`
          : `Only the problems of share ${k} run here. The grid still shows the whole queue; the other shares are dimmed.`;
    const vers = (preview && preview.machine_versions) || [];
    const names = ((preview && preview.experiments) || []).slice(0, 3).map((e) => e.name);
    const sig = JSON.stringify([shares, vers, k, Q.split, preview && preview.needs_name, names, Math.floor(Date.now() / 60000)]);
    if (panel.dataset.sig === sig && panel.firstChild) return;          // same board as on screen: leave the DOM alone
    panel.dataset.sig = sig;
    const wasOpen = panel.querySelector("details") ? !!panel.querySelector("details[open]") : true;
    const oldScroll = panel.querySelector(".board-scroll");
    const keepTop = oldScroll ? oldScroll.scrollTop : 0, keepLeft = oldScroll ? oldScroll.scrollLeft : 0;
    panel.innerHTML = "";
    if (preview && preview.needs_name) {
      // No experiment name on this machine yet: progress is counted per name, so there is nothing to show. (It used to count the
      // runs of every experiment, and a teammate saw other experiments' work as finished shares.)
      panel.append(h("p", { class: "hint warn-text", text: "Type the experiment name above to see this queue's board. Progress, claims and who is running which share are counted per experiment name; with no name nothing is counted." }));
      if (names.length) {
        panel.append(h("div", { class: "pick" }, ...names.map((nm) => h("button", { type: "button", class: "secondary", title: "Use this experiment name",
          text: "Use: " + nm, onclick: () => { const i = $("q-exp"); i.value = nm; i.dispatchEvent(new Event("input")); } }))));
      }
      return;
    }
    if (vers.length > 1) {
      panel.append(h("p", { class: "hint warn-text" },
        "Machines differ in their tools: " + vers.map((g) => `${g.people.join(", ")}: ${g.versions} (${plural(g.n, "run")})`).join(" | ") +
        ". Runs from different versions are still recorded, but check that a difference between machines is not a difference between versions."));
    }
    if (!shares.length) return;
    buildBoard(panel, shares, k, keepTop, keepLeft);
  }
  $("q-share").addEventListener("change", (e) => { pref.set(shareKey(), e.target.value); renderShares(); refreshPreview(true); });
  $("q-order").addEventListener("change", (e) => { Q.order = e.target.value === "cell" ? "cell" : "step"; markDirty(); });
  $("q-split").addEventListener("change", (e) => {
    Q.split = e.target.value === "repeat" ? "repeat" : "problem";
    Q.shares = null;                                           // hand-made shares are lists of problems
    markDirty(); renderShares();
  });
  $("q-machines").addEventListener("change", (e) => {
    const n = Math.max(1, Math.min(20, Math.round(Number(e.target.value) || 1)));
    e.target.value = String(n);
    if (n === (Q.machines || 1)) return;
    Q.machines = n;
    if (Q.shares && Q.shares.length !== n) Q.shares = null;   // hand-made shares only fit the count they were made for
    markDirty(); renderShares();
  });

  // ---- rendering ------------------------------------------------------------------------------
  function renderAll() { renderHead(); renderSteps(); renderTotals(); renderShares(); renderOverview(); }

  function renderHead() {
    if (dirty) setStatus("Unsaved changes", "dirty");
    else setStatus(Q.queue_id ? "Saved" : "", Q.queue_id ? "saved" : "");
    if (!dirty && Q.queue_id) $("q-state").hidden = false;
    $("q-save").disabled = !dirty && !!Q.queue_id;
    $("q-empty").hidden = Q.steps.length > 0;
    $("q-add").hidden = Q.steps.length === 0;
    $("q-name").disabled = false;
    layoutViews();
    renderOpen();
  }

  function stepChips(s) {
    const chips = [
      h("span", { class: "chip", text: s.agent_label || s.agent_id }),
      h("span", { class: "chip mono", text: s.model_id, title: "agent model" }),
      h("span", { class: "chip mono", text: "judge " + s.judge_model_id }),
    ];
    if (s.config_label) chips.push(h("span", { class: "chip mono", text: s.config_label, title: "config label" }));
    if ((s.agent_switches || []).length) chips.push(h("span", { class: "chip mono", text: s.agent_switches.join("+"), title: "agent switches on" }));
    chips.push(h("span", { class: "chip" + (s.run_class === "frozen" ? " frozen" : ""), text: s.run_class === "frozen" ? "frozen campaign" : "exploration" }));
    if (s.purpose) chips.push(h("span", { class: "chip", text: "purpose: " + s.purpose, title: "set for every run this step records" }));
    return chips;
  }

  function cellsGrid(s, sp) {
    // The running cell is marked on the queue that runs, and also on another saved queue that has the same step (same name),
    // so a copy of a queue open on the screen does not lose the highlight.
    const jp = job && job.progress;
    const here = jp && jobBusy() && job.kind === "queue" && (queueRunning() ? jp.step === Q.steps.indexOf(s) + 1 : jp.step_name === s.name);
    const run = here ? jp : null;
    const byProblem = new Map();
    (sp ? sp.cells : []).forEach((c) => { if (!byProblem.has(c.problem)) byProblem.set(c.problem, []); byProblem.get(c.problem).push(c); });
    const grid = h("div", { class: "camp-grid" });
    byProblem.forEach((cs, problem) => {
      const other = cs.length && cs[0].mine === false;
      const row = h("div", { class: "camp-row" + (other ? " other" : "") }, h("span", { class: "camp-name", text: problem, title: problem + (other ? " (another machine's share)" : "") }));
      cs.forEach((c) => {
        const isNow = run && run.problem === problem && run.repeat === c.repeat && c.status !== "pass" && c.status !== "fail";
        const sym = isNow ? "…" : { pass: "✓", fail: "✗", incomplete: "?", missing: "·" }[c.status];
        const by = c.by ? ` · run by ${c.by}` : "";
        row.append(h("span", { class: `cell ${isNow ? "running" : c.status}${c.mine === false ? " other" : ""}`, text: sym, title: `${problem}, repeat ${c.repeat}: ${isNow ? "running now" : c.status}${by}` }));
      });
      grid.append(row);
    });
    return grid;
  }

  function renderStepCards() {
    const ol = $("q-steps");
    const openCells = new Set(Array.from(ol.querySelectorAll("details.step-cells[open]")).map((d) => d.dataset.step));
    ol.innerHTML = "";
    Q.steps.forEach((s, i) => {
      const sp = stepPreview(s);
      const running = queueRunning() && job.progress && job.progress.step === i + 1 && s.enabled !== false;
      const done = !!sp && sp.total > 0 && sp.todo === 0 && s.enabled !== false;
      const li = h("li", { class: "step" + (running ? " running" : "") + (s.enabled === false ? " off" : "") + (done ? " done" : ""), "data-step": s.step_id });
      const total = s.problems.length * s.repeat_n;

      const tools = h("div", { class: "step-tools" },
        h("label", { class: "switch", title: "Switched-off steps are skipped when the queue runs" },
          h("input", { type: "checkbox", checked: s.enabled !== false, onchange: (e) => { s.enabled = e.target.checked; markDirty(); renderSteps(); } }), "On"),
        h("button", { class: "ghost small", type: "button", text: "Edit", onclick: () => openStepDialog(i) }),
        h("button", { class: "ghost small", type: "button", text: "Duplicate", onclick: () => duplicateStep(i) }),
        h("button", { class: "ghost small", type: "button", text: "Copy for other agents…", title: "Same problems, repeats and models; choose several agents at once", onclick: () => openStepDialog(null, Q.steps[i]) }),
        h("button", { class: "ghost small icon", type: "button", text: "↑", "aria-label": "Move up", disabled: i === 0, onclick: () => moveStep(i, -1) }),
        h("button", { class: "ghost small icon", type: "button", text: "↓", "aria-label": "Move down", disabled: i === Q.steps.length - 1, onclick: () => moveStep(i, 1) }),
        h("button", { class: "ghost small danger", type: "button", text: "Remove", onclick: () => removeStep(i) }));
      li.append(h("div", { class: "step-top" },
        h("div", { class: "step-title" }, h("h3", { text: s.name }), h("div", { class: "step-chips" }, stepChips(s))), tools));

      const setNote = s.set_label ? ` (${s.set_label})` : "";
      li.append(h("p", { class: "step-line" },
        h("strong", { text: plural(s.problems.length, "problem") }), setNote, ` × ${plural(s.repeat_n, "repeat")} = `,
        h("strong", { text: plural(total, "run") }), s.only_missing === false && !s.manifest_id ? " · re-runs everything" : ""));

      if (sp) {
        const c = sp.counts, n = Math.max(1, sp.total);
        li.append(h("div", { class: "bar", role: "img", "aria-label": `${c.pass} passed, ${c.fail} failed, ${c.incomplete} incomplete of ${sp.total}` },
          h("span", { class: "b-pass", style: `width:${(100 * c.pass) / n}%` }),
          h("span", { class: "b-fail", style: `width:${(100 * c.fail) / n}%` }),
          h("span", { class: "b-inc", style: `width:${(100 * c.incomplete) / n}%` })));
        li.append(h("div", { class: "step-counts" },
          h("span", {}, h("b", { text: c.pass + c.fail }), ` of ${sp.total} done${myShare() ? " in your share" : ""}`),
          h("span", {}, h("b", { text: c.pass }), " passed"), h("span", {}, h("b", { text: c.fail }), " failed"),
          c.incomplete ? h("span", { title: "Ran, but no outcome was recorded. Never counted as a failure; run again on the next start." }, h("b", { text: c.incomplete }), " incomplete") : null,
          h("span", {}, h("b", { text: s.enabled === false ? 0 : sp.todo }), " to run")));
        if (sp.issue) li.append(h("p", { class: "step-issue", text: sp.issue }));
        const det = h("details", { class: "step-cells", "data-step": s.step_id }, h("summary", { text: "Show every problem and repeat" }), cellsGrid(s, sp));
        if (openCells.has(s.step_id)) det.open = true;
        li.append(det);
      }
      ol.append(li);
    });
  }

  // ---- views: Overview | Steps | Shares & people | Log -------------------------------------------------------------------
  const VIEWS = ["overview", "steps", "shares", "screening", "log"];
  let view = pref.get("lab_q_view", "overview");
  let stepsMode = pref.get("lab_q_steps_mode", "compact");
  const expandedSteps = new Set();
  function layoutViews() {
    const empty = !Q.steps.length;
    $("q-views").hidden = empty;
    VIEWS.forEach((x) => { $("qv-" + x).hidden = empty ? x !== "steps" : x !== view; });
    document.querySelectorAll(".q-view-btn").forEach((b) => { b.classList.toggle("active", b.dataset.view === view); b.setAttribute("aria-selected", b.dataset.view === view ? "true" : "false"); });
    $("q-steps-count").textContent = Q.steps.length ? String(Q.steps.length) : "";
  }
  function setView(v) {
    view = VIEWS.includes(v) ? v : "overview";
    pref.set("lab_q_view", view);
    layoutViews();
    if (view === "overview" || view === "shares") refreshPreview(true);
    if (view === "screening") loadScreening();
  }
  // ---- Screening: the ladder's 16 contrasts, verdict per switch, candidate (backend/screening.py) ----------------------
  function scrParams(extra) {
    const p = new URLSearchParams({ experiment: expName(), base: $("scr-base").value.trim() || "R2 B2", all: $("scr-all").value.trim() || "R2 ALL" });
    if (Q.queue_id) p.set("queue_id", Q.queue_id);
    if ($("scr-commits").value.trim()) p.set("exclude_commits", $("scr-commits").value.trim());
    if ($("scr-null").checked) p.set("include_null", "true");
    Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v));
    return p.toString();
  }
  const pct = (x) => (x == null ? "–" : `${Math.round(100 * x)}%`);
  const pfmt = (x) => (x == null ? "–" : x < 0.0001 ? "<0.0001" : x.toFixed(4));
  async function loadScreening() {
    const out = $("scr-out"), dl = $("scr-dl");
    dl.replaceChildren();
    if (!expName()) { out.replaceChildren(h("p", { class: "hint", text: "Type the experiment name in the bar above: the report is made from the runs recorded under it." })); return; }
    out.replaceChildren(h("p", { class: "hint", text: "Working…" }));
    let rep;
    try {
      const res = await fetch("/api/screening?" + scrParams());
      rep = await res.json();
      if (!res.ok) { out.replaceChildren(h("p", { class: "step-issue", text: rep.error || `The Lab answered ${res.status}.` })); return; }
    } catch (e) { out.replaceChildren(h("p", { class: "step-issue", text: "The Lab did not answer." })); return; }
    [["Markdown", "md"], ["Configurations CSV", "csv-configs"], ["Contrasts CSV", "csv-contrasts"]].forEach(([t, f]) =>
      dl.append(h("a", { class: "button-link small", href: "/api/screening?" + scrParams({ format: f }), text: t, download: "" })));
    const th = (t, cls) => h("th", { text: t, class: cls || "" });
    const cfg = h("table", { class: "qt" }, h("thead", {}, h("tr", {}, th("Configuration"), th("Cells", "num"), th("Pass", "num"), th("Rate", "num"), th("95% interval"), th("Missing", "num"), th("Worst case"), th(`All ${rep.repeats} repeats pass`))),
      h("tbody", {}, rep.configs.map((c) => h("tr", { class: c.in_design ? "" : "off" },
        h("td", { text: c.label }), h("td", { class: "num", text: String(c.cells) }), h("td", { class: "num", text: String(c.passes) }), h("td", { class: "num", text: pct(c.rate) }),
        h("td", { text: c.wilson ? `${pct(c.wilson[0])} to ${pct(c.wilson[1])}` : "–" }), h("td", { class: "num", text: String(c.missing) }),
        h("td", { text: c.missing ? `${pct(c.worst_low)} to ${pct(c.worst_high)}` : "–" }), h("td", { text: c.problems_full ? `${c.pass_all} of ${c.problems_full}` : "–", title: "Problems with a result for every repeat, and how many of them pass every repeat" })))));
    const verdict = Object.fromEntries(rep.verdicts.map((v) => [v.switch, v]));
    const row = (c) => h("tr", {}, h("td", { text: c.id }), h("td", { text: `${c.treated} vs ${c.control}` }), h("td", { class: "num", text: String(c.pairs) }),
      h("td", { class: "num", text: `${c.treated_only} / ${c.control_only}`, title: "pairs only the first passes / only the second passes" }),
      h("td", { class: "num" + (c.passes_raw ? " sig" : ""), text: pfmt(c.p) }), h("td", { class: "num" + (c.passes_holm ? " sig" : ""), text: c.kind === "all" ? "outside the 16" : pfmt(c.p_holm) }));
    const con = h("table", { class: "qt" }, h("thead", {}, h("tr", {}, th("Contrast"), th("Compared"), th("Pairs", "num"), th("Flipped (for / against)", "num"), th("p (exact)", "num"), th("p Holm (16)", "num"))),
      h("tbody", {}, [...rep.contrasts, rep.all_vs_base].map(row)));
    const ver = h("table", { class: "qt" }, h("thead", {}, h("tr", {}, th("Switch"), th("Verdict"), th("Why"))),
      h("tbody", {}, rep.verdicts.map((v) => h("tr", {}, h("td", { text: v.switch }),
        h("td", {}, h("span", { class: "st " + (v.verdict.startsWith("finding: helps") ? "st-finished" : v.verdict.startsWith("candidate") ? "st-paused" : v.verdict.includes("hurts") ? "st-stopped" : "st-ended"), text: v.verdict })),
        h("td", { text: v.why })))));
    const cand = rep.candidate;
    out.replaceChildren();
    put(out,
      h("p", { class: "hint", text: `${rep.experiment} · ${plural(rep.runs_used, "run")} by ${rep.people.join(", ") || "nobody"} · commits ${rep.commits.join(", ") || "–"} · ${rep.pairs_expected} pairs expected per configuration` +
        (rep.excluded_problems.length ? ` · left out (null agent passes them): ${rep.excluded_problems.join(", ")}` : "") }),
      ...rep.warnings.map((w) => h("div", { class: "warn-box", text: w })),
      h("h3", { text: "Configurations" }), h("div", { class: "scr-scroll" }, cfg),
      h("h3", { text: "Verdict per switch" }), h("div", { class: "scr-scroll" }, ver),
      cand ? h("div", { class: "scr-cand" }, h("strong", { text: "Candidate by the doc 15 rule: " }), cand.note, (cand.switches || []).length ? ` Switches: ${cand.switches.join(", ")}.` : "") : null,
      h("h3", { text: "The 16 contrasts, and ALL against B2" }), h("div", { class: "scr-scroll" }, con),
      h("details", { class: "scr-rule" }, h("summary", { text: "The rule" }), h("p", { text: rep.rule })));
  }
  $("q-share-logs").addEventListener("click", async () => {
    const hint = $("q-log-hint"), btn = $("q-share-logs");
    btn.disabled = true; hint.textContent = "Sharing the logs of your earlier runs…";
    try {
      const res = await fetch("/api/logs/share-existing", { method: "POST" });
      const d = await res.json();
      hint.textContent = res.ok ? `Shared ${plural(d.shared, "log")}; ${d.skipped} skipped. ${d.note}` : (d.error || `The Lab answered ${res.status}.`);
    } catch (e) { hint.textContent = "The Lab did not answer."; }
    btn.disabled = false;
  });
  $("scr-refresh").addEventListener("click", loadScreening);
  document.querySelectorAll(".q-view-btn").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  function setStepsMode(m) {
    stepsMode = m === "cards" ? "cards" : "compact";
    pref.set("lab_q_steps_mode", stepsMode);
    $("q-mode-compact").classList.toggle("active", stepsMode === "compact");
    $("q-mode-cards").classList.toggle("active", stepsMode === "cards");
    renderSteps();
  }
  $("q-mode-compact").addEventListener("click", () => setStepsMode("compact"));
  $("q-mode-cards").addEventListener("click", () => setStepsMode("cards"));

  function renderSteps() {
    $("q-mode-compact").classList.toggle("active", stepsMode === "compact");
    $("q-mode-cards").classList.toggle("active", stepsMode === "cards");
    if (stepsMode === "cards") renderStepCards(); else renderStepRows();
  }

  // One row per step: the 18-step ladder fits on one screen instead of six.
  function renderStepRows() {
    const ol = $("q-steps");
    ol.innerHTML = "";
    if (!Q.steps.length) return;
    const th = (t, cls) => h("th", { text: t, class: cls || "" });
    const tb = h("tbody");
    Q.steps.forEach((s, i) => {
      const sp = stepPreview(s);
      const running = queueRunning() && job.progress && job.progress.step === i + 1 && s.enabled !== false;
      const c = sp ? sp.counts : null, n = Math.max(1, sp ? sp.total : 1);
      const swTitle = (s.agent_switches || []).join("+") || "no switches";
      const open = expandedSteps.has(s.step_id);
      tb.append(h("tr", { class: (s.enabled === false ? "off " : "") + (running ? "running" : ""), "data-step": s.step_id },
        h("td", { class: "n", text: String(i + 1) }),
        h("td", {}, h("div", { class: "nm", text: s.name, title: swTitle }),
          h("div", { class: "sub", text: `${s.agent_label || s.agent_id} · ${String(s.model_id || "").replace(/^gemini\//, "")}${s.run_class === "frozen" ? " · frozen campaign" : ""}`, title: swTitle })),
        h("td", { class: "num", text: `${s.problems.length} × ${s.repeat_n}`, title: `${plural(s.problems.length, "problem")} × ${plural(s.repeat_n, "repeat")} = ${plural(s.problems.length * s.repeat_n, "run")}` }),
        h("td", { class: "bar-cell" }, sp ? h("div", { class: "bar", role: "img", "aria-label": `${c.pass} passed, ${c.fail} failed, ${c.incomplete} incomplete of ${sp.total}` },
          h("span", { class: "b-pass", style: `width:${(100 * c.pass) / n}%` }), h("span", { class: "b-fail", style: `width:${(100 * c.fail) / n}%` }),
          h("span", { class: "b-inc", style: `width:${(100 * c.incomplete) / n}%` })) : "—"),
        h("td", { class: "num", text: sp ? `${c.pass + c.fail}/${sp.total}` : "—", title: "done / total" }),
        h("td", { class: "num pp-ok", text: sp ? String(c.pass) : "" }),
        h("td", { class: "num pp-bad", text: sp ? String(c.fail) : "" }),
        h("td", { class: "num", text: sp ? (s.enabled === false ? "0" : String(sp.todo)) : "", title: "cells still to run" }),
        h("td", {}, h("label", { class: "switch", title: "Switched-off steps are skipped when the queue runs" },
          h("input", { type: "checkbox", checked: s.enabled !== false, onchange: (e) => { s.enabled = e.target.checked; markDirty(); renderSteps(); } }), "On")),
        h("td", { class: "acts" },
          h("button", { class: "ghost small exp-btn", type: "button", text: open ? "▾" : "▸", title: "Show every problem and repeat", "aria-expanded": open ? "true" : "false",
            onclick: () => { if (open) expandedSteps.delete(s.step_id); else expandedSteps.add(s.step_id); renderSteps(); } }),
          h("button", { class: "ghost small", type: "button", text: "Edit", onclick: () => openStepDialog(i) }),
          h("button", { class: "ghost small", type: "button", text: "Copy", title: "Duplicate this step", onclick: () => duplicateStep(i) }),
          h("button", { class: "ghost small icon", type: "button", text: "↑", "aria-label": "Move up", disabled: i === 0, onclick: () => moveStep(i, -1) }),
          h("button", { class: "ghost small icon", type: "button", text: "↓", "aria-label": "Move down", disabled: i === Q.steps.length - 1, onclick: () => moveStep(i, 1) }),
          h("button", { class: "ghost small danger", type: "button", text: "✕", title: "Remove this step", "aria-label": "Remove", onclick: () => removeStep(i) }))));
      if (sp && sp.issue) tb.append(h("tr", {}, h("td", { colspan: 10, class: "step-issue", text: sp.issue })));
      if (open && sp) tb.append(h("tr", { class: "cells-row" }, h("td", { colspan: 10 }, cellsGrid(s, sp))));
    });
    const t = h("table", { class: "qt" }, h("thead", {}, h("tr", {}, th("#"), th("Step"), th("Problems × repeats"), th("Progress"), th("Done"), th("Pass"), th("Fail"), th("To run"), th("On"), th(""))), tb);
    ol.append(h("li", { class: "q-table-li" }, h("div", { class: "q-table-wrap" }, t)));
  }

  // ---- overview: live numbers for the whole queue under the experiment name --------------------------------------------
  const PALETTE = ["#2b62e8", "#107a52", "#c77700", "#a23ad1", "#d6336c", "#0e8fa8", "#6b7280", "#8a5a2b"];
  const unitOf = (c) => (Q.split === "repeat" ? `${c.problem} #${c.repeat}` : c.problem);
  function overviewData() {
    const out = { total: 0, pass: 0, fail: 0, inc: 0, missing: 0, problems: new Map(), people: new Map(), enabled: [] };
    Q.steps.forEach((s) => {
      if (s.enabled === false) return;
      const sp = stepPreview(s);
      if (!sp) return;
      const si = out.enabled.length;
      out.enabled.push(s);
      sp.cells.forEach((c) => {
        out.total++;
        out[c.status === "pass" ? "pass" : c.status === "fail" ? "fail" : c.status === "incomplete" ? "inc" : "missing"]++;
        let p = out.problems.get(c.problem);
        if (!p) { p = { problem: c.problem, pass: 0, fail: 0, inc: 0, missing: 0, steps: [], by: new Map() }; out.problems.set(c.problem, p); }
        p[c.status === "pass" ? "pass" : c.status === "fail" ? "fail" : c.status === "incomplete" ? "inc" : "missing"]++;
        (p.steps[si] = p.steps[si] || { name: s.name, pass: 0, fail: 0, inc: 0, missing: 0 })[c.status === "pass" ? "pass" : c.status === "fail" ? "fail" : c.status === "incomplete" ? "inc" : "missing"]++;
        if (c.by) {
          p.by.set(c.by, (p.by.get(c.by) || 0) + 1);
          let q = out.people.get(c.by);
          if (!q) { q = { name: c.by, cells: 0, pass: 0, fail: 0, inc: 0, last: "" }; out.people.set(c.by, q); }
          q.cells++; if (c.status === "pass") q.pass++; else if (c.status === "fail") q.fail++; else if (c.status === "incomplete") q.inc++;
          if ((c.at || "") > q.last) q.last = c.at;
        }
      });
    });
    return out;
  }
  const tile = (k, v, s, cls, bar) => h("div", { class: "ov-tile " + (cls || "") }, h("div", { class: "k", text: k }), h("div", { class: "v" + (String(v).length > 9 ? " long" : ""), text: v }), s ? h("div", { class: "s", text: s }) : null, bar || null);
  function renderOverview() {
    const tiles = $("ov-tiles"), note = $("ov-note");
    if (!Q.steps.length) { tiles.innerHTML = ""; return; }
    if (!preview) { tiles.innerHTML = ""; note.hidden = false; note.textContent = "Loading the numbers…"; return; }
    if (preview.needs_name) {
      tiles.innerHTML = ""; $("ov-problems").innerHTML = ""; $("ov-people").innerHTML = ""; $("ov-activity").innerHTML = "";
      note.hidden = false; note.className = "hint warn-text";
      note.textContent = "Type the experiment name in the bar above. Progress is counted per experiment name, so with no name there is nothing to show.";
      return;
    }
    note.hidden = true;
    const d = overviewData(), done = d.pass + d.fail, left = d.total - done;
    const act = preview.activity || { per_hour: 0, attempts: 0, buckets: [], by_person: {}, hours: 24 };
    const sharesRunning = (preview.shares || []).filter((sh) => sh.state === "running").length;
    const li = preview.label_info || { runs: 0 };
    const yieldPerAttempt = li.runs ? Math.min(1, done / li.runs) : 1;       // cells with a result per recorded attempt
    const cellsPerHour = act.per_hour * yieldPerAttempt;
    const eta = !left ? "done" : cellsPerHour > 0 ? fmtDur((left / cellsPerHour) * 3600) : "no recent runs";
    const bar = h("div", { class: "bar" }, h("span", { class: "b-pass", style: `width:${100 * d.pass / Math.max(1, d.total)}%` }), h("span", { class: "b-fail", style: `width:${100 * d.fail / Math.max(1, d.total)}%` }), h("span", { class: "b-inc", style: `width:${100 * d.inc / Math.max(1, d.total)}%` }));
    tiles.replaceChildren(
      tile("Cells with a result", `${done} / ${d.total}`, `${d.total ? Math.round(100 * done / d.total) : 0}% of the queue`, "", bar),
      tile("Passed", String(d.pass), done ? `${Math.round(100 * d.pass / done)}% of cells with a result` : "", "ok"),
      tile("Failed", String(d.fail), "", "bad"),
      tile("Incomplete", String(d.inc), "ran, no outcome; run again", d.inc ? "warn" : ""),
      tile("Left to run", String(left), `${d.missing} not started`),
      tile("Pace", act.per_hour ? `${act.per_hour} / h` : "—", `${act.attempts} attempts in the last ${act.hours} h, all machines`),
      tile("Time left (estimate)", eta, "from the pace above; stops and failures make it longer"),
      tile("Machines running", `${sharesRunning}${preview.machines > 1 ? " / " + preview.machines : ""}`, preview.machines > 1 ? "shares with a live claim" : "one machine"));
    renderOvProblems(d);
    renderOvPeople(d);
    renderOvActivity(act);
  }
  function heatClass(st) {
    const n = st.pass + st.fail + st.inc + st.missing, done = st.pass + st.fail;
    if (st.missing === n) return "";
    let base = st.pass && st.fail ? "mix" : st.pass ? "pass" : st.fail ? "fail" : "inc";
    if (!done && st.inc) base = "inc";
    return base + (st.missing ? " part" : "");
  }
  function renderOvProblems(d) {
    const rows = [...d.problems.values()].map((p) => {
      const done = p.pass + p.fail, n = done + p.inc + p.missing;
      const heat = h("div", { class: "heat" });
      d.enabled.forEach((s, si) => {
        const st = p.steps[si] || { name: s.name, pass: 0, fail: 0, inc: 0, missing: 0 };
        heat.append(h("i", { class: heatClass(st), title: `${st.name}: ${st.pass} passed, ${st.fail} failed, ${st.inc} incomplete, ${st.missing} not started` }));
      });
      const chips = [...p.by.entries()].sort((a, b) => b[1] - a[1]).map(([name, k]) => h("span", { class: "who-chip", text: `${name} ${k}`, title: `${name} ran ${plural(k, "cell")} of this problem` }));
      return h("tr", {},
        h("td", { class: "pn", text: p.problem }),
        h("td", { class: "num", text: `${done}/${n}` }),
        h("td", { class: "num " + (done && !p.pass ? "pp-bad" : ""), text: done ? `${Math.round(100 * p.pass / done)}%` : "—" }),
        h("td", {}, heat),
        h("td", {}, chips.length ? chips : h("span", { class: "hint", text: "nobody yet" })));
    });
    $("ov-prob-hint").textContent = `${rows.length} problems × ${d.enabled.length} steps; each square is one step (all repeats of the problem)`;
    $("ov-problems").replaceChildren(h("table", { class: "ov" }, h("thead", {}, h("tr", {}, ...["Problem", "Done", "Pass", "Steps", "Who ran it"].map((t) => h("th", { text: t })))), h("tbody", {}, rows)),
      h("div", { class: "legend" }, h("span", { text: "Faded square = some repeats not run yet." }), ...[["pass", "passed"], ["mix", "mixed"], ["fail", "failed"], ["inc", "incomplete"]].map(([c, t]) => h("span", {}, h("i", { class: "heat-key", style: `background:${{ pass: "var(--ok)", fail: "var(--bad)", mix: "linear-gradient(135deg,var(--ok) 50%,var(--bad) 50%)", inc: "#e3b04b" }[c]}` }), t))));
  }
  function renderOvPeople(d) {
    const shares = preview.shares || [];
    const rows = new Map(d.people);
    shares.forEach((sh) => {
      (sh.assigned || []).forEach((n) => { if (!rows.has(n)) rows.set(n, { name: n, cells: 0, pass: 0, fail: 0, inc: 0, last: "" }); });
      if (sh.claim && sh.claim.run_by && !rows.has(sh.claim.run_by)) rows.set(sh.claim.run_by, { name: sh.claim.run_by, cells: 0, pass: 0, fail: 0, inc: 0, last: "" });
    });
    if (!rows.size) { $("ov-people").replaceChildren(h("p", { class: "hint", text: "Nobody has recorded a run under this name yet." })); return; }
    const body = [...rows.values()].sort((a, b) => b.cells - a.cells).map((p) => {
      const low = p.name.toLowerCase();
      const mine = shares.filter((sh) => (sh.assigned || []).some((x) => x.toLowerCase() === low)).map((sh) => sh.share);
      const live = shares.find((sh) => sh.claim && (sh.claim.run_by || "").toLowerCase() === low && (sh.state === "running" || sh.state === "silent"));
      const now = live ? (live.state === "running" ? (live.claim.now_problem || "running") : "silent " + ago(live.claim.updated_at)) : (p.last ? "last " + ago(p.last) : "—");
      return h("tr", {}, h("td", { text: p.name }), h("td", { class: "num", text: mine.length ? mine.join(", ") : "—", title: "shares assigned to this person" }),
        h("td", { class: "num", text: String(p.cells) }), h("td", { class: "num pp-ok", text: String(p.pass) }), h("td", { class: "num pp-bad", text: String(p.fail) }),
        h("td", { text: now, title: now }));
    });
    $("ov-people").replaceChildren(h("table", { class: "ov" }, h("thead", {}, h("tr", {}, ...["Person", "Share", "Cells", "Pass", "Fail", "Now"].map((t) => h("th", { text: t })))), h("tbody", {}, body)));
  }
  function renderOvActivity(act) {
    const box = $("ov-activity");
    const names = Object.keys(act.by_person || {});
    const W = 560, H = 150, padL = 26, padB = 20, nB = act.hours || 24, bw = (W - padL) / nB;
    const maxV = Math.max(1, ...act.buckets);
    const ns = "http://www.w3.org/2000/svg";
    const el = (t, a) => { const e = document.createElementNS(ns, t); Object.entries(a || {}).forEach(([k, v]) => e.setAttribute(k, v)); return e; };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "act-svg", role: "img", "aria-label": `Attempts per hour over the last ${nB} hours: ${act.attempts} in all` });
    [0, 0.5, 1].forEach((f) => { const y = (H - padB) - f * (H - padB - 8); svg.append(el("line", { x1: padL, x2: W, y1: y, y2: y, stroke: "currentColor", "stroke-opacity": ".12" })); const t = el("text", { x: 2, y: y + 3 }); t.textContent = String(Math.round(maxV * f)); svg.append(t); });
    const base = new Array(nB).fill(0);
    names.forEach((nm, ni) => {
      (act.by_person[nm] || []).forEach((v, i) => {
        if (!v) return;
        const hgt = (v / maxV) * (H - padB - 8), y = (H - padB) - base[i] * ((H - padB - 8) / maxV) - hgt;
        const r = el("rect", { x: padL + i * bw + 1, y, width: Math.max(1, bw - 2), height: hgt, fill: PALETTE[ni % PALETTE.length], rx: 1.5 });
        const tt = el("title"); tt.textContent = `${nm}: ${v} attempt${v === 1 ? "" : "s"}, ${nB - 1 - i} h ago`; r.append(tt); svg.append(r);
        base[i] += v;
      });
    });
    [[0, `-${nB} h`], [Math.floor(nB / 2), `-${Math.floor(nB / 2)} h`], [nB - 1, "now"]].forEach(([i, t]) => { const x = el("text", { x: padL + i * bw, y: H - 5 }); x.textContent = t; svg.append(x); });
    const parts = [act.attempts ? svg : h("p", { class: "hint", text: "No attempts recorded in the last 24 hours." })];
    if (names.length) parts.push(h("div", { class: "legend" }, ...names.map((nm, ni) => h("span", {}, h("i", { style: `background:${PALETTE[ni % PALETTE.length]}` }), nm))));
    box.replaceChildren(...parts);
  }

  // ---- shares & people: board with assign / split / move ----------------------------------------------------------------
  function knownPeople() {
    const set = new Set();
    ((preview && preview.shares) || []).forEach((sh) => { (sh.people || []).forEach((p) => set.add(p.name)); if (sh.claim && sh.claim.run_by) set.add(sh.claim.run_by); (sh.assigned || []).forEach((n) => set.add(n)); });
    ((preview && preview.label_info && preview.label_info.people) || []).forEach((n) => set.add(n));
    if (who()) set.add(who());
    set.delete("unknown");
    return [...set].sort((a, b) => a.localeCompare(b));
  }
  function undoneOf(sh) {
    const mineUnits = new Set(sh.problems), out = new Set();
    Q.steps.forEach((s) => {
      if (s.enabled === false) return;
      const sp = stepPreview(s);
      if (!sp) return;
      sp.cells.forEach((c) => { if (mineUnits.has(unitOf(c)) && (c.status === "missing" || c.status === "incomplete")) out.add(unitOf(c)); });
    });
    return sh.problems.filter((u) => out.has(u));
  }
  const put = (parent, ...kids) => { kids.flat().forEach((c) => { if (c != null && c !== false) parent.append(c); }); return parent; };
  function modal(build) {
    const d = h("dialog", { class: "q-modal" });
    document.body.append(d);
    d.addEventListener("close", () => d.remove());
    d.addEventListener("click", (e) => { if (e.target === d) d.close(); });
    build(d, () => d.close());
    d.showModal();
    return d;
  }
  async function needSaved() {
    if (!Q.queue_id) { alert("Save the queue first (Save button above), then change the shares."); return false; }
    if (dirty && !(await saveQueue(true))) return false;
    return true;
  }
  async function applyShareChange(url, payload, d, close) {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    let data = {};
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) return { ok: false, error: data.error || `The Lab answered ${res.status}.` };
    if (data.queue) { Q.shares = data.queue.shares || null; Q.machines = data.queue.machines || Q.machines; dirty = false; await loadSaved(); renderAll(); }
    refreshPreview(true);
    return { ok: true, data };
  }
  function openAssign(sh) {
    modal((d, close) => {
      const sel = new Set((sh.assigned || []).map((x) => x.toLowerCase()));
      const boxes = knownPeople().map((nm) => h("label", {}, h("input", { type: "checkbox", value: nm, checked: sel.has(nm.toLowerCase()) }), nm));
      const extra = h("input", { type: "text", placeholder: "Another name, or several separated by commas", "aria-label": "Another name" });
      const err = h("p", { class: "step-issue", hidden: true });
      put(d, h("h3", { text: `Share ${sh.share}: who runs it` }),
        h("p", { text: "A plan, not a lock. The board and the Run dialog warn when someone else starts this share." }),
        h("div", { class: "pick-list" }, boxes.length ? boxes : h("span", { class: "hint", text: "No names known yet; type one below." })), extra, err,
        h("div", { class: "row-end" },
          h("button", { class: "ghost", type: "button", text: "Clear", onclick: () => save([]) }),
          h("button", { class: "secondary", type: "button", text: "Cancel", onclick: close }),
          h("button", { class: "primary", type: "button", text: "Save", onclick: () => save(null) })));
      async function save(forced) {
        if (!(await needSaved())) return;
        let names = forced;
        if (names === null) {
          names = Array.from(d.querySelectorAll('input[type="checkbox"]:checked')).map((x) => x.value);
          extra.value.split(",").map((x) => x.trim()).filter(Boolean).forEach((x) => { if (!names.some((y) => y.toLowerCase() === x.toLowerCase())) names.push(x); });
        }
        const r = await applyShareChange(`/api/queue/${encodeURIComponent(Q.queue_id)}/assign`, { share: sh.share, names, run_by: who() || null });
        if (!r.ok) { err.hidden = false; err.textContent = r.error; return; }
        close();
      }
    });
  }
  function resultBox(d, close, data) {
    d.replaceChildren(h("h3", { text: "Done" }),
      h("p", { text: `${plural((data.moved || []).length, "problem")} moved: ` + (data.moved || []).map((m) => `${m.unit} → share ${m.to}`).join(", ") }),
      ...(data.warnings || []).map((w) => h("div", { class: "warn-box", text: w })),
      h("p", { text: data.note || "" }),
      h("div", { class: "row-end" }, h("button", { class: "primary", type: "button", text: "Close", onclick: close })));
  }
  // Runs left per share unit (a problem, or "problem #r"): one run per enabled step that still has a result to get.
  function unitLeftMap() {
    const m = new Map();
    Q.steps.forEach((s) => {
      if (s.enabled === false) return;
      const sp = stepPreview(s);
      if (!sp) return;
      sp.cells.forEach((c) => { if (c.status === "missing" || c.status === "incomplete") m.set(unitOf(c), (m.get(unitOf(c)) || 0) + 1); });
    });
    return m;
  }
  function openRedistribute(sh) {
    const shares = (preview && preview.shares) || [];
    const m = unitLeftMap();
    const mine = sh.problems.filter((u) => m.has(u));
    const others = shares.filter((x) => x.share !== sh.share);
    const base = new Map(shares.map((x) => [x.share, x.problems.reduce((a, u) => a + (m.get(u) || 0), 0)]));
    const myRuns = base.get(sh.share) || 0;
    const tag = (x) => (x.assigned || []).length ? ` · ${x.assigned.join(", ")}` : "";
    modal((d, close) => {
      d.classList.add("wide");
      const err = h("p", { class: "step-issue", hidden: true });
      const showErr = (t) => { err.hidden = !t; err.textContent = t || ""; };
      const warn = [];
      if (!expName()) warn.push(h("div", { class: "warn-box", text: "Type the experiment name in the bar above first: what is left is counted under it." }));
      if (sh.claim && sh.state === "running") warn.push(h("div", { class: "warn-box", text: `${sh.claim.run_by || "Someone"} is running this share now. They keep their old share until they press Continue; ask them to pause first.` }));
      // 1. where the work may go
      const boxes = others.map((x) => h("label", {}, h("input", { type: "checkbox", value: String(x.share), checked: true }),
        h("span", { class: "grow", text: `Share ${x.share}${tag(x)}` }), h("span", { class: "hint", text: `${stateText(x).split(" · ")[0]} · ${base.get(x.share) || 0} runs left` })));
      const keep = h("input", { type: "checkbox", id: "rd-keep" });
      const setTargets = (fn) => d.querySelectorAll(".rd-targets input[type=checkbox]").forEach((c) => { c.checked = fn(others.find((x) => String(x.share) === c.value)); });
      // 2. each problem: stay or go to a share
      const sels = new Map();
      const rows = mine.map((u) => {
        const sel = h("select", { "aria-label": `Where ${u} goes` }, h("option", { value: "0", text: `Stay in share ${sh.share}` }),
          ...others.map((x) => h("option", { value: String(x.share), text: `Share ${x.share}${tag(x)}` })));
        sel.addEventListener("change", refresh);
        sels.set(u, sel);
        return h("tr", {}, h("td", { text: u }), h("td", { class: "num", text: String(m.get(u)) }), h("td", {}, sel));
      });
      // 3. what it adds up to
      const sumBody = h("tbody");
      function chosen() { const out = new Map(); sels.forEach((sel, u) => { const v = Number(sel.value); if (v) out.set(u, v); }); return out; }
      function refresh() {
        const mv = chosen(), after = new Map(base);
        mv.forEach((to, u) => { after.set(sh.share, after.get(sh.share) - m.get(u)); after.set(to, (after.get(to) || 0) + m.get(u)); });
        sumBody.replaceChildren(...shares.map((x) => {
          const was = base.get(x.share) || 0, now = after.get(x.share) || 0, diff = now - was;
          return h("tr", { class: diff ? "chg" : "" }, h("td", { text: `Share ${x.share}${tag(x)}` }), h("td", { class: "num", text: String(was) }),
            h("td", { class: "num", text: String(now) }), h("td", { class: "num", text: diff ? (diff > 0 ? `+${diff}` : String(diff)) : "" }));
        }));
        go.disabled = !mv.size;
        go.textContent = mv.size ? `Apply: move ${plural(mv.size, "problem")} (${[...mv.keys()].reduce((a, u) => a + m.get(u), 0)} runs)` : "Apply";
      }
      function auto() {
        showErr("");
        const to = Array.from(d.querySelectorAll(".rd-targets input:checked")).map((x) => Number(x.value));
        if (keep.checked) to.push(sh.share);
        if (!to.length) { showErr("Tick at least one share to move work to."); return; }
        const load = new Map(to.map((t) => [t, t === sh.share ? 0 : (base.get(t) || 0)]));
        sels.forEach((sel) => { sel.value = "0"; });
        [...mine].sort((a, b) => m.get(b) - m.get(a)).forEach((u) => {
          const t = to.reduce((best, k) => (load.get(k) < load.get(best) || (load.get(k) === load.get(best) && k < best) ? k : best), to[0]);
          load.set(t, load.get(t) + m.get(u));
          sels.get(u).value = t === sh.share ? "0" : String(t);
        });
        refresh();
      }
      const go = h("button", { class: "primary", type: "button", text: "Apply", disabled: true, onclick: apply });
      put(d, h("h3", { text: `Redistribute share ${sh.share}` }),
        h("p", { text: mine.length
          ? `${plural(mine.length, Q.split === "repeat" ? "problem × repeat pair" : "problem")} not finished yet, ${myRuns} runs left. One of these counts all its runs (one per enabled step), so the numbers below are runs, not problems. Choose where each one goes, or let the Lab balance by runs. Finished problems stay in share ${sh.share}, so the record of who ran what does not change.`
          : `Share ${sh.share} has nothing left to move.` }),
        ...warn,
        h("h4", { text: "1. Shares that may take work" }),
        h("div", { class: "pick-list rd-targets" }, boxes),
        h("div", { class: "rd-quick" },
          h("button", { class: "ghost small", type: "button", text: "All", onclick: () => setTargets(() => true) }),
          h("button", { class: "ghost small", type: "button", text: "None", onclick: () => setTargets(() => false) }),
          h("button", { class: "ghost small", type: "button", text: "Not started or finished only", title: "Leave out shares somebody is running right now", onclick: () => setTargets((x) => x.state === "not taken" || x.state === "finished") }),
          h("label", { class: "hint" }, keep, " keep some of it here too (level this share as well)"),
          h("button", { class: "secondary small", type: "button", text: "Auto-balance by runs", onclick: auto, disabled: !mine.length }),
          h("button", { class: "ghost small", type: "button", text: "Reset", title: "Everything stays where it is", onclick: () => { sels.forEach((x) => { x.value = "0"; }); showErr(""); refresh(); } })),
        h("h4", { text: "2. Each problem" }),
        h("div", { class: "rd-scroll" }, h("table", { class: "rd-table" }, h("thead", {}, h("tr", {}, h("th", { text: Q.split === "repeat" ? "Problem #repeat" : "Problem" }), h("th", { class: "num", text: "Runs left" }), h("th", { text: "Goes to" }))), h("tbody", {}, rows))),
        h("h4", { text: "3. Runs left per share, before and after" }),
        h("div", { class: "rd-scroll" }, h("table", { class: "rd-table" }, h("thead", {}, h("tr", {}, h("th", { text: "Share" }), h("th", { class: "num", text: "Now" }), h("th", { class: "num", text: "After" }), h("th", { class: "num", text: "Change" }))), sumBody)),
        err,
        h("div", { class: "row-end" }, h("button", { class: "secondary", type: "button", text: "Cancel", onclick: close }), go));
      refresh();
      async function apply() {
        showErr("");
        if (!expName()) { showErr("Type the experiment name in the bar above first: what is left is counted under it."); return; }
        const mv = chosen();
        if (!mv.size) { showErr("Nothing is set to move."); return; }
        if (!(await needSaved())) return;
        const moves = [...mv].map(([unit, to_share]) => ({ unit, to_share }));
        const r = await applyShareChange(`/api/queue/${encodeURIComponent(Q.queue_id)}/reassign`, { experiment: expName(), moves, run_by: who() || null });
        if (!r.ok) { showErr(r.error); return; }
        resultBox(d, close, r.data);
      }
    });
  }
  function buildBoard(panel, shares, k, keepTop, keepLeft) {
    const count = (st) => shares.filter((sh) => sh.state === st).length;
    const bits = [[count("running"), "running"], [count("finished"), "finished"], [count("paused") + count("stopped") + count("ended"), "stopped or paused"],
                  [count("silent"), "silent"], [count("not taken"), "not taken"]].filter(([x]) => x).map(([x, w]) => `${x} ${w}`);
    const th = (t) => h("th", { text: t });
    const tb = h("tbody");
    shares.forEach((sh) => {
      const c = sh.claim;
      const taken = c ? `${c.run_by || "?"}${c.machine ? " · " + c.machine : ""}` : (sh.people.length ? sh.people.map((x) => x.name).join(", ") : "nobody yet");
      const status = stateText(sh), plist = sh.problems.join(", "), asg = sh.assigned || [];
      tb.append(h("tr", { class: sh.share === k ? "me" : "" },
        h("td", { text: `${sh.share}${sh.share === k ? " · you" : ""}` }),
        h("td", { class: "assigned" + (asg.length ? "" : " none"), text: asg.length ? asg.join(", ") : "not assigned" }),
        h("td", { text: taken, title: taken + (c && c.started_at ? `\nsince ${fmtWhen(c.started_at)}` : "") }),
        h("td", {}, h("span", { class: "st st-" + sh.state.replace(" ", "-"), text: status, title: status })),
        h("td", { text: `${sh.done} of ${sh.total}` }),
        h("td", { text: `${sh.passed} / ${sh.failed}` }),
        h("td", { text: String(sh.left) }),
        h("td", { class: "probs" }, h("div", { class: "clamp", text: plist, title: plist })),
        h("td", { class: "acts" },
          h("button", { class: "secondary small", type: "button", text: "Assign", title: "Plan who runs this share", onclick: () => openAssign(sh) }),
          h("button", { class: "secondary small", type: "button", text: "Redistribute…", title: "Choose where the unfinished problems of this share go, or let the Lab balance them by runs", disabled: !sh.left, onclick: () => openRedistribute(sh) }))));
    });
    const t = h("table", {}, h("thead", {}, h("tr", {}, th("Share"), th("Assigned to"), th("Taken by"), th("Status"), th("Done"), th("Pass / fail"), th("Left"), th(Q.split === "repeat" ? "Problem #repeat" : "Problems"), th(""))), tb);
    const scroll = h("div", { class: "board-scroll" }, t);
    panel.append(h("p", { class: "board-head", text: `${bits.join(" · ") || "no shares"} · ${shares.reduce((x, sh) => x + sh.left, 0)} runs left in all` }), scroll);
    scroll.scrollTop = keepTop; scroll.scrollLeft = keepLeft;
  }

  function renderTotals() {
    const on = Q.steps.filter((s) => s.enabled !== false);
    const total = preview ? preview.total_runs : on.reduce((a, s) => a + s.problems.length * s.repeat_n, 0);
    const left = preview ? preview.runs_left : total;
    const k = myShare();
    $("q-total-line").textContent = !Q.steps.length ? "No steps yet"
      : `${plural(on.length, "step")} · ${plural(total, "run")} · ${left} left` + (k ? ` in share ${k} of ${Q.machines}` : "");
    $("q-total-sub").textContent = k && preview ? `Whole queue: ${preview.all_total_runs} runs, ${preview.all_runs_left} left across all machines.` : "";
    const eta = $("q-eta");
    if (eta) eta.textContent = left ? "≈ " + fmtDur(left * minPerRun() * 60) : "nothing left to run";
    const busy = jobBusy();
    const named = !!expName();
    $("q-run").disabled = busy || !on.length || !named || (!!preview && left === 0);
    $("q-run").title = busy ? "Another job is running" : (!named ? "Give the experiment a name first" : (preview && left === 0 ? "Every cell already has a result under this name" : ""));
    $("q-stop").disabled = !busy;
    const hasRuns = !!preview && ((preview.experiments || []).some((e) => e.name === expName()));
    const rs = $("q-restart-share"), k2 = myShare();
    rs.hidden = !((Q.machines || 1) > 1 && k2);
    const shRuns = preview && preview.shares && preview.shares[k2 - 1];
    const shHas = !!shRuns && (shRuns.done > 0 || (shRuns.people || []).length > 0);
    rs.disabled = busy || !on.length || !named || !hasRuns || !shHas;
    rs.title = busy ? "Another job is running" : (!named ? "Give the experiment a name first" : (!shHas ? `Nothing is recorded in share ${k2} under this name yet; use Run queue` : `Start only share ${k2} again. The other shares keep their results.`));
    $("q-restart").disabled = busy || !on.length || !named || !hasRuns;
    $("q-restart").title = busy ? "Another job is running" : (!named ? "Give the experiment a name first" : (!hasRuns ? "Nothing is recorded under this name yet; use Run queue" : "Start the whole queue again from step 1 as a new round of this experiment"));
  }

  function moveStep(i, d) { const j = i + d; if (j < 0 || j >= Q.steps.length) return; [Q.steps[i], Q.steps[j]] = [Q.steps[j], Q.steps[i]]; markDirty(); renderSteps(); }
  function removeStep(i) {
    if (!confirm(`Remove "${Q.steps[i].name}" from this queue? Results already recorded are kept.`)) return;
    Q.steps.splice(i, 1); markDirty(); renderAll();
  }
  function duplicateStep(i) {
    const copy = JSON.parse(JSON.stringify(Q.steps[i]));
    copy.step_id = uid(); if (!copy.auto_name) copy.name = copy.name + " (copy)";
    Q.steps.splice(i + 1, 0, copy); markDirty(); renderAll();
    openStepDialog(i + 1);
  }

  // ---- step dialog ----------------------------------------------------------------------------
  const dlg = $("step-dialog");
  const D = { setIdx: 0, selected: new Set(), custom: "", variants: new Set() };
  const agentOf = () => CONFIG.agents[Number($("sd-agent").value)];
  const currentSet = () => CONFIG.problem_sets[D.setIdx];
  const isFrozen = () => document.querySelector('input[name="sd-class"]:checked').value === "frozen";
  const chosenManifest = () => manifests.find((m) => m.manifest_id === $("sd-manifest").value);

  function fillSets(selectId) {
    const sel = $(selectId);
    const keep = sel.value;
    sel.innerHTML = "";
    CONFIG.problem_sets.forEach((s, i) => sel.append(h("option", { value: i, text: s.label })));
    if (keep !== "" && Number(keep) < CONFIG.problem_sets.length) sel.value = keep;
  }
  function selectedProblems() {
    if (currentSet().id === "custom") return D.custom.split(",").map((x) => x.trim()).filter(Boolean);
    return currentSet().problems.map((p) => p.id).filter((id) => D.selected.has(id));
  }
  function renderPick() {
    const set = currentSet(), custom = set.id === "custom";
    $("sd-custom-wrap").hidden = !custom;
    $("sd-pick-toggle").hidden = custom;
    if (custom) $("sd-pick-panel").hidden = true;
    const n = selectedProblems().length;
    $("sd-pick-count").textContent = custom ? String(n) : `${n} of ${set.problems.length}`;
    const groups = $("sd-pick-groups");
    groups.innerHTML = "";
    renderCats(set, custom);
    renderSetNote(set);
    if (custom) return;
    const q = $("sd-pick-filter").value.trim().toLowerCase();
    const by = new Map();
    set.problems.forEach((p) => {
      if (q && !(p.id.toLowerCase().includes(q) || p.category.toLowerCase().includes(q))) return;
      if (!by.has(p.category)) by.set(p.category, []);
      by.get(p.category).push(p);
    });
    by.forEach((ps, cat) => {
      const fs = h("fieldset", { class: "problem-group" }, h("legend", { text: `${cat} (${ps.length})` }));
      ps.forEach((p) => {
        const cb = h("input", { type: "checkbox", value: p.id, checked: D.selected.has(p.id), onchange: (e) => {
          if (e.target.checked) D.selected.add(p.id); else D.selected.delete(p.id);
          $("sd-pick-count").textContent = `${selectedProblems().length} of ${set.problems.length}`;
        } });
        const lab = h("label", {}, cb, h("span", { class: "problem-n", text: p.n }), h("span", { text: p.id }));
        if (p.difficulty) lab.append(h("span", { class: "problem-diff diff-" + p.difficulty, text: p.difficulty }));
        fs.append(lab);
      });
      groups.append(fs);
    });
    if (!by.size) groups.append(h("p", { class: "hint", text: "No problem matches this filter." }));
  }
  // One chip per fault category in the chosen set: click to add the whole category to the selection, click again
  // to remove it. "None" then a chip gives a single category on its own.
  function renderCats(set, custom) {
    const box = $("sd-cats");
    box.innerHTML = "";
    box.hidden = custom || pickLocked();
    if (box.hidden) return;
    const by = new Map();
    set.problems.forEach((p) => { if (!by.has(p.category)) by.set(p.category, []); by.get(p.category).push(p.id); });
    if (by.size < 2) { box.hidden = true; return; }
    box.append(h("span", { class: "tag", text: "By category:" }));
    by.forEach((ids, cat) => {
      const on = ids.filter((id) => D.selected.has(id)).length;
      const state = on === ids.length ? "on" : on ? "part" : "";
      box.append(h("button", { type: "button", class: "chip pick " + state, title: on === ids.length ? "remove this category from the selection" : "add this category to the selection",
        onclick: () => { ids.forEach((id) => (on === ids.length ? D.selected.delete(id) : D.selected.add(id))); renderPick(); } },
        cat, h("span", { class: "chip-n", text: on === ids.length || !on ? String(ids.length) : `${on}/${ids.length}` })));
    });
    box.append(h("button", { type: "button", class: "ghost small", text: "Clear", onclick: () => { D.selected.clear(); renderPick(); } }));
  }
  const pickLocked = () => isFrozen() && !!chosenManifest();
  function renderSetNote(set) {
    const n = $("sd-set-note");
    const pin = set.pin;
    n.hidden = !pin;
    if (!pin) return;
    n.className = "hint set-note " + (pin.status === "ok" ? "pin-" + pin.role : "pin-bad");
    n.textContent = pin.status !== "ok"
      ? (pin.status === "changed" ? "This file no longer matches what was pinned. Do not measure with it until the change is deliberate and re-pinned." : "The pinned file is missing.")
      : pin.role === "frozen"
        ? "Pinned frozen set. Measure it once per configuration, in a frozen campaign. Outside a campaign these runs count as experiments, and failures here must not be read to choose agent changes."
        : "Pinned dev set. Run it freely, read the failures and tune on it. It does not overlap the frozen set.";
  }
  function chooseSet(idx, keepSelection) {
    D.setIdx = idx; $("sd-set").value = idx;
    if (!keepSelection) D.selected = new Set(currentSet().problems.map((p) => p.id));
    renderPick();
  }

  function lockFromManifest() {
    const frozen = isFrozen();
    $("sd-frozen-box").hidden = !frozen;
    const sel = $("sd-manifest");
    sel.innerHTML = "";
    if (frozen) {
      if (!manifests.length) sel.append(h("option", { value: "", text: "No frozen campaigns yet. Freeze one in the Campaigns tab first." }));
      manifests.forEach((m) => sel.append(h("option", { value: m.manifest_id,
        text: `${m.name} · ${m.model_id} · ${m.action_space || "-"} · ${m.problems.length} problems × ${m.repeat_n}` })));
      if (dlg.dataset.manifest) sel.value = dlg.dataset.manifest;
    }
    const m = frozen ? chosenManifest() : null;
    if (m) {
      const idx = CONFIG.agents.findIndex((a) => a.id === m.agent_id && (a.action_space || "") === (m.action_space || ""));
      if (idx >= 0) $("sd-agent").value = idx;
      $("sd-model").value = m.model_id; $("sd-judge").value = m.judge_model_id; $("sd-label").value = m.config_label;
      $("sd-repeat").value = m.repeat_n;
      const customIdx = CONFIG.problem_sets.findIndex((s) => s.id === "custom");
      D.custom = m.problems.join(", "); $("sd-custom").value = D.custom;
      chooseSet(customIdx, true);
      if (!$("sd-name").value) $("sd-name").placeholder = m.name;
    }
    ["sd-agent", "sd-model", "sd-judge", "sd-label", "sd-repeat", "sd-set", "sd-custom", "sd-pick-toggle", "sd-save-set-btn"].forEach((id) => { $(id).disabled = !!m; });
    $("sd-locked").hidden = !m;
    $("sd-agent-desc").textContent = agentOf() && agentOf().desc ? agentOf().desc : "";
    $("sd-ok").disabled = frozen && !m;
    syncVariantBox();
    $("sd-purpose").disabled = frozen;
    $("sd-purpose-note").textContent = frozen
      ? "A frozen campaign is the reported measurement; its results are final when the problems are the pinned frozen set."
      : "Automatic: a dev-set problem is dev, anything else is an experiment. You can change the purpose of any run later in Results.";
  }

  function openStepDialog(index, seed) {
    editing = index;
    const s = index == null ? null : Q.steps[index];
    const last = Q.steps[Q.steps.length - 1];
    $("sd-title").textContent = s ? "Edit step" : seed ? "Copy this step for other agents" : "Add step";
    $("sd-ok").textContent = s ? "Save changes" : "Add step";
    $("sd-msg").textContent = "";
    const agents = $("sd-agent");
    agents.innerHTML = "";
    CONFIG.agents.forEach((a, i) => agents.append(h("option", { value: i, text: a.label })));
    fillSets("sd-set");

    const src = s || seed || {};
    let agentIdx = (s || seed) ? CONFIG.agents.findIndex((a) => a.id === src.agent_id && (a.action_space || "") === (src.action_space || "")) : -1;
    if (agentIdx < 0) agentIdx = last ? Math.max(0, CONFIG.agents.findIndex((a) => a.id === last.agent_id && (a.action_space || "") === (last.action_space || ""))) : Number($("agent").value) || 0;
    agents.value = agentIdx;
    $("sd-name").value = seed || src.auto_name ? "" : (src.name || "");
    $("sd-name").placeholder = "e.g. A4 · typed on the frozen 30";
    $("sd-model").value = src.model_id || (last && last.model_id) || $("model").value || (CONFIG.model_presets[0] || "");
    $("sd-judge").value = src.judge_model_id || (last && last.judge_model_id) || $("judge-model").value || (CONFIG.model_presets[0] || "");
    $("sd-label").value = src.config_label !== undefined ? src.config_label : "";
    renderSwitches(src.agent_switches || []);
    D.variants = new Set();
    renderVariants();
    $("sd-repeat").value = src.repeat_n || (last && last.repeat_n) || 1;
    $("sd-missing").checked = src.only_missing !== false;
    $("sd-build").checked = !!src.force_build;
    $("sd-walk").checked = src.record_walkthrough !== false;
    document.querySelector(`input[name="sd-class"][value="${src.run_class || "exploration"}"]`).checked = true;
    $("sd-purpose").value = src.purpose || "";
    dlg.dataset.manifest = src.manifest_id || "";

    // problems: start from the first real set, or the step's own list
    const sets = CONFIG.problem_sets;
    const wanted = src.problems || null;
    let idx = 0;
    if (wanted) {
      idx = sets.findIndex((x) => x.id !== "custom" && wanted.every((p) => x.problems.some((q) => q.id === p)));
      if (idx < 0) idx = sets.findIndex((x) => x.id === "custom");
    } else if (last && last.problems) {
      idx = sets.findIndex((x) => x.id !== "custom" && last.problems.every((p) => x.problems.some((q) => q.id === p)));
      if (idx < 0) idx = 0;
    }
    D.custom = wanted ? wanted.join(", ") : "";
    $("sd-custom").value = D.custom;
    $("sd-pick-filter").value = "";
    $("sd-pick-panel").hidden = true; $("sd-pick-toggle").textContent = "Choose individual problems";
    $("sd-save-set").hidden = true;
    chooseSet(idx);
    if (wanted && sets[idx].id !== "custom") { D.selected = new Set(wanted); renderPick(); }
    lockFromManifest();
    if (!dlg.open) dlg.showModal();
    $("sd-name").focus();
  }

  function submitStep() {
    const frozen = isFrozen(), m = frozen ? chosenManifest() : null;
    const a = agentOf();
    const problems = m ? m.problems : selectedProblems();
    const repeat = Math.floor(Number($("sd-repeat").value));
    const model = $("sd-model").value.trim(), judge = $("sd-judge").value.trim();
    const fail = (msg, focusId) => { $("sd-msg").textContent = msg; if (focusId) $(focusId).focus(); return false; };
    if (!a) return fail("Pick an agent.", "sd-agent");
    if (!model) return fail("Enter the agent model.", "sd-model");
    if (!judge) return fail("Enter the judge model.", "sd-judge");
    if (!problems.length) return fail("Pick at least one problem.", "sd-pick-toggle");
    if (!(repeat >= 1)) return fail("Repeats must be 1 or more.", "sd-repeat");
    if (frozen && !m) return fail("Pick a frozen campaign.", "sd-manifest");
    const set = currentSet();
    const setLabel = m ? `campaign ${m.name}` : set.id === "custom" ? "custom" : (set.name || set.label.replace(/\s*\(.*$/, "")) + (problems.length < set.problems.length ? `, ${problems.length} of ${set.problems.length}` : "");
    const typedName = $("sd-name").value.trim();
    const common = {
      model_id: model, judge_model_id: judge,
      run_class: frozen ? "frozen" : "exploration", manifest_id: m ? m.manifest_id : null,
      problems, set_label: setLabel, repeat_n: repeat, purpose: frozen ? null : ($("sd-purpose").value || null),
      only_missing: $("sd-missing").checked, record_walkthrough: $("sd-walk").checked,
    };
    const picked = !$("sd-variants-box").hidden ? (CONFIG.variant_presets || []).filter((v) => D.variants.has(v.id)) : [];
    if (picked.length) {
      const bad = picked.find((v) => !variantAgent(v));
      if (bad) return fail(`The agent for ${bad.id} is not available in this Lab.`, "sd-variants-box");
      picked.forEach((v, k) => {
        const va = variantAgent(v);
        Q.steps.push({
          ...common, step_id: uid(), name: `${v.id} · ${setLabel}`, auto_name: false,
          agent_id: va.id, agent_label: va.label, action_space: va.action_space || null,
          config_label: v.id, agent_switches: v.switches.slice(),
          force_build: $("sd-build").checked && k === 0,      // one rebuild for the whole batch, not one per step
          enabled: true,
        });
      });
      dlg.close();
      markDirty(); renderAll();
      return true;
    }
    const step = {
      ...common, step_id: editing != null ? Q.steps[editing].step_id : uid(),
      name: typedName || `${a.label} · ${setLabel}`, auto_name: !typedName,
      agent_id: a.id, agent_label: a.label, action_space: a.action_space || null,
      config_label: $("sd-label").value.trim(),
      agent_switches: a.id === "reliops_v2" ? pickedSwitches() : [],
      force_build: $("sd-build").checked,
      enabled: editing != null ? Q.steps[editing].enabled !== false : true,
    };
    if (editing != null) Q.steps[editing] = step; else Q.steps.push(step);
    dlg.close();
    markDirty(); renderAll();
    return true;
  }

  $("sd-form").addEventListener("submit", (e) => { e.preventDefault(); submitStep(); });
  $("sd-cancel").addEventListener("click", () => dlg.close());
  $("sd-x").addEventListener("click", () => dlg.close());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });   // click on the backdrop
  $("sd-agent").addEventListener("change", () => { lockFromManifest(); syncSwitchBox(); });

  // Ready-made agent variants: tick several and each becomes its own step with the rest of the form shared.
  const variantAgent = (v) => CONFIG.agents.find((a) => a.id === v.agent_id && (a.action_space || "") === (v.action_space || ""));
  const variantRunnable = (v) => !!variantAgent(v) && !v.needs_notes;
  function syncVariantBox() {
    $("sd-variants-box").hidden = editing != null || isFrozen() || !(CONFIG.variant_presets || []).length;
  }
  function renderVariants() {
    const box = $("sd-variants");
    box.innerHTML = "";
    let group = null;
    (CONFIG.variant_presets || []).forEach((v) => {
      if (v.group !== group) { group = v.group; box.append(h("div", { class: "variant-group", text: group })); }
      const id = "var-" + v.id.replace(/[^A-Za-z0-9]/g, "_");
      const cb = h("input", { type: "checkbox", id, checked: D.variants.has(v.id), onchange: (e) => {
        if (e.target.checked) D.variants.add(v.id); else D.variants.delete(v.id);
        updateVariantCount();
      } });
      const note = v.needs_notes ? " (waits for the reviewed Kubernetes notes; tick it once knowledge/k8s_notes.md exists)" : "";
      box.append(h("label", { class: "variant-row", for: id, title: v.help }, cb, h("span", { class: "mono", text: v.id }), h("span", { class: "hint", text: " " + v.help + note })));
    });
    updateVariantCount();
    syncVariantBox();
  }
  function updateVariantCount() {
    const n = D.variants.size;
    $("sd-var-count").textContent = n ? `${plural(n, "agent")} ticked: ${plural(n, "step")} will be added instead of the single agent above` : "";
  }
  $("sd-var-all").addEventListener("click", () => {
    (CONFIG.variant_presets || []).forEach((v) => { if (variantRunnable(v)) D.variants.add(v.id); });
    renderVariants();
  });
  $("sd-var-none").addEventListener("click", () => { D.variants.clear(); renderVariants(); });

  // ReliOps v2 switches: one checkbox each, shown only when the chosen agent is reliops_v2
  function renderSwitches(on) {
    const box = $("sd-switches");
    box.innerHTML = "";
    (CONFIG.agent_switches || []).forEach((sw) => {
      const id = "sw-" + sw.id;
      const cb = h("input", { type: "checkbox", id, value: sw.id });
      cb.checked = on.includes(sw.id);
      box.append(h("label", { class: "switch-row", title: sw.help }, [cb, h("span", { class: "mono", text: sw.id }), h("span", { class: "hint", text: " " + sw.help })]));
    });
    syncSwitchBox();
  }
  const pickedSwitches = () => Array.from($("sd-switches").querySelectorAll("input:checked")).map((i) => i.value);
  function syncSwitchBox() {
    const a = agentOf();
    $("sd-switches-box").hidden = !(a && a.id === "reliops_v2");
  }
  $("sd-manifest").addEventListener("change", lockFromManifest);
  document.querySelectorAll('input[name="sd-class"]').forEach((r) => r.addEventListener("change", () => { dlg.dataset.manifest = ""; lockFromManifest(); }));
  $("sd-set").addEventListener("change", () => chooseSet(Number($("sd-set").value)));
  $("sd-custom").addEventListener("input", () => { D.custom = $("sd-custom").value; renderPick(); });
  $("sd-pick-filter").addEventListener("input", renderPick);
  $("sd-pick-all").addEventListener("click", () => { currentSet().problems.forEach((p) => D.selected.add(p.id)); renderPick(); });
  $("sd-pick-none").addEventListener("click", () => { D.selected.clear(); renderPick(); });
  $("sd-pick-toggle").addEventListener("click", () => {
    const p = $("sd-pick-panel");
    p.hidden = !p.hidden;
    $("sd-pick-toggle").textContent = p.hidden ? "Choose individual problems" : "Hide the list";
  });
  $("sd-set-name").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("sd-set-save").click(); } });
  $("sd-save-set-btn").addEventListener("click", () => { $("sd-save-set").hidden = !$("sd-save-set").hidden; $("sd-set-name").focus(); });
  $("sd-set-save").addEventListener("click", async () => {
    const name = $("sd-set-name").value.trim(), ps = selectedProblems();
    if (!name || !ps.length) { $("sd-msg").textContent = "A set needs a name and at least one selected problem."; return; }
    const { ok, data } = await jsonPost("/api/problem_set", { name, problems: ps, created_by: who() });
    if (!ok) { $("sd-msg").textContent = data.error || "Could not save the set."; return; }
    try { CONFIG.problem_sets = (await jsonGet("/api/config")).problem_sets; } catch (e) { /* keep the old list */ }
    fillSets("sd-set"); fillSets("problem-set");
    const idx = CONFIG.problem_sets.findIndex((s) => s.id === "saved:" + data.set.set_id);
    if (idx >= 0) { chooseSet(idx); }
    $("sd-save-set").hidden = true; $("sd-set-name").value = ""; $("sd-msg").textContent = "";
  });
  $("q-add").addEventListener("click", () => openStepDialog(null));
  $("q-add-empty").addEventListener("click", () => openStepDialog(null));

  // ---- header buttons ---------------------------------------------------------------------------
  $("q-name").addEventListener("input", () => { Q.name = $("q-name").value; if (!dirty) { dirty = true; renderHead(); } });
  $("q-new").addEventListener("click", () => { if (dirty && !confirm("Discard the unsaved changes to this queue?")) return; openQueue(null); });
  $("q-open").addEventListener("change", () => {
    const q = saved.find((x) => x.queue_id === $("q-open").value);
    if (!q) return;
    if (dirty && !confirm("Discard the unsaved changes to this queue?")) { renderOpen(); return; }
    openQueue(q);
  });
  $("q-save").addEventListener("click", () => saveQueue(false));
  // Deleting a queue needs the password set on this machine (LAB_DELETE_PASSWORD in the Lab's .env). The server checks it.
  const dd = $("del-dialog");
  $("q-delete").addEventListener("click", async () => {
    if (!Q.queue_id) return;
    let off = false;
    try { off = (await jsonGet("/api/queue/delete-lock")).locked_off; } catch (e) { /* the server answers again on submit */ }
    $("dd-text").textContent = off
      ? `Deleting queues is locked on this machine. To unlock it, add a line LAB_DELETE_PASSWORD=your-password to the Lab's .env file. Results already recorded are kept either way.`
      : `Delete the queue "${Q.name}"? Results already recorded are kept. Type the delete password to confirm.`;
    $("dd-pass").value = ""; $("dd-pass").hidden = $("dd-pass-label").hidden = off;
    $("dd-ok").disabled = off; $("dd-msg").textContent = "";
    dd.showModal(); if (!off) $("dd-pass").focus();
  });
  $("dd-cancel").addEventListener("click", () => dd.close());
  $("dd-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!Q.queue_id) return;
    $("dd-ok").disabled = true; $("dd-msg").textContent = "";
    let r, data = {};
    try {
      r = await fetch("/api/queue/" + encodeURIComponent(Q.queue_id), { method: "DELETE", headers: { "X-Delete-Password": $("dd-pass").value } });
      data = await r.json().catch(() => ({}));
    } catch (err) { $("dd-msg").textContent = "Could not reach the Lab."; $("dd-ok").disabled = false; return; }
    $("dd-ok").disabled = false;
    if (!r.ok) { $("dd-msg").textContent = data.error || "Could not delete the queue."; $("dd-pass").select(); return; }
    dd.close(); await loadSaved(); openQueue(null);
  });
  window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  // ---- run / stop --------------------------------------------------------------------------------
  const rd = $("run-dialog");
  $("q-run").addEventListener("click", async () => {
    if (!(await saveQueue(true))) return;
    await new Promise((r) => { refreshPreview(true); setTimeout(r, 400); });
    const body = $("rd-body");
    body.innerHTML = "";
    const on = Q.steps.filter((s) => s.enabled !== false);
    body.append(h("p", { class: "hint", text: `"${Q.name}" will run ${plural(on.length, "step")} in this order, recorded under the experiment "${(preview && preview.experiment_label) || expName()}". Cells that already have a result under that name are skipped.` }));
    if (myShare()) {
      const sh = preview && preview.shares && preview.shares[myShare() - 1];
      const busy = busyElsewhere(sh);
      if (busy) body.append(h("p", { class: "hint warn-text", text: `${busy.run_by || "Someone"} (${busy.machine || "another machine"}) is running share ${myShare()} right now, last seen ${ago(busy.updated_at)}. Starting here runs the same cells twice. Press Cancel and pick another share unless you agreed this.` }));
      const asg = (sh && sh.assigned) || [];
      if (asg.length && !asg.some((x) => x.toLowerCase() === who().toLowerCase())) body.append(h("p", { class: "hint warn-text", text: `Share ${myShare()} is assigned to ${asg.join(", ")}, not to ${who() || "you"}. Start it only if they agreed.` }));
      body.append(h("p", { class: "hint", text: `This machine runs share ${myShare()} of ${Q.machines} only${sh ? ": " + sh.problems.join(", ") : ""}. The other machines run the rest under the same experiment name.` }));
    } else if ((Q.machines || 1) > 1) {
      body.append(h("p", { class: "step-issue", text: `This queue is split across ${Q.machines} machines but no share is picked, so this machine would run every problem. Pick your share under "This machine runs" unless you mean that.` }));
    }
    const list = h("ol", { class: "q-steps", style: "margin-top:12px" });
    let issue = false;
    Q.steps.forEach((s) => {
      const sp = stepPreview(s);
      if (s.enabled === false) return;
      if (sp && sp.issue) issue = true;
      list.append(h("li", { class: "step", style: "padding:10px 14px 10px 54px;box-shadow:none" },
        h("strong", { text: s.name }),
        h("div", { class: "step-line", text: sp ? `${sp.todo} of ${sp.total} runs to do` : `${s.problems.length * s.repeat_n} runs` })));
    });
    body.append(list);
    const left = preview ? preview.runs_left : 0;
    body.append(h("p", { class: "hint", text: `About ${fmtDur(left * minPerRun() * 60)} at ${minPerRun()} min per run. You can close this tab; the queue keeps running on this machine and the Lab shows it again when you come back.` }));
    if (issue) body.append(h("p", { class: "step-issue", text: "A step refers to a frozen campaign this machine cannot find. Fix it before starting." }));
    $("rd-ok").disabled = issue;
    rd.showModal();
  });
  $("rd-cancel").addEventListener("click", () => rd.close());
  rd.addEventListener("click", (e) => { if (e.target === rd) rd.close(); });
  $("rd-ok").addEventListener("click", (e) => {
    e.preventDefault(); rd.close();
    $("q-log").textContent = ""; lastId = 0; lastRecorded = -1;
    startQueue(false);
  });
  function startQueue(restart) {
    $("q-log").textContent = ""; lastId = 0; lastRecorded = -1;
    const p = new URLSearchParams({ queue_id: Q.queue_id, experiment: expName() });
    if (restart) p.set("restart", "true");
    if (myShare()) p.set("share", String(myShare()));
    if (who()) p.set("run_by", who());
    openStream("/api/queue/run?" + p.toString());
    setTimeout(pollState, 600);
  }
  $("q-restart").addEventListener("click", async () => {
    if (!(await saveQueue(true))) return;
    const r = roundOf(preview && preview.experiment_label);
    const next = r.n + 1;
    const li = (preview && preview.label_info) || { runs: 0 };
    const shared = (Q.machines || 1) > 1
      ? `\nThis queue is shared by ${Q.machines} machines. Restart starts a new round for ALL of them, not only your share: the others join it when they press Run queue. Only one person should press Restart.`
        + (r.n > 1 && li.runs ? ` "${preview.experiment_label}" already has ${plural(li.runs, "run")} (${(li.people || []).join(", ")}): someone has restarted already, so press Cancel and use Run queue to join it.` : "") + "\n"
      : "";
    const msg = `Restart "${Q.name}" from scratch?\n\n`
      + `Every step runs again from the beginning, recorded as "${r.base || expName()} (restart ${next})".\n`
      + `Results already recorded are kept; nothing is deleted.\n${shared}\nOnly press OK if you really mean to run it all again.`;
    if (!confirm(msg)) return;
    startQueue(true);
  });
  $("q-restart-share").addEventListener("click", async () => {
    const k = myShare();
    if (!k || !(await saveQueue(true))) return;
    const sh = (preview && preview.shares && preview.shares[k - 1]) || { problems: [], done: 0 };
    const label = (preview && preview.experiment_label) || expName();
    const busy = sh.claim && sh.claim.status === "running" && sh.state === "running" && sh.claim.machine !== (preview && preview.this_machine);
    const msg = `Restart share ${k} of ${Q.machines} only?\n\n`
      + `Experiment "${label}": the ${plural(sh.done, "finished run")} in share ${k} (${sh.problems.join(", ")}) stop counting, so those cells run again.\n`
      + `The other shares and other experiments are not touched. Nothing is deleted: the old runs stay in Results, they are only ignored by this queue.\n`
      + (busy ? `\n${sh.claim.run_by || "Someone"} (${sh.claim.machine || "another machine"}) is running share ${k} right now. Ask them to pause first.\n` : "")
      + `\nAfter OK, press Run queue to run the share again.`;
    if (!confirm(msg)) return;
    try {
      const res = await fetch(`/api/queue/${encodeURIComponent(Q.queue_id)}/restart-share`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ experiment: expName(), share: k, run_by: who() || null }) });
      const data = await res.json();
      if (!res.ok) { alert(data.error || "Could not restart the share."); return; }
      $("q-log").textContent = `Share ${k} restarted under "${data.experiment}": ${plural(data.runs_set_aside, "run")} set aside (kept in Results). Press Run queue to run the share again.\n`;
      refreshPreview(true);
    } catch (e) { alert("Could not restart the share: " + e); }
  });
  $("q-exp").addEventListener("input", () => {
    pref.set(expKey(), expName());
    renderTotals(); refreshPreview();
  });

  // ---- pause / continue -----------------------------------------------------------------------
  let qstate = null;
  async function loadQueueState() {
    try { qstate = (await jsonGet("/api/queue/state")).state; } catch (e) { qstate = null; }
    renderPaused();
  }
  function renderPaused() {
    const box = $("q-paused"), btn = $("q-continue");
    const open = !!qstate && ["paused", "halted", "interrupted"].includes(qstate.status) && !jobBusy();
    box.hidden = !open; btn.hidden = !open;
    if (!open) return;
    const where = qstate.step ? ` after step ${qstate.step}${qstate.step_name ? " (" + qstate.step_name + ")" : ""}` : "";
    const left = qstate.left != null ? `${plural(qstate.left, "run")} left` : "";
    const what = { paused: "Paused", halted: "Stopped by itself", interrupted: "Cut off" }[qstate.status];
    const why = qstate.status === "halted" && qstate.reason ? `: ${qstate.reason}. Fix that first` : qstate.status === "interrupted" ? " (the Lab or the machine went down)" : "";
    box.className = "q-paused" + (qstate.problem ? " problem" : "");
    const shareTxt = qstate.share ? ` · share ${qstate.share} of ${qstate.machines || "?"}` : "";
    box.textContent = qstate.problem || `${what} "${qstate.queue_name}" · experiment "${qstate.experiment}"${shareTxt}${where}${why}. ${left}. Continue keeps every finished run and runs only what is missing${qstate.share ? " in the same share" : ""}.`;
    btn.disabled = !qstate.can_continue;
  }
  $("q-continue").addEventListener("click", async () => {
    if (!qstate || !qstate.can_continue) return;
    // Continue runs the queue as stored on the server, so edits made since the last save (a ticked force-build, a changed
    // step) are saved first, exactly as Run queue does. Without this they were silently ignored.
    if (dirty && !(await saveQueue(true))) return;
    $("q-log").textContent = ""; lastId = 0; lastRecorded = -1;
    const p = new URLSearchParams();
    if (who()) p.set("run_by", who());
    qstate = null; renderPaused();
    openStream("/api/queue/continue?" + p.toString());
    setTimeout(pollState, 600);
  });

  async function requestStop() {
    stopping = true;
    ["q-stop", "job-stop"].forEach((id) => { $(id).disabled = true; $(id).textContent = "Pausing after this run…"; });
    try { await fetch("/api/stop", { method: "POST" }); } catch (e) { /* server unreachable */ }
    appendQ("\n[pause requested: the run in progress will finish and be recorded, then the queue pauses. Continue picks up from there]\n");
  }
  $("q-stop").addEventListener("click", requestStop);
  $("job-stop").addEventListener("click", requestStop);
  $("job-pill").addEventListener("click", () => showTab("queue"));

  // ---- live log -------------------------------------------------------------------------------
  const logEl = $("q-log");
  function appendQ(text) {
    const stick = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
    logEl.append(document.createTextNode(text));
    if (logEl.textContent.length > 400000) logEl.textContent = "[…earlier output trimmed here; the full log is saved on disk…]\n" + logEl.textContent.slice(-300000);
    if (stick) logEl.scrollTop = logEl.scrollHeight;
  }
  function closeStream() { if (es) { es.close(); es = null; } }
  function openStream(url) {
    closeStream();
    es = new EventSource(url);
    es.onmessage = (ev) => {
      if (ev.data === "[[DONE]]") { closeStream(); pollState(); return; }
      if (ev.lastEventId) lastId = Number(ev.lastEventId) || lastId;
      appendQ(JSON.parse(ev.data));
    };
    es.onerror = () => { closeStream(); };   // the state poll re-attaches while the job is still running
  }
  $("q-log-clear").addEventListener("click", () => { logEl.textContent = ""; });

  // ---- job state (header pill, "now" panel) -----------------------------------------------------
  async function pollState() {
    let st;
    try { st = await jsonGet("/api/state"); } catch (e) { return; }
    const prev = job;
    job = st.job;
    renderKeyAlerts(st.key_alerts);
    if (job && job.state === "running" && !es) openStream(`/api/job/stream?offset=${lastId > 0 ? lastId : -300}`);
    renderJob(prev);
  }

  function renderJob(prev) {
    const running = jobBusy();
    const pill = $("job-pill");
    pill.hidden = !running;
    $("job-stop").hidden = !running || job.kind === "cluster";   // a cluster repair has no "after current" point to stop at
    if (running) {
      const p = job.progress || {};
      const bits = [job.kind === "queue" ? "Queue running" : job.kind === "cluster" ? "Cluster repair running" : "Run in progress"];
      if (p.step && p.steps) bits.push(`step ${p.step}/${p.steps}`);
      if (job.run_elapsed_s != null) bits.push(fmtDur(job.run_elapsed_s));
      $("job-pill-text").textContent = bits.join(" · ");
      if (!stopping) { $("job-stop").disabled = false; $("job-stop").textContent = "Pause after current"; }
    } else {
      stopping = false;
      ["q-stop", "job-stop"].forEach((id) => { $(id).textContent = id === "q-stop" ? "Pause after current run" : "Pause after current"; });
    }
    const now = $("q-now");
    now.hidden = !job;
    if (job) {
      const p = job.progress || {};
      const t0 = new Date(job.started_at * 1000);
      const when = t0.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
      if (running) {
        $("q-now-title").textContent = job.kind === "queue" ? `Running "${job.name}"` : job.kind === "cluster" ? `Cluster repair: ${job.name}` : "Quick run in progress";
        $("q-now-sub").textContent = `started ${when}`;
        const main = [];
        if (p.step && p.steps) main.push(`Step ${p.step}/${p.steps}: ${p.step_name}`);
        $("q-now-main").textContent = p.problem ? `${main.join(" · ")}${main.length ? "  →  " : ""}${p.problem}${p.repeat_n > 1 ? ` (repeat ${p.repeat}/${p.repeat_n})` : ""}` : (main.join(" · ") || "starting…");
        const pct = p.step_runs ? Math.min(100, (100 * (p.step_recorded || 0)) / p.step_runs) : 0;
        $("q-now-meter").style.width = pct + "%";
      } else {
        const label = { done: "Finished", stopped: "Paused", error: "Stopped by an error" }[job.state] || job.state;
        const end = job.finished_at ? new Date(job.finished_at * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
        $("q-now-title").textContent = `${label}: ${job.name}`;
        $("q-now-sub").textContent = end ? `ended ${end}` : "";
        $("q-now-main").textContent = `${plural((job.progress && job.progress.recorded) || 0, "run")} recorded in this job.${job.log_file ? " Full log: results/gui_logs/" + job.log_file : ""}`;
        $("q-now-meter").style.width = job.state === "done" ? "100%" : "0%";
      }
    }
    if (prev && prev.state === "running" && !running) loadQueueState();
    else if (running && qstate) { qstate = null; renderPaused(); }
    // outcome-driven refresh: a run was recorded, or the job just ended
    const rec = job && job.progress ? job.progress.recorded || 0 : 0;
    if (rec !== lastRecorded || (prev && prev.state === "running" && !running)) {
      lastRecorded = rec;
      refreshPreview(true);
      if (prev && prev.state === "running" && !running && typeof loadResults === "function") loadResults();
    } else {
      const sig = JSON.stringify([job && job.id, job && job.state, job && job.progress && [job.progress.step, job.progress.problem, job.progress.repeat]]);
      if (sig !== lastSig) renderSteps();
      lastSig = sig;
    }
    renderTotals();
    if (stopping && running) { $("q-stop").disabled = true; $("q-stop").textContent = "Pausing after this run…"; }
  }

  // live elapsed counter between polls
  setInterval(() => {
    if (!jobBusy() || job.run_elapsed_s == null) return;
    job.run_elapsed_s += 1;
    const p = job.progress || {};
    const bits = [job.kind === "queue" ? "Queue running" : job.kind === "cluster" ? "Cluster repair running" : "Run in progress"];
    if (p.step && p.steps) bits.push(`step ${p.step}/${p.steps}`);
    bits.push(fmtDur(job.run_elapsed_s));
    $("job-pill-text").textContent = bits.join(" · ");
  }, 1000);

  // ---- tabs: remember the open tab in the address, and refresh when the queue tab opens -----------
  const origShowTab = showTab;
  showTab = function (name) {
    origShowTab(name);
    try { history.replaceState(null, "", "#" + name); } catch (e) { /* ignore */ }
  };

  // ---- Key health (Setup tab): the Lab marks a key dead or resting when a run shows the provider's error,
  // and a rotation pool skips such keys. You can also set a mark by hand. Only names are shown, never the secret.
  async function loadKeyHealth() {
    let d;
    try { d = await jsonGet("/api/keys"); } catch (e) { return; }
    const box = $("key-health");
    box.innerHTML = "";
    if (!d.keys.length) box.append(h("p", { class: "hint", text: "No model key saved yet." }));
    d.keys.forEach((k) => {
      const until = k.status === "exhausted" && k.until ? " until " + new Date(k.until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
      const row = h("div", { class: "key-row", "data-var": k.var });
      if (k.rotatable) {
        const cb = h("input", { type: "checkbox", "aria-label": "rotate " + k.var, title: k.status === "dead" ? "Dead: replace the key or clear the mark first" : "Include in rotation" });
        cb.checked = !!k.in_pool; cb.disabled = k.status === "dead" && !k.in_pool;
        row.append(cb);
      }
      row.append(h("code", { text: k.var }));
      if (k.rotatable) row.append(h("input", { type: "text", class: "ko", placeholder: "owner", value: k.owner !== k.var ? k.owner : "", "aria-label": "owner of " + k.var }));
      row.append(h("span", { class: "pill " + (k.status === "ok" ? "ok" : "warn"), title: k.reason || "" }, h("span", { class: "dot" }), k.status === "ok" ? "ok" : k.status === "dead" ? "dead" : "resting" + until));
      const set = (status) => async () => { await jsonPost("/api/keys/mark", { var: k.var, status, by: who() }); loadKeyHealth(); pollState(); };
      row.append(h("button", { type: "button", class: "ghost small", text: "Mark dead", onclick: set("dead") }),
                 h("button", { type: "button", class: "ghost small", text: "Mark resting for today", onclick: set("exhausted") }),
                 h("button", { type: "button", class: "ghost small", text: "Clear", onclick: set("ok") }));
      if (k.source) row.append(h("span", { class: "hint", style: "flex-basis:100%;margin:0", text: `from ${k.source}${k.shape ? " · " + k.shape : ""}${k.note ? ". " + k.note : ""}` }));
      box.append(row);
    });
    $("key-health-help").textContent = d.pool ? "Rotation is ON: each run uses the next healthy ticked key." : "Rotation is OFF: every run uses GEMINI_API_KEY.";
  }
  $("key-pool-save").addEventListener("click", async () => {
    const vars = [], owners = {};
    document.querySelectorAll("#key-health .key-row").forEach((r) => {
      const cb = r.querySelector('input[type="checkbox"]');
      if (cb && cb.checked) { const v = r.getAttribute("data-var"); vars.push(v); const o = r.querySelector("input.ko"); if (o && o.value.trim()) owners[v] = o.value.trim(); }
    });
    const msg = $("key-pool-msg");
    const { ok, data } = await jsonPost("/api/keys/pool", { vars, owners });
    msg.textContent = ok ? (data.message || "saved") : (data.error || "could not save");
    loadKeyHealth(); pollState();
  });

  // ---- Key alert bar (every tab): a dead key needs a person; a resting one comes back by itself ----------------
  const baseTitle = document.title;
  function renderKeyAlerts(alerts) {
    const bar = $("key-alert");
    alerts = alerts || [];
    bar.innerHTML = "";
    bar.hidden = alerts.length === 0;
    const dead = alerts.filter((a) => a.status === "dead");
    document.title = dead.length ? `⚠ ${dead.length} dead key${dead.length > 1 ? "s" : ""} · ${baseTitle}` : baseTitle;
    alerts.forEach((a) => {
      const who_ = a.owner && a.owner !== a.var ? `${a.var} (${a.owner})` : a.var;
      const row = h("div", { class: "ka-row" + (a.status === "dead" ? "" : " rest") });
      if (a.status === "dead") {
        row.append(h("span", { class: "ka-text" }, h("strong", { text: "Dead key: " }), `${who_}. ${a.reason || "the provider rejected it"}. ${a.in_pool ? "It is skipped while it stays dead. Remove it from the rotation, or replace the key value in .env (the mark clears by itself)." : "Runs will stop until you replace the key value in .env."}`));
        if (a.in_pool) row.append(h("button", { type: "button", class: "secondary small", text: "Remove from rotation", onclick: async () => { await jsonPost("/api/keys/pool/remove", { var: a.var }); loadKeyHealth(); pollState(); } }));
        row.append(h("button", { type: "button", class: "ghost small", text: "It works: clear the mark", onclick: async () => { await jsonPost("/api/keys/mark", { var: a.var, status: "ok", by: who() }); loadKeyHealth(); pollState(); } }));
      } else {
        const t = a.until ? new Date(a.until * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "later";
        row.append(h("span", { class: "ka-text" }, h("strong", { text: "Resting: " }), `${who_} used up its quota and is back at ${t}.`));
      }
      bar.append(row);
    });
  }
  // ---- Cluster health (Setup tab): a read-only check of the local kind cluster, and three fixed repairs. The browser
  // never sends a command, only the name of a repair; the server builds the commands itself (see backend/cluster.py).
  async function loadCluster() {
    const btn = $("cluster-check");
    btn.disabled = true;
    $("cluster-checked").textContent = "checking…";
    let d;
    try { d = await jsonGet("/api/cluster"); } catch (e) { $("cluster-checked").textContent = "could not check"; btn.disabled = false; return; }
    btn.disabled = false;
    $("cluster-checked").textContent = "checked " + new Date(d.checked_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const finds = $("cluster-findings");
    finds.innerHTML = "";
    d.findings.forEach((f) => finds.append(h("div", { class: "cf " + f.level, text: f.text })));
    Object.entries(d.kubelet_logs || {}).forEach(([name, text]) => finds.append(
      h("details", { class: "cf-log" }, h("summary", { text: `Last kubelet log lines on ${name}` }), h("pre", { text }))));
    const nodes = $("cluster-nodes");
    nodes.innerHTML = "";
    (d.nodes || []).forEach((n) => nodes.append(h("span", { class: "pill " + (n.status === "Ready" ? "ok" : "warn"), title: n.roles.join(", ") + (n.cordoned ? " · cordoned" : "") },
      h("span", { class: "dot" }), `${n.name} ${n.status}`)));
    $("cluster-advice").textContent = d.advice || "";
    const box = $("cluster-actions");
    box.innerHTML = "";
    d.actions.forEach((a) => {
      const run = async () => {
        const cmds = a.commands.length ? a.commands.join("\n") : "(none right now)";
        if (!confirm(`${a.title}?\n\n${a.effect}\n\nThe Lab will run:\n${cmds}\n\nGo ahead?`)) return;
        const { ok, data } = await jsonPost("/api/cluster/repair", { action: a.id, confirm: true });
        if (!ok) { alert(data.error || "could not start the repair"); return; }
        showTab("queue");
        pollState();
      };
      const btn2 = h("button", { type: "button", class: "secondary", text: a.title, onclick: run, title: a.disabled_reason || a.effect });
      btn2.disabled = !a.enabled;
      const row = h("div", { class: "ca-row" }, btn2, h("span", { class: "hint", text: a.enabled ? a.effect : a.disabled_reason }));
      box.append(row);
    });
  }
  $("cluster-check").addEventListener("click", loadCluster);

  TAB_LOADERS.run = () => { loadKeyHealth(); loadCluster(); };

  TAB_LOADERS.queue = () => { loadManifests(); refreshPreview(true); };
  // The board shows what other machines are doing, so it refreshes by itself while the Queue tab is open (one cheap read).
  setInterval(() => { if (!document.hidden && !dirty && !$("tab-queue").hidden && Q && Q.steps && Q.steps.length) refreshPreview(true); }, 60000);

  // ---- boot ------------------------------------------------------------------------------------
  async function init() {
    while (typeof CONFIG === "undefined" || !CONFIG) await sleep(100);
    const est = $("q-total-sub");
    est.innerHTML = "";
    est.append(h("span", { id: "q-eta" }), " at ",
      h("input", { id: "q-min", type: "number", min: "1", value: pref.get("lab_min_per_run", "20"), "aria-label": "Minutes per run", title: "Your own estimate of minutes per run; the sprint plan assumes about 20" }),
      " min per run (your estimate)");
    $("q-min").addEventListener("input", () => { pref.set("lab_min_per_run", $("q-min").value); renderTotals(); });
    await Promise.all([loadSaved(), loadManifests()]);
    const wanted = pref.get("lab_queue_id", "");
    openQueue(saved.find((q) => q.queue_id === wanted) || saved[0] || null);
    const tab = (location.hash || "").slice(1);
    if (tab && document.getElementById("tab-" + tab)) showTab(tab);
    await pollState();
    loadQueueState();
    // An error while drawing one update must not end the loop: the page would then stop following the queue (no running
    // problem, no counts, no new results) until it is reloaded.
    for (;;) { await sleep(jobBusy() ? 3000 : 8000); try { await pollState(); } catch (e) { console.error("state refresh failed", e); } }
  }
  init();
})();
