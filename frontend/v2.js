// ReliOps Lab v1.1 / v2 screens: failure tagging, standing audit, frozen campaigns, judge audit, problem cards.
// Loaded after app.js and uses its globals (CONFIG, showTab, showDetail, linkButton, el, pct, interval, ...).

const jsonGet = async (url) => (await fetch(url)).json();
const jsonPost = async (url, body) => {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return { ok: res.ok, data: await res.json() };
};
const who = () => (document.getElementById("run-by").value || "").trim();

// ---- Failure tagging (shown under a run's text / story) ------------------------------
let TAGS = null;
async function loadTags() {
  if (!TAGS) {
    try { TAGS = (await jsonGet("/api/failure_tags")).tags; } catch (e) { TAGS = []; }
  }
  return TAGS;
}

window.buildReviewForm = function (r) {
  const form = document.createElement("div");
  form.className = "review-form";
  const sel = document.createElement("select");
  sel.appendChild(new Option("failure tag…", ""));
  loadTags().then((tags) => {
    tags.forEach((t) => sel.appendChild(new Option(t, t)));
    sel.value = r.failure_mode_tag || "";
  });
  const step = document.createElement("input");
  step.type = "number"; step.min = "1"; step.placeholder = "first wrong step #"; step.style.width = "150px";
  if (r.first_bad_step) step.value = r.first_bad_step;
  const note = document.createElement("input");
  note.className = "note"; note.placeholder = "what went wrong (one line)"; note.value = r.review_note || "";
  const btn = document.createElement("button");
  btn.type = "button"; btn.className = "secondary small"; btn.textContent = "Save tag";
  const msg = document.createElement("span");
  msg.className = "hint";
  msg.textContent = r.reviewed_by ? `last tagged by ${r.reviewed_by}` : "";
  btn.addEventListener("click", async () => {
    const { ok, data } = await jsonPost("/api/review", {
      run_id: r.run_id, failure_mode_tag: sel.value, first_bad_step: step.value ? Number(step.value) : null,
      note: note.value.trim(), reviewer: who(),
    });
    msg.textContent = ok ? "saved" : (data.error || "could not save");
    if (ok) loadResults();
  });
  [sel, step, note, btn, msg].forEach((x) => form.appendChild(x));
  return form;
};

