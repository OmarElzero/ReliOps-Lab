// Results dashboard. Loaded after app.js, v2.js and queue.js; uses their globals (jsonGet, jsonPost, who, el,
// pct, interval, costText, tagText, groupName, passCell, timeCell, logCell, storyCell, linkButton, showDetail,
// diagnosisDetail, applyGroups, fillLabelPresets, TAB_LOADERS).
// The server does the work: POST /api/dashboard with the filters returns every number shown here, computed from
// exactly the runs the filters leave in view.
(function () {
  "use strict";

  const STORE_KEY = "relilab.dash.filters";
  // filter name -> label shown on the dropdown, in the order they appear
  const FIELDS = [
    ["category", "Category"], ["problem_id", "Problem"], ["agent_id", "Agent"], ["action_space", "Action space"],
    ["model_id", "Model"], ["config_label", "Config label"], ["judge_model_id", "Judge"], ["run_class", "Run class"],
    ["outcome", "Outcome"], ["failure_tag", "Failure tag"], ["app", "App"], ["run_by", "Run by"], ["manifest_id", "Frozen campaign"], ["experiment", "Experiment"],
  ];
  const PURPOSES = [
    ["", "All runs", "every recorded run"],
    ["experiment", "Experiments", "trying things; read for learning, never reported"],
    ["dev", "Dev set", "development set; failures may be read to choose agent changes"],
    ["final", "Final", "frozen set of the current phase; reported, never used to choose changes"],
  ];

  let F = loadFilters();
  let DATA = null;
  let openName = null;
  let timer = null;
  let seq = 0;
  const selected = new Set();

  const $ = (id) => document.getElementById(id);
  const metric = () => $("sum-metric").value;

  function loadFilters() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {}; } catch (e) { return {}; }
  }
  function saveFilters() { try { localStorage.setItem(STORE_KEY, JSON.stringify(F)); } catch (e) { /* storage may be blocked */ } }

  function isActive(k) { return Array.isArray(F[k]) ? F[k].length > 0 : !!F[k]; }
  function anyActive() { return Object.keys(F).some(isActive); }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(load, ms === undefined ? 120 : ms);
  }

  async function load() {
    const my = ++seq;
    let d;
    try {
      const r = await fetch("/api/dashboard", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filters: F, metric: metric() }),
      });
      d = await r.json();
      if (!r.ok) throw new Error(d.error || r.status);
    } catch (e) {
      $("dash-source").textContent = "Could not load results: " + e.message + " — is the server running?";
      return;
    }
    if (my !== seq) return;          // a newer request is already on its way
    DATA = d;
    render();
  }

  // ---- rendering -----------------------------------------------------------------
  function render() {
    const d = DATA;
    $("dash-source").textContent = d.provenance.source === "firestore"
      ? "Showing the whole team's runs (via Firestore)."
      : "Showing this machine's own runs only. Firebase is not connected, so teammates' runs are not here (Setup & quick run → Keys & Firebase).";
    renderPurpose();
    renderFilters();
    renderActive();
    renderKpis();
    renderNotes();
    renderPeople();
    renderProvenance();
    renderSummary();
    renderCategories();
    renderMatrix();
    renderTags();
    renderTimeline();
    renderRuns();
    if (window.applyGroups) window.applyGroups(d.groups, metric());
    if (window.fillLabelPresets) window.fillLabelPresets(d.runs);
  }

  function renderPurpose() {
    const box = $("dash-purpose");
    box.innerHTML = "";
    const counts = Object.fromEntries((DATA.options.purpose || []).map((o) => [o.value, o.n]));
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const cur = (F.purpose || [])[0] || "";
    PURPOSES.forEach(([val, label, help]) => {
      const b = el("button", undefined, "chip" + (cur === val ? " on" : ""));
      b.type = "button";
      b.title = help;
      b.appendChild(document.createTextNode(label));
      b.appendChild(el("span", String(val ? (counts[val] || 0) : total), "chip-n"));
      b.addEventListener("click", () => { F.purpose = val ? [val] : []; saveFilters(); selected.clear(); schedule(0); });
      box.appendChild(b);
    });
  }

  function renderFilters() {
    const bar = $("dash-filters");
    bar.innerHTML = "";
    const q = el("input", undefined, "f-search");
    q.type = "search"; q.placeholder = "Search problem, label, person, note, diagnosis…"; q.value = F.q || "";
    q.addEventListener("input", () => { F.q = q.value; saveFilters(); schedule(250); });
    bar.appendChild(q);

    FIELDS.forEach(([name, label]) => {
      const opts = (DATA.options[name] || []);
      if (!opts.length || (opts.length === 1 && !isActive(name) && name !== "category" && name !== "problem_id")) return;
      const det = el("details", undefined, "msel" + (isActive(name) ? " has" : ""));
      if (openName === name) det.open = true;
      det.addEventListener("toggle", () => {
        if (det.open) { openName = name; document.querySelectorAll("details.msel").forEach((o) => { if (o !== det) o.open = false; }); }
        else if (openName === name) openName = null;
      });
      const sum = el("summary");
      sum.appendChild(document.createTextNode(label));
      const n = (F[name] || []).length;
      if (n) sum.appendChild(el("span", String(n), "msel-n"));
      det.appendChild(sum);
      const panel = el("div", undefined, "msel-panel");
      opts.forEach((o) => {
        const lab = el("label", undefined, "msel-opt");
        const cb = document.createElement("input");
        cb.type = "checkbox"; cb.checked = (F[name] || []).includes(o.value);
        cb.addEventListener("change", () => {
          const set = new Set(F[name] || []);
          if (cb.checked) set.add(o.value); else set.delete(o.value);
          F[name] = [...set]; saveFilters(); selected.clear(); schedule();
        });
        lab.appendChild(cb);
        lab.appendChild(el("span", o.value, "msel-v"));
        lab.appendChild(el("span", String(o.n), "msel-c"));
        panel.appendChild(lab);
      });
      det.appendChild(panel);
      bar.appendChild(det);
    });

    const dates = el("div", undefined, "f-dates");
    [["date_from", "From"], ["date_to", "To"]].forEach(([k, label]) => {
      const lab = el("label", undefined, "f-date");
      lab.appendChild(document.createTextNode(label));
      const i = document.createElement("input");
      i.type = "date"; i.value = F[k] || "";
      i.addEventListener("change", () => { F[k] = i.value; saveFilters(); schedule(0); });
      lab.appendChild(i);
      dates.appendChild(lab);
    });
    bar.appendChild(dates);
  }

  function renderActive() {
    const box = $("dash-active");
    box.innerHTML = "";
    if (!anyActive()) { box.hidden = true; return; }
    box.hidden = false;
    const add = (key, value, text) => {
      const c = el("button", undefined, "fchip");
      c.type = "button"; c.title = "remove this filter";
      c.appendChild(document.createTextNode(text));
      c.appendChild(el("span", "×", "x"));
      c.addEventListener("click", () => {
        if (Array.isArray(F[key])) F[key] = F[key].filter((v) => v !== value); else F[key] = "";
        saveFilters(); selected.clear(); schedule(0);
      });
      box.appendChild(c);
    };
    FIELDS.concat([["purpose", "Purpose"]]).forEach(([k, label]) => (F[k] || []).forEach((v) => add(k, v, `${label}: ${v}`)));
    if (F.q) add("q", null, `Search: “${F.q}”`);
    if (F.date_from) add("date_from", null, `From ${F.date_from}`);
    if (F.date_to) add("date_to", null, `To ${F.date_to}`);
    const clear = el("button", "Clear all filters", "link-btn");
    clear.type = "button";
    clear.addEventListener("click", () => { F = {}; saveFilters(); selected.clear(); schedule(0); });
    box.appendChild(clear);
  }

  function renderKpis() {
    const k = DATA.kpis;
    const tiles = [
      ["Runs in view", k.runs, `${k.known} with a known outcome`],
      ["Passed", `${k.passes} / ${k.known}`, "of runs with a known outcome"],
      ["Failed", k.failures, `${k.tagged_failures} tagged, ${k.failures - k.tagged_failures} to read`],
      ["Incomplete", k.incomplete, "no known outcome; never counted as failed"],
      ["Problems", k.problems, ""],
      ["Configurations", k.configs, ""],
    ];
    const box = $("dash-kpis");
    box.innerHTML = "";
    tiles.forEach(([label, value, sub]) => {
      const t = el("div", undefined, "kpi");
      t.appendChild(el("div", label, "kpi-l"));
      t.appendChild(el("div", String(value), "kpi-v"));
      if (sub) t.appendChild(el("div", sub, "kpi-s"));
      box.appendChild(t);
    });
  }

  function renderNotes() {
    const box = $("dash-notes");
    box.innerHTML = "";
    box.hidden = !DATA.notes.length;
    if (!DATA.notes.length) return;
    box.appendChild(el("strong", "Read before quoting a number from this view"));
    const ul = el("ul");
    DATA.notes.forEach((n) => ul.appendChild(el("li", n)));
    box.appendChild(ul);
  }

  function kv(grid, key, value) {
    grid.appendChild(el("div", key, "pv-k"));
    const v = el("div", undefined, "pv-v");
    if (value instanceof Node) v.appendChild(value); else v.textContent = value;
    grid.appendChild(v);
  }
  const counted = (obj) => Object.entries(obj || {}).map(([k, v]) => `${k} (${v})`).join(", ") || "—";

  function renderProvenance() {
    const p = DATA.provenance;
    const box = $("dash-prov");
    box.innerHTML = "";
    const grid = el("div", undefined, "pv-grid");
    kv(grid, "Runs", `${p.runs}${p.date_from ? ` · ${p.date_from} to ${p.date_to}` : ""}`);
    kv(grid, "Purposes", counted(p.purposes));
    kv(grid, "Run classes", counted(p.run_classes));
    kv(grid, "Agent models", p.models.join(", ") || "—");
    kv(grid, "Run by", p.run_by.join(", ") || "—");
    kv(grid, "Agent code commits", p.commits.map((c) => `${(c.commit || "?").slice(0, 8)} (${c.n} runs)`).join(", ") || "—");
    kv(grid, "Hand-labelled", `${p.hand_labelled_in_view} runs in view · ${p.total_hand_labels} labels overall`);
    box.appendChild(grid);

    box.appendChild(el("h3", "Judges", "pv-h"));
    const jt = el("table", undefined, "results-table");
    const jh = el("tr");
    ["Judge", "Agrees with hand labels", "Cohen’s kappa", "Overlap"].forEach((h) => jh.appendChild(el("th", h)));
    jt.appendChild(jh);
    p.judges.forEach((j) => {
      const a = j.audit;
      const tr = el("tr");
      tr.appendChild(el("td", j.judge));
      if (a && a.labeled_overlap) {
        tr.appendChild(el("td", `${a.agree} of ${a.labeled_overlap} (${pct(a.agreement)})`));
        tr.appendChild(el("td", typeof a.kappa === "number" ? a.kappa.toFixed(2) : "—"));
        tr.appendChild(el("td", `${a.labeled_overlap} runs`));
      } else {
        const td = el("td", "no hand-labelled runs yet, so its diagnosis verdicts are unchecked");
        td.colSpan = 3; td.className = "r-warn";
        tr.appendChild(td);
      }
      jt.appendChild(tr);
    });
    const jw = el("div", undefined, "results-table-wrap"); jw.appendChild(jt); box.appendChild(jw);

    if (p.manifests.length) {
      box.appendChild(el("h3", "Frozen campaigns in view", "pv-h"));
      const t = el("table", undefined, "results-table");
      const h = el("tr");
      ["Campaign", "Configuration", "Commit", "Judge", "Problems (hash)", "Repeats", "Cells done"].forEach((x) => h.appendChild(el("th", x)));
      t.appendChild(h);
      p.manifests.forEach((m) => {
        const tr = el("tr");
        if (m.missing) {
          tr.appendChild(el("td", m.manifest_id));
          const td = el("td", "not found on this machine — it may be on a teammate's laptop and not synced");
          td.colSpan = 6; td.className = "r-warn"; tr.appendChild(td);
        } else {
          const c = m.counts;
          [`${m.name} (${m.manifest_id})`, `${m.agent_id} · ${m.model_id} · ${m.action_space || "-"} · ${m.config_label}`,
            (m.git_commit || "?").slice(0, 8), m.judge_model_id, `${m.problems} (${m.problems_hash})`, String(m.repeat_n),
            `${c.pass + c.fail} of ${m.cells_total}` + (c.incomplete || c.missing ? ` · ${c.incomplete} incomplete, ${c.missing} missing` : " · complete"),
          ].forEach((x) => tr.appendChild(el("td", x)));
        }
        t.appendChild(tr);
      });
      const w = el("div", undefined, "results-table-wrap"); w.appendChild(t); box.appendChild(w);
    }
  }

  function renderPeople() {
    const box = $("dash-people");
    box.innerHTML = "";
    if (!DATA.people.length) { box.appendChild(el("p", "No runs match these filters.", "hint")); return; }
    const t = el("table", undefined, "results-table");
    const h = el("tr");
    ["Person", "Runs", "Pass / known", "Incomplete", "Problems", "Configurations", "Purposes", "Active"].forEach((x) => h.appendChild(el("th", x)));
    t.appendChild(h);
    DATA.people.forEach((p) => {
      const tr = el("tr");
      const name = el("td", undefined, "r-problem");
      name.appendChild(linkButton(p.name, () => addFilter("run_by", p.name), "show only this person's runs"));
      tr.appendChild(name);
      [String(p.runs), `${p.pass} / ${p.n}`, String(p.incomplete), String(p.problems), String(p.configs),
        Object.entries(p.purposes).map(([k, v]) => `${k} ${v}`).join(", "),
        p.first === p.last ? (p.first || "—") : `${p.first} to ${p.last}`].forEach((x) => tr.appendChild(el("td", x)));
      t.appendChild(tr);
    });
    box.appendChild(t);
  }

  function renderSummary() {
    const tbody = $("summary-tbody");
    tbody.innerHTML = "";
    $("summary-empty").hidden = DATA.groups.length > 0;
    DATA.groups.forEach((g) => {
      const tr = el("tr");
      const cfg = el("td", undefined, "r-config");
      cfg.appendChild(el("div", g.config_label !== "-" ? g.config_label : "(no label)", "cfg-label"));
      cfg.appendChild(el("div", `${g.agent_id} · ${g.model_id} · ${g.action_space}`, "cfg-sub"));
      cfg.appendChild(el("div", `judge ${g.judge_model_id} · ${g.run_class}`, "cfg-sub"));
      tr.appendChild(cfg);
      tr.appendChild(el("td", `${g.passes} / ${g.n}`));
      const rate = el("td", undefined, "r-rate");
      rate.appendChild(el("div", pct(g.rate)));
      if (typeof g.wilson_lo === "number") {
        const bar = el("div", undefined, "ratebar");
        const ci = el("span", undefined, "ci");
        ci.style.left = (g.wilson_lo * 100) + "%"; ci.style.width = Math.max(1, (g.wilson_hi - g.wilson_lo) * 100) + "%";
        const pt = el("span", undefined, "pt"); pt.style.left = (g.rate * 100) + "%";
        bar.appendChild(ci); bar.appendChild(pt);
        bar.title = `rate ${pct(g.rate)}, 95% interval ${interval(g.wilson_lo, g.wilson_hi)}`;
        rate.appendChild(bar);
      }
      tr.appendChild(rate);
      tr.appendChild(el("td", interval(g.wilson_lo, g.wilson_hi), "r-interval"));
      tr.appendChild(el("td", interval(g.boot_lo, g.boot_hi), "r-interval"));
      tr.appendChild(el("td", String(g.problems)));
      tr.appendChild(el("td", String(g.incomplete || 0)));
      tr.appendChild(el("td", costText(g), "r-wrap"));
      tr.appendChild(el("td", tagText(g), "r-wrap"));
      tbody.appendChild(tr);
    });
  }

  function colHead(g) {
    const th = el("th", undefined, "col-cfg");
    th.title = groupName(g);
    th.appendChild(el("div", g.config_label !== "-" ? g.config_label : g.agent_id, "cfg-label"));
    th.appendChild(el("div", `${g.action_space} · ${g.model_id}`, "cfg-sub"));
    return th;
  }

  function cellTd(c) {
    const td = el("td", undefined, "mcell");
    if (!c || (!c.n && !c.incomplete)) { td.textContent = "—"; td.classList.add("m-none"); return td; }
    td.textContent = c.n ? `${c.pass}/${c.n}` : "?";
    if (c.incomplete) td.textContent += ` +${c.incomplete}?`;
    if (!c.n) td.classList.add("m-none");
    else if (c.pass === c.n) td.classList.add("m-pass");
    else if (c.pass === 0) td.classList.add("m-fail");
    else td.classList.add("m-mix");
    td.title = `${c.pass} passed of ${c.n} with a known outcome` + (c.incomplete ? `; ${c.incomplete} incomplete` : "");
    return td;
  }

  function renderCategories() {
    const box = $("dash-cats");
    box.innerHTML = "";
    if (!DATA.categories.length) { box.appendChild(el("p", "No runs match these filters.", "hint")); return; }
    const t = el("table", undefined, "results-table matrix");
    const h = el("tr");
    h.appendChild(el("th", "Category")); h.appendChild(el("th", "Problems"));
    DATA.groups.forEach((g) => h.appendChild(colHead(g)));
    t.appendChild(h);
    DATA.categories.forEach((c) => {
      const tr = el("tr");
      const name = el("td", undefined, "r-problem");
      name.appendChild(linkButton(c.category, () => addFilter("category", c.category), "show only this category"));
      tr.appendChild(name);
      tr.appendChild(el("td", String(c.problems)));
      DATA.groups.forEach((g) => tr.appendChild(cellTd(c.cells[g.key])));
      t.appendChild(tr);
    });
    box.appendChild(t);
  }

  function renderMatrix() {
    const box = $("dash-matrix");
    box.innerHTML = "";
    if (!DATA.matrix.length) { box.appendChild(el("p", "No runs match these filters.", "hint")); return; }
    const t = el("table", undefined, "results-table matrix");
    const h = el("tr");
    ["Problem", "Purpose"].forEach((x) => h.appendChild(el("th", x)));
    DATA.groups.forEach((g) => h.appendChild(colHead(g)));
    t.appendChild(h);
    let last = null;
    DATA.matrix.forEach((row) => {
      if (row.category !== last) {
        last = row.category;
        const gr = el("tr", undefined, "grp");
        const td = el("td", row.category); td.colSpan = 2 + DATA.groups.length;
        gr.appendChild(td); t.appendChild(gr);
      }
      const tr = el("tr");
      const name = el("td", undefined, "r-problem");
      name.appendChild(linkButton(row.problem, () => addFilter("problem_id", row.problem), "show only this problem"));
      tr.appendChild(name);
      tr.appendChild(el("td", row.purposes.join(", "), "tag"));
      DATA.groups.forEach((g) => tr.appendChild(cellTd(row.cells[g.key])));
      t.appendChild(tr);
    });
    box.appendChild(t);
  }

  function addFilter(key, value) {
    const set = new Set(F[key] || []); set.add(value); F[key] = [...set];
    saveFilters(); selected.clear(); schedule(0);
    $("dash-filters").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderTags() {
    const box = $("dash-tags");
    box.innerHTML = "";
    if (!DATA.tags.length) { box.appendChild(el("p", "No failures in view.", "hint")); return; }
    const max = Math.max(...DATA.tags.map((t) => t.n));
    DATA.tags.forEach((t) => {
      const row = el("div", undefined, "bar-row");
      const l = linkButton(t.tag, () => addFilter("failure_tag", t.tag), "show only runs with this tag");
      l.classList.add("bar-l");
      row.appendChild(l);
      const track = el("div", undefined, "bar-track");
      const fill = el("span", undefined, "bar-fill" + (t.tag === "(untagged)" ? " untagged" : ""));
      fill.style.width = (t.n / max * 100) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el("span", String(t.n), "bar-n"));
      box.appendChild(row);
    });
  }

  function renderTimeline() {
    const box = $("dash-timeline");
    box.innerHTML = "";
    const tl = DATA.timeline;
    if (!tl.length) { box.appendChild(el("p", "No runs in view.", "hint")); return; }
    const W = 360, H = 120, pad = 18, n = tl.length;
    const max = Math.max(...tl.map((d) => d.pass + d.fail + d.incomplete));
    const bw = Math.min(28, (W - 2 * pad) / n - 3);
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "timeline");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `Runs per day, ${tl[0].day} to ${tl[n - 1].day}: passed, failed and incomplete`);
    tl.forEach((d, i) => {
      const x = pad + i * ((W - 2 * pad) / n) + 1.5;
      let y = H - 20;
      [["pass", "t-pass"], ["fail", "t-fail"], ["incomplete", "t-inc"]].forEach(([k, cls]) => {
        const h = d[k] / max * (H - 40);
        if (!h) return;
        const r = document.createElementNS(ns, "rect");
        r.setAttribute("x", x); r.setAttribute("y", y - h); r.setAttribute("width", Math.max(2, bw)); r.setAttribute("height", h);
        r.setAttribute("class", cls);
        const tt = document.createElementNS(ns, "title"); tt.textContent = `${d.day}: ${d.pass} passed, ${d.fail} failed, ${d.incomplete} incomplete`;
        r.appendChild(tt);
        svg.appendChild(r);
        y -= h;
      });
      if (n <= 8 || i === 0 || i === n - 1) {
        const t = document.createElementNS(ns, "text");
        t.setAttribute("x", x + bw / 2); t.setAttribute("y", H - 5); t.setAttribute("text-anchor", "middle");
        t.textContent = d.day.slice(5); svg.appendChild(t);
      }
    });
    box.appendChild(svg);
    const legend = el("div", undefined, "legend");
    [["t-pass", "passed"], ["t-fail", "failed"], ["t-inc", "incomplete"]].forEach(([c, l]) => {
      const s = el("span"); s.appendChild(el("i", undefined, "sw " + c)); s.appendChild(document.createTextNode(l)); legend.appendChild(s);
    });
    box.appendChild(legend);
  }

  // ---- runs table --------------------------------------------------------------
  function purposeCell(r) {
    const td = el("td", undefined, "r-purpose");
    const sel = document.createElement("select");
    sel.className = "purpose-sel p-" + r.purpose + (r.purpose_source === "marked" ? " marked" : "");
    [["experiment", "experiment"], ["dev", "dev"], ["final", "final"]].forEach(([v, l]) => sel.appendChild(new Option(l, v)));
    sel.appendChild(new Option("↺ automatic", "__auto"));
    sel.value = r.purpose;
    sel.title = r.purpose_source === "marked" ? "set by a person; choose ‘automatic’ to clear" : "automatic, from the pinned sets; choose a value to override";
    sel.addEventListener("change", async () => {
      await mark([r.run_id], sel.value === "__auto" ? "" : sel.value);
    });
    td.appendChild(sel);
    return td;
  }

  function textButton(r) {
    const td = el("td");
    if (!r.has_text) { td.textContent = "—"; td.title = "no diagnosis text stored for this run"; return td; }
    td.appendChild(linkButton("text", async () => {
      showDetail(`${r.problem_id} — diagnosis and judge verdict`, "loading…");
      try {
        const full = await jsonGet("/api/run/" + encodeURIComponent(r.run_id));
        showDetail(`${full.problem_id} — diagnosis and judge verdict`, diagnosisDetail(full), window.buildReviewForm ? window.buildReviewForm(full) : null);
      } catch (e) { showDetail(r.problem_id, "could not load this run"); }
    }, "what the agent submitted and how the judge scored it, plus a form to tag the failure"));
    return td;
  }

  function renderRuns() {
    const tbody = $("results-tbody");
    tbody.innerHTML = "";
    const runs = DATA.runs;
    $("runs-count").textContent = `${runs.length} shown`;
    $("results-empty").hidden = runs.length > 0;
    const more = $("runs-more");
    more.hidden = !DATA.runs_truncated;
    if (DATA.runs_truncated) more.textContent = "Only the newest runs are listed here. Narrow the filters to see the rest; the numbers above use every run in view.";
    const live = new Set(runs.map((r) => r.run_id));
    [...selected].forEach((id) => { if (!live.has(id)) selected.delete(id); });
    runs.forEach((r) => {
      const tr = el("tr");
      const cbTd = el("td");
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.checked = selected.has(r.run_id); cb.setAttribute("aria-label", "select run " + r.problem_id);
      cb.addEventListener("change", () => { if (cb.checked) selected.add(r.run_id); else selected.delete(r.run_id); bulkState(); });
      cbTd.appendChild(cb); tr.appendChild(cbTd);
      tr.appendChild(el("td", fmtWhen(r.timestamp_utc)));
      tr.appendChild(el("td", r.run_by || "unknown"));
      const prob = el("td", undefined, "r-problem");
      const label = r.repeat_n > 1 ? `${r.problem_id} (${r.repeat_index}/${r.repeat_n})` : (r.problem_id || "—");
      prob.appendChild(linkButton(label, () => { if (window.openProblem) window.openProblem(r.problem_id); }, "open this problem's card and attempt log"));
      tr.appendChild(prob);
      tr.appendChild(el("td", r.category || "—", "tag"));
      tr.appendChild(purposeCell(r));
      tr.appendChild(el("td", `${r.agent_id || "—"}${r.action_space ? " · " + r.action_space : ""}`));
      tr.appendChild(el("td", r.model_id || "—"));
      tr.appendChild(el("td", r.experiment || "—"));
      const lab = el("td", (r.config_label || "—") + (r.run_class === "frozen" ? " ❄" : ""));
      lab.title = r.run_class === "frozen" ? "frozen run" : "exploration run";
      tr.appendChild(lab);
      tr.appendChild(passCell(r.diagnosis_pass));
      tr.appendChild(passCell(r.mitigation_pass));
      tr.appendChild(passCell(r.e2e_pass));
      tr.appendChild(timeCell(r.ttd_s));
      tr.appendChild(timeCell(r.ttm_s));
      const why = el("td", r.incomplete_reason || "—", r.incomplete_reason ? "r-why" : "");
      if (r.incomplete_reason) why.title = r.incomplete_reason;
      tr.appendChild(why);
      tr.appendChild(el("td", r.failure_mode_tag ? r.failure_mode_tag + (r.first_bad_step ? ` @${r.first_bad_step}` : "") : "—"));
      tr.appendChild(textButton(r));
      tr.appendChild(storyCell(r));
      tr.appendChild(logCell(r.log_file));
      tbody.appendChild(tr);
    });
    $("runs-all").checked = false;
    bulkState();
  }

  function bulkState() {
    $("bulk-count").textContent = selected.size ? `${selected.size} selected` : "none selected";
    $("bulk-apply").disabled = selected.size === 0;
  }

  async function mark(ids, purpose) {
    const { ok, data } = await jsonPost("/api/marks", { run_ids: ids, purpose: purpose || null, marked_by: who() });
    if (!ok) { alert(data.error || "could not save the mark"); return; }
    selected.clear();
    schedule(0);
  }

  // ---- wiring -------------------------------------------------------------------
  $("sum-metric").addEventListener("change", () => schedule(0));
  $("dash-refresh").addEventListener("click", () => load());
  $("dash-report").addEventListener("click", async () => {
    const r = await fetch("/api/report.md", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filters: F, metric: metric() }) });
    if (!r.ok) { alert("could not build the report"); return; }
    const blob = new Blob([await r.text()], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "reliops_results_report.md";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  $("runs-all").addEventListener("change", (e) => {
    selected.clear();
    if (e.target.checked) DATA.runs.forEach((r) => selected.add(r.run_id));
    document.querySelectorAll("#results-tbody input[type=checkbox]").forEach((c) => { c.checked = e.target.checked; });
    bulkState();
  });
  $("bulk-apply").addEventListener("click", () => mark([...selected], $("bulk-purpose").value));
  document.addEventListener("click", (e) => {
    if (!e.target.closest("details.msel")) document.querySelectorAll("details.msel[open]").forEach((d) => { d.open = false; });
  });

  TAB_LOADERS.results = () => load();

  // Other code (saving a failure tag, a finished job) calls loadResults() to refresh; keep that working.
  const classicReload = window.loadResults;
  window.loadResults = async function () {
    if (classicReload) await classicReload();
    if (!$("tab-results").hidden) load();
  };
})();
