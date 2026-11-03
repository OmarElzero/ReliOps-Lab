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
  function loadExpName() { $("q-exp").value = pref.get(expKey(), pref.get("lab_exp_last", "")); }
  function renderExperiments() {
    const dl = $("q-exp-list");
    dl.innerHTML = "";
    ((preview && preview.experiments) || []).forEach((e) => dl.append(h("option", { value: e.name, label: `${plural(e.runs, "run")} recorded` })));
    const note = $("q-exp-note");
    const name = expName();
    if (!name) { note.textContent = "Give this experiment a name; every run is recorded under it."; return; }
    const known = ((preview && preview.experiments) || []).find((e) => e.name === name);
    const r = roundOf(preview && preview.experiment_label);
    note.textContent = !known ? "New name: nothing recorded under it yet, so every cell is to do."
      : `${plural(known.runs, "run")} already recorded${r.n > 1 ? ` (counting round ${r.n})` : ""}. Same name continues; a new name starts fresh.`;
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
  function refreshPreview(now) {
    clearTimeout(previewTimer);
    if (!Q.steps.length) { preview = null; renderSteps(); renderTotals(); return; }
    previewTimer = setTimeout(async () => {
      const { ok, data } = await jsonPost("/api/queue/preview", { queue_id: Q.queue_id, name: Q.name, steps: Q.steps, experiment: expName(),
                                                                  machines: Q.machines || 1, shares: Q.shares, split: Q.split || "problem", order: Q.order || "step", share: myShare() || null });
      preview = ok ? data : null;
      renderSteps(); renderTotals(); renderExperiments(); renderShares();
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
  function renderShares() {
    const n = Q.machines || 1, box = $("q-share-box"), panel = $("q-shares");
    box.hidden = n <= 1; panel.hidden = n <= 1; $("q-split").hidden = n <= 1;
    if (n <= 1) return;
    const shares = (preview && preview.shares) || [];
    const sel = $("q-share"), k = myShare();
    sel.innerHTML = "";
    sel.append(h("option", { value: "0", text: "Every problem (no split)" }));
    for (let i = 1; i <= n; i++) {
      const sh = shares[i - 1];
      const unitWord = Q.split === "repeat" ? "pair" : "problem";
      sel.append(h("option", { value: String(i), text: `Share ${i} of ${n}` + (sh ? ` · ${plural(sh.problems.length, unitWord)}, ${sh.left} left` : "") }));
    }
    sel.value = String(k);
    const note = $("q-share-note");
    const mine = k && shares[k - 1];
    const others = mine ? mine.people.filter((x) => x.name !== who() && Date.now() - Date.parse(x.last || 0) < 3 * 3600 * 1000) : [];
    note.className = "hint" + (others.length ? " warn-text" : "");
    note.textContent = !k
      ? "Pick your share so the machines never run the same cells. Agree who takes which share first."
      : others.length
        ? `${others.map((x) => x.name).join(", ")} recorded runs in share ${k} ${ago(others[0].last)}. Two machines on one share run the same cells twice; check before you start.`
        : `Only the problems of share ${k} run here. The grid still shows the whole queue; the other shares are dimmed.`;
    const wasOpen = !!panel.querySelector("details[open]");
    panel.innerHTML = "";
    if (!shares.length) return;
    const t = h("table", {}, h("thead", {}, h("tr", {},
      h("th", { text: "Share" }), h("th", { text: Q.split === "repeat" ? "Problem #repeat" : "Problems" }), h("th", { text: "Done" }), h("th", { text: "Left" }), h("th", { text: "Run by" }))));
    const tb = h("tbody");
    shares.forEach((sh) => {
      const people = sh.people.length ? sh.people.map((x) => `${x.name} (${ago(x.last)})`).join(", ") : "nobody yet";
      tb.append(h("tr", { class: sh.share === k ? "me" : "" },
        h("td", { text: `${sh.share}${sh.share === k ? " · this machine" : ""}` }),
        h("td", { class: "probs", text: sh.problems.join(", ") }),
        h("td", { text: `${sh.done} of ${sh.total}` }),
        h("td", { text: String(sh.left) }),
        h("td", { text: people })));
    });
    t.append(tb);
    const det = h("details", {}, h("summary", { text: `Machine shares: who runs what (${n} machines, ${shares.reduce((x, sh) => x + sh.left, 0)} runs left in all)` }), t);
    det.open = wasOpen;
    panel.append(det);
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
  function renderAll() { renderHead(); renderSteps(); renderTotals(); renderShares(); }

  function renderHead() {
    if (dirty) setStatus("Unsaved changes", "dirty");
    else setStatus(Q.queue_id ? "Saved" : "", Q.queue_id ? "saved" : "");
    if (!dirty && Q.queue_id) $("q-state").hidden = false;
    $("q-save").disabled = !dirty && !!Q.queue_id;
    $("q-empty").hidden = Q.steps.length > 0;
    $("q-add").hidden = Q.steps.length === 0;
    $("q-name").disabled = false;
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
    const run = job && job.progress && queueRunning() && job.progress.step === Q.steps.indexOf(s) + 1 ? job.progress : null;
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

  function renderSteps() {
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
  $("q-delete").addEventListener("click", async () => {
    if (!Q.queue_id || !confirm(`Delete the queue "${Q.name}"? Results already recorded are kept.`)) return;
    await fetch("/api/queue/" + encodeURIComponent(Q.queue_id), { method: "DELETE" });
    await loadSaved(); openQueue(null);
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
    const msg = `Restart "${Q.name}" from scratch?\n\n`
      + `Every step runs again from the beginning, recorded as "${r.base || expName()} (restart ${next})".\n`
      + `Results already recorded are kept; nothing is deleted.\n\nOnly press OK if you really mean to run it all again.`;
    if (!confirm(msg)) return;
    startQueue(true);
  });
  $("q-exp").addEventListener("input", () => {
    pref.set(expKey(), expName()); pref.set("lab_exp_last", expName());
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
    for (;;) { await sleep(jobBusy() ? 3000 : 8000); await pollState(); }
  }
  init();
})();