// ---- Standing audit ------------------------------------------------------------------
window.loadStanding = async function (metric) {
  const tbody = document.getElementById("audit-tbody");
  let data;
  try { data = await jsonGet("/api/standing_audit?metric=" + encodeURIComponent(metric)); } catch (e) { return; }
  tbody.innerHTML = "";
  data.rows.forEach((r) => {
    const tr = document.createElement("tr");
    const name = `${r.config_label !== "-" ? r.config_label : "(no label)"} · ${r.agent_id} · ${r.model_id} · ${r.run_class}`;
    const cells = [
      name,
      `${pct(r.rate)} (≥ ${pct(r.wilson_lo)}, n=${r.n})`,
      r.floor_rate === null ? "—" : `${pct(r.floor_rate)} (n=${r.floor_n})`,
      r.floor_status,
      r.control_runs ? `${r.control_passes}/${r.control_runs} passed` : "none yet",
    ];
    cells.forEach((t, i) => {
      const td = document.createElement("td");
      td.textContent = t;
      if (i === 0) td.className = "r-wrap";
      if (i === 3) td.className = r.floor_status === "clearly above floor" ? "r-pass" : "r-fail";
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  document.getElementById("audit-notes").textContent = (data.notes || []).join(" ");
};

// ---- Campaigns -------------------------------------------------------------------------
const campSelect = document.getElementById("camp-select");

async function loadManifests(keep) {
  let data;
  try { data = await jsonGet("/api/manifests"); } catch (e) { return; }
  const prev = keep || campSelect.value;
  campSelect.innerHTML = "";
  data.manifests.forEach((m) => campSelect.appendChild(new Option(`${m.name} · ${m.config_label} · ${m.model_id} (${m.problems.length} problems × ${m.repeat_n})`, m.manifest_id)));
  if (prev && data.manifests.some((m) => m.manifest_id === prev)) campSelect.value = prev;
  await loadGrid();
}

async function loadGrid() {
  const info = document.getElementById("camp-info");
  const grid = document.getElementById("camp-grid");
  grid.innerHTML = "";
  if (!campSelect.value) { info.textContent = "No frozen campaign yet. Freeze one above."; return; }
  const k = Math.max(1, Number(document.getElementById("camp-k").value) || 1);
  const n = Math.max(1, Number(document.getElementById("camp-n").value) || 1);
  const q = new URLSearchParams({ manifest_id: campSelect.value, k: String(k), n: String(n) });
  const g = await jsonGet("/api/campaign?" + q.toString());
  if (g.error) { info.textContent = g.error; return; }
  const c = g.counts, m = g.manifest;
  const warn = [];
  if (g.agent_dirty) warn.push("agent/ or scripts/ have uncommitted changes, runs will be refused");
  if (m.git_commit && g.current_commit && m.git_commit !== g.current_commit) warn.push(`this machine is at ${g.current_commit.slice(0, 8)} but the campaign was frozen at ${m.git_commit.slice(0, 8)}, runs will be refused (git pull / checkout)`);
  info.textContent = `${c.pass} pass · ${c.fail} fail · ${c.incomplete} incomplete (will re-run) · ${c.missing} missing. ` +
    `Your slice ${k}/${n}: ${g.slice.cells.length} cell(s). ` +
    (g.summary && g.summary.n ? `So far ${g.summary.passes}/${g.summary.n} (Wilson ${pct(g.summary.wilson_lo)}–${pct(g.summary.wilson_hi)}). ` : "") +
    (warn.length ? "WARNING: " + warn.join("; ") + "." : "");
  const mine = new Set(g.slice.cells.map((x) => x.problem + "\u0000" + x.repeat));
  const byProblem = new Map();
  g.cells.forEach((cell) => { if (!byProblem.has(cell.problem)) byProblem.set(cell.problem, []); byProblem.get(cell.problem).push(cell); });
  byProblem.forEach((cells, problem) => {
    const row = document.createElement("div");
    row.className = "camp-row";
    row.appendChild(el("span", problem, "camp-name"));
    cells.forEach((cell) => {
      const d = el("span", { pass: "✓", fail: "✗", incomplete: "?", missing: "·" }[cell.status], `cell ${cell.status}` + (mine.has(problem + "\u0000" + cell.repeat) ? " mine" : ""));
      d.title = `${problem} repeat ${cell.repeat}: ${cell.status}`;
      row.appendChild(d);
    });
    grid.appendChild(row);
  });
}

document.getElementById("freeze-go").addEventListener("click", async () => {
  const msg = document.getElementById("freeze-msg");
  const agent = selectedAgent();
  const body = {
    name: document.getElementById("freeze-name").value.trim(),
    agent_id: agent.id,
    model_id: document.getElementById("model").value.trim(),
    action_space: agent.action_space || null,
    judge_model_id: document.getElementById("judge-model").value.trim(),
    config_label: document.getElementById("config-label").value.trim(),
    problems: selectedProblems(),
    repeat_n: Math.max(1, Number(document.getElementById("repeat-n").value) || 1),
    created_by: who(),
  };
  const { ok, data } = await jsonPost("/api/manifest", body);
  if (!ok) { msg.textContent = data.error || "could not freeze"; return; }
  msg.textContent = `Frozen as ${data.manifest.manifest_id} at commit ${(data.manifest.git_commit || "?").slice(0, 8)} (${data.manifest.problems.length} problems × ${data.manifest.repeat_n}).`;
  await loadManifests(data.manifest.manifest_id);
});
document.getElementById("camp-refresh").addEventListener("click", () => loadManifests());
campSelect.addEventListener("change", loadGrid);
document.getElementById("camp-k").addEventListener("change", loadGrid);
document.getElementById("camp-n").addEventListener("change", loadGrid);

document.getElementById("camp-run").addEventListener("click", async () => {
  if (!campSelect.value) return;
  const agent = selectedAgent();
  const params = new URLSearchParams({
    agent: agent.id,
    model: document.getElementById("model").value.trim(),
    judge_model: document.getElementById("judge-model").value.trim(),
    problems: "x",  // ignored: the manifest decides the problems
    manifest_id: campSelect.value,
    slice_k: document.getElementById("camp-k").value || "1",
    slice_n: document.getElementById("camp-n").value || "1",
    config_label: document.getElementById("config-label").value.trim(),
    run_class: "frozen",
    record_walkthrough: document.getElementById("record-walkthrough").checked ? "true" : "false",
  });
  if (agent.action_space) params.set("action_space", agent.action_space);
  if (who()) params.set("run_by", who());
  showTab("run");
  appendLog("\n[running the missing cells of the frozen campaign]\n");
  await streamFrom("/api/run?" + params.toString());
  loadResults();
});
TAB_LOADERS.campaigns = () => loadManifests();

// ---- Judge audit -------------------------------------------------------------------------
let labelSkip = 0;  // how many items the labeller has skipped this session (rotates through the queue)
async function loadLabelQueue() {
  const box = document.getElementById("label-box");
  box.innerHTML = "";
  let data;
  try { data = await jsonGet("/api/label_queue?limit=50"); } catch (e) { box.textContent = "could not load the queue"; return; }
  if (!data.queue.length) {
    box.appendChild(el("p", `Nothing left to label (${data.labeled} labelled so far). Runs show up here once they have a stored diagnosis text.`, "hint"));
    return;
  }
  const item = data.queue[labelSkip % data.queue.length];
  box.appendChild(el("p", `${data.queue.length} waiting · ${data.labeled} labelled`, "hint"));
  box.appendChild(el("div", item.problem_id, "cfg-label"));
  box.appendChild(el("div", item.diagnosis_text, "label-text"));
  box.appendChild(el("div", item.expected || "No problem card for this problem yet, so there is no written description of a correct diagnosis. Label against the problem's rubric or skip it.", "label-expected"));
  const row = document.createElement("div");
  row.className = "row"; row.style.marginTop = "10px";
  const mk = (text, cls, fn) => { const b = el("button", text, cls); b.type = "button"; b.addEventListener("click", fn); return b; };
  const send = async (correct) => { await jsonPost("/api/label", { run_id: item.run_id, correct, labeler: who() }); loadLabelQueue(); loadJudgeAudit(); };
  row.appendChild(mk("Correct", "primary", () => send(true)));
  row.appendChild(mk("Incorrect", "secondary", () => send(false)));
  if (data.queue.length > 1) {
    row.appendChild(mk("Skip", "secondary", () => { labelSkip += 1; loadLabelQueue(); }));
  }
  box.appendChild(row);
}

async function loadJudgeAudit() {
  let a;
  try { a = await jsonGet("/api/judge_audit"); } catch (e) { return; }
  const kap = (x) => (typeof x === "number" ? x.toFixed(2) : "—");
  const tb = document.getElementById("judge-tbody");
  tb.innerHTML = "";
  a.judges.forEach((j) => {
    const tr = document.createElement("tr");
    [j.judge, String(j.judged), String(j.labeled_overlap),
     j.agreement === null ? "—" : `${j.agree}/${j.labeled_overlap} (${pct(j.agreement)})`,
     j.agreement_wilson ? interval(j.agreement_wilson[0], j.agreement_wilson[1]) : "—", kap(j.kappa),
     `${j.tp} / ${j.fp} / ${j.tn} / ${j.fn}`].forEach((t) => tr.appendChild(el("td", t)));
    tb.appendChild(tr);
  });
  const pb = document.getElementById("pair-tbody");
  pb.innerHTML = "";
  a.pairs.forEach((p) => {
    const tr = document.createElement("tr");
    [p.a, p.b, String(p.n), `${p.agree}/${p.n}`, kap(p.kappa)].forEach((t) => tr.appendChild(el("td", t)));
    pb.appendChild(tr);
  });
  document.getElementById("judge-notes").textContent =
    `${a.labeled} hand label(s). ` + (a.warnings || []).join(" ") +
    " TP/FP/TN/FN count the judge's 'correct' verdicts against the human label.";
}

document.getElementById("rejudge-go").addEventListener("click", () => {
  const model = document.getElementById("rejudge-model").value.trim();
  const log = document.getElementById("rejudge-log");
  if (!model) { log.textContent = "type the judge model first"; return; }
  log.textContent = "";
  const es = new EventSource("/api/rejudge?" + new URLSearchParams({ judge_model: model, scope: document.getElementById("rejudge-scope").value }).toString());
  es.onmessage = (ev) => {
    if (ev.data === "[[DONE]]") { es.close(); loadJudgeAudit(); return; }
    log.textContent += JSON.parse(ev.data);
    log.scrollTop = log.scrollHeight;
  };
  es.onerror = () => { log.textContent += "\n[stream ended]\n"; es.close(); loadJudgeAudit(); };
});
TAB_LOADERS.audit = () => { loadLabelQueue(); loadJudgeAudit(); };

// ---- Problem cards -------------------------------------------------------------------------
const cardSelect = document.getElementById("card-select");
let CARDS = [];
const scriptCache = {};
function loadScript(url) {
  if (!scriptCache[url]) {
    scriptCache[url] = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = url; s.onload = resolve; s.onerror = () => reject(new Error("could not load " + url));
      document.head.appendChild(s);
    });
  }
  return scriptCache[url];
}

function sanitizeHtml(html) {
  // Cards come from the team's own repo, but never run script from a rendered file anyway.
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll("script, iframe, object, embed, style, link").forEach((n) => n.remove());
  doc.querySelectorAll("*").forEach((n) => {
    Array.from(n.attributes).forEach((a) => {
      if (a.name.startsWith("on") || (["href", "src"].includes(a.name) && /^\s*javascript:/i.test(a.value))) n.removeAttribute(a.name);
    });
  });
  return doc.body.innerHTML;
}

async function renderCard(text) {
  const body = document.getElementById("card-body");
  try {
    await loadScript("https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js");
    body.innerHTML = sanitizeHtml(window.marked.parse(text));
  } catch (e) {
    body.innerHTML = "";
    body.appendChild(el("p", "Showing the raw Markdown (the renderer could not be loaded; it needs internet access).", "hint"));
    body.appendChild(el("pre", text));
    return;
  }
  const blocks = Array.from(body.querySelectorAll("pre code.language-mermaid"));
  if (!blocks.length) return;
  try {
    await loadScript("https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js");
    window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "default" });
    const nodes = blocks.map((b) => { const d = document.createElement("div"); d.className = "mermaid"; d.textContent = b.textContent; b.parentElement.replaceWith(d); return d; });
    await window.mermaid.run({ nodes });
  } catch (e) { /* the diagram stays as readable source */ }
}

async function showCard(problemId) {
  const body = document.getElementById("card-body");
  body.textContent = "loading…";
  const res = await fetch("/api/card/" + encodeURIComponent(problemId));
  const text = await res.text();
  if (!res.ok) { body.innerHTML = ""; body.appendChild(el("p", text, "hint")); }
  else await renderCard(text);
  loadAttempts(problemId);
}

async function loadAttempts(problemId) {
  const tb = document.getElementById("attempt-tbody");
  tb.innerHTML = "";
  let data;
  try { data = await jsonGet("/api/results?limit=5000"); } catch (e) { return; }
  data.runs.filter((r) => r.problem_id === problemId).forEach((r) => {
    const tr = document.createElement("tr");
    tr.appendChild(el("td", fmtWhen(r.timestamp_utc)));
    tr.appendChild(el("td", (r.config_label || "—") + (r.run_class === "frozen" ? " ❄" : "")));
    tr.appendChild(el("td", r.model_id || "—"));
    tr.appendChild(passCell(r.diagnosis_pass));
    tr.appendChild(passCell(r.e2e_pass));
    tr.appendChild(el("td", r.failure_mode_tag || "—"));
    tr.appendChild(el("td", r.first_bad_step ? String(r.first_bad_step) : "—"));
    tr.appendChild(textCell(r));
    tr.appendChild(storyCell(r));
    tb.appendChild(tr);
  });
  if (!tb.children.length) { const tr = document.createElement("tr"); tr.appendChild(el("td", "no recorded attempts yet")); tb.appendChild(tr); }
}

async function loadCardList(select) {
  let data;
  try { data = await jsonGet("/api/cards"); } catch (e) { return; }
  CARDS = data.cards;
  const have = new Set(CARDS.map((c) => c.problem_id));
  const all = new Set(have);
  (CONFIG ? CONFIG.problem_sets : []).forEach((s) => (s.problems || []).forEach((p) => all.add(p.id)));
  cardSelect.innerHTML = "";
  Array.from(all).sort((a, b) => (have.has(b) - have.has(a)) || a.localeCompare(b)).forEach((id) => {
    cardSelect.appendChild(new Option((have.has(id) ? "● " : "○ ") + id, id));
  });
  if (select && all.has(select)) cardSelect.value = select;
  if (cardSelect.value) await showCard(cardSelect.value);
}

cardSelect.addEventListener("change", () => showCard(cardSelect.value));
document.getElementById("card-refresh").addEventListener("click", () => loadCardList(cardSelect.value));
TAB_LOADERS.problems = () => loadCardList(cardSelect.value);
window.openProblem = (id) => { showTab("problems"); loadCardList(id); };
