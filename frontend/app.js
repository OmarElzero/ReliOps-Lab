const logEl = document.getElementById("log");
const fbStatusEl = document.getElementById("fb-status");
const runByEl = document.getElementById("run-by");

function appendLog(line) {
  logEl.textContent += line;
  logEl.scrollTop = logEl.scrollHeight;
}

function setFbStatus(text) {
  fbStatusEl.textContent = "Firebase: " + text;
  fbStatusEl.className = "pill" + (text.startsWith("connected") ? " ok" : /failed|error/i.test(text) ? " warn" : "");
}

// "Your name" is a per-viewer convenience, not an identity: just labels Firestore rows so a
// shared-key team can tell whose run is whose. Kept in this browser's localStorage only — never
// sent anywhere except as the plain "run_by" field on /api/run.
try {
  runByEl.value = localStorage.getItem("reliops_lab_run_by") || "";
} catch (e) { /* private window or blocked storage — just start blank */ }
runByEl.addEventListener("change", () => {
  try { localStorage.setItem("reliops_lab_run_by", runByEl.value.trim()); } catch (e) { /* ignore */ }
});

let CONFIG = null;

async function loadConfig() {
  const res = await fetch("/api/config");
  CONFIG = await res.json();

  const agentSel = document.getElementById("agent");
  agentSel.innerHTML = "";
  CONFIG.agents.forEach((a, i) => {
    const opt = document.createElement("option");
    opt.value = i;
    opt.textContent = a.label;
    agentSel.appendChild(opt);
  });
  renderAgentDesc();

  const presets = document.getElementById("model-presets");
  presets.innerHTML = "";
  CONFIG.model_presets.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m;
    presets.appendChild(opt);
  });

  const setSel = document.getElementById("problem-set");
  setSel.innerHTML = "";
  CONFIG.problem_sets.forEach((s, i) => {
    const opt = document.createElement("option");
    opt.value = i;
    opt.textContent = s.label;
    setSel.appendChild(opt);
  });
  renderProblemSet();

  renderKeyStatus();

  setFbStatus(CONFIG.firebase_status);
  appendLog(`\n[Firebase: ${CONFIG.firebase_status}]\n`);
  _lastFbStatus = CONFIG.firebase_status;
  document.getElementById("reliops-dir-hint").textContent =
    "ReliOps clone: " + CONFIG.reliops_dir + " (created automatically on first Setup run)";
}

// Firebase has no "Connect" step any more — this project's own .env is the only source (see
// firebase_sync.py / runner.lab_env_path()). Poll its status periodically so saving a key there
// is picked up on its own within a few seconds, with no page reload needed.
let _lastFbStatus = null;
async function pollFirebaseStatus() {
  try {
    const res = await fetch("/api/firebase-status");
    const data = await res.json();
    setFbStatus(data.status);
    if (data.status !== _lastFbStatus) {
      appendLog(`\n[Firebase: ${data.status}]\n`);
      _lastFbStatus = data.status;
    }
  } catch (e) { /* server not up yet, or a blip — next poll will retry */ }
}
setInterval(pollFirebaseStatus, 10000);

function renderAgentDesc() {
  const agent = selectedAgent();
  document.getElementById("agent-desc").textContent = agent && agent.desc ? agent.desc : "";
}

function selectedProvider() {
  const sel = document.getElementById("key-provider").value;
  if (sel === "__custom__") {
    return document.getElementById("key-provider-custom").value.trim().toUpperCase();
  }
  return sel;
}

function renderKeyStatus() {
  const isCustom = document.getElementById("key-provider").value === "__custom__";
  document.getElementById("key-provider-custom").hidden = !isCustom;
  const provider = selectedProvider();
  const statusEl = document.getElementById("key-status");
  const customSaved = CONFIG && CONFIG.custom_saved_keys ? CONFIG.custom_saved_keys : [];

  if (isCustom && !provider) {
    statusEl.textContent = customSaved.length
      ? `Type the env var name this key should be saved under. Already saved: ${customSaved.join(", ")}.`
      : "Type the env var name this key should be saved under.";
    return;
  }
  const saved = (CONFIG && CONFIG.saved_providers && CONFIG.saved_providers[provider]) || customSaved.includes(provider);
  statusEl.textContent = saved
    ? `${provider} already has a key saved — Save overwrites it.`
    : `${provider} has no key saved yet.`;
}

function renderProblemSet() {
  const idx = Number(document.getElementById("problem-set").value);
  const set = CONFIG.problem_sets[idx];
  const groupsEl = document.getElementById("problem-groups");
  const customWrap = document.getElementById("custom-problem-wrap");
  const toolbar = document.querySelector(".problem-toolbar");
  groupsEl.innerHTML = "";

  if (set.id === "custom") {
    customWrap.style.display = "block";
    groupsEl.style.display = "none";
    toolbar.style.display = "none";
    return;
  }
  customWrap.style.display = "none";
  groupsEl.style.display = "block";
  toolbar.style.display = "flex";

  // Group by category, in first-seen order, numbered by the set's own `n` (testing order).
  const byCategory = new Map();
  set.problems.forEach((p) => {
    if (!byCategory.has(p.category)) byCategory.set(p.category, []);
    byCategory.get(p.category).push(p);
  });

  byCategory.forEach((problems, category) => {
    const fieldset = document.createElement("fieldset");
    fieldset.className = "problem-group";
    const legend = document.createElement("legend");
    legend.textContent = `${category} (${problems.length})`;
    fieldset.appendChild(legend);

    problems.forEach((p) => {
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = p.id;
      cb.checked = true;
      label.appendChild(cb);
      const n = document.createElement("span");
      n.className = "problem-n";
      n.textContent = p.n;
      label.appendChild(n);
      label.appendChild(document.createTextNode(p.id));
      if (p.difficulty) {
        const diff = document.createElement("span");
        diff.className = "problem-diff diff-" + p.difficulty;
        diff.textContent = p.difficulty;
        label.appendChild(diff);
      }
      fieldset.appendChild(label);
    });
    groupsEl.appendChild(fieldset);
  });
}

function selectedAgent() {
  return CONFIG.agents[Number(document.getElementById("agent").value)];
}

function selectedProblems() {
  const idx = Number(document.getElementById("problem-set").value);
  const set = CONFIG.problem_sets[idx];
  if (set.id === "custom") {
    const v = document.getElementById("custom-problem").value.trim();
    return v ? [v] : [];
  }
  return Array.from(document.querySelectorAll("#problem-groups input:checked")).map((cb) => cb.value);
}

function streamFrom(url, onLine) {
  return new Promise((resolve) => {
    const es = new EventSource(url);
    es.onmessage = (ev) => {
      if (ev.data === "[[DONE]]") {
        es.close();
        resolve();
        return;
      }
      const line = JSON.parse(ev.data);
      appendLog(line);
      if (onLine) onLine(line);
    };
    es.onerror = () => {
      appendLog("\n[stream ended or connection lost]\n");
      es.close();
      resolve();
    };
  });
}

function fmtElapsed(ms) {
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function chunk(arr, size) {
  if (!size || size <= 0 || size >= arr.length) return [arr];
  const out = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

document.getElementById("problem-set").addEventListener("change", renderProblemSet);
document.getElementById("agent").addEventListener("change", renderAgentDesc);
document.getElementById("key-provider").addEventListener("change", renderKeyStatus);
document.getElementById("key-provider-custom").addEventListener("input", renderKeyStatus);

document.getElementById("select-all").addEventListener("click", () => {
  document.querySelectorAll("#problem-groups input[type=checkbox]").forEach((cb) => (cb.checked = true));
});
document.getElementById("select-none").addEventListener("click", () => {
  document.querySelectorAll("#problem-groups input[type=checkbox]").forEach((cb) => (cb.checked = false));
});

document.getElementById("run-doctor").addEventListener("click", async () => {
  const verdictEl = document.getElementById("doctor-verdict");
  const detailsEl = document.getElementById("doctor-details");
  const notesEl = document.getElementById("doctor-notes");
  verdictEl.textContent = "checking…";
  verdictEl.className = "pill";
  const res = await fetch("/api/doctor");
  const d = await res.json();

  verdictEl.textContent = d.verdict;
  verdictEl.className = "pill" + (/blocked/.test(d.verdict) ? " warn" : /struggle/.test(d.verdict) ? " warn" : " ok");

  detailsEl.hidden = false;
  detailsEl.innerHTML = "";
  const rows = [
    ["CPU cores", d.cpu_cores ?? "unknown"],
    ["RAM", d.ram_gb != null ? d.ram_gb + " GB" : "unknown"],
    ["Free disk", d.disk_free_gb != null ? d.disk_free_gb + " GB" : "unknown"],
    ["Docker", d.docker_running ? "running" : "not running"],
    ["kubectl", d.tools.kubectl || "missing"],
    ["helm", d.tools.helm || "missing"],
    ["kind", d.tools.kind || "missing"],
    ["uv", d.tools.uv || "missing"],
  ];
  rows.forEach(([label, value]) => {
    const dt = document.createElement("div");
    dt.className = "doctor-k";
    dt.textContent = label;
    const dd = document.createElement("div");
    dd.className = "doctor-v";
    dd.textContent = value;
    detailsEl.appendChild(dt);
    detailsEl.appendChild(dd);
  });

  notesEl.innerHTML = "";
  (d.notes || []).forEach((n) => {
    const li = document.createElement("li");
    li.textContent = n;
    notesEl.appendChild(li);
  });

  appendLog(`\n[machine check: ${d.verdict}]\n`);
});

document.getElementById("save-key").addEventListener("click", async () => {
  const isCustom = document.getElementById("key-provider").value === "__custom__";
  const provider = selectedProvider();
  const value = document.getElementById("key-value").value.trim();
  if (!provider) {
    appendLog("\n[pick a provider, or type a name in the Custom… field, first]\n");
    return;
  }
  if (!value) return;
  const res = await fetch("/api/key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, value }),
  });
  const data = await res.json();
  document.getElementById("key-value").value = "";
  if (data.ok) {
    appendLog(`\n[${data.message}]\n`);
    if (CONFIG) {
      if (isCustom) {
        CONFIG.custom_saved_keys = CONFIG.custom_saved_keys || [];
        if (!CONFIG.custom_saved_keys.includes(provider)) CONFIG.custom_saved_keys.push(provider);
      } else {
        CONFIG.saved_providers = CONFIG.saved_providers || {};
        CONFIG.saved_providers[provider] = true;
      }
      renderKeyStatus();
    }
  } else {
    appendLog(`\n[ERROR: ${data.message}]\n`);
  }
});

document.getElementById("run-setup").addEventListener("click", () => {
  appendLog("\n[starting setup — clone + patches + cluster build; first run can take ~10 min]\n");
  streamFrom("/api/setup");
});

document.getElementById("run-demo").addEventListener("click", () => {
  appendLog("\n[starting demo mode — simulated output, no cluster needed]\n");
  streamFrom("/api/demo");
});

document.getElementById("run-test").addEventListener("click", async () => {
  const agent = selectedAgent();
  const model = document.getElementById("model").value.trim();
  const judgeModel = document.getElementById("judge-model").value.trim();
  const problems = selectedProblems();
  const forceBuild = document.getElementById("force-build").checked;
  const runBy = runByEl.value.trim();
  const batchSize = Number(document.getElementById("batch-size").value) || 0;
  const repeatN = Math.max(1, Number(document.getElementById("repeat-n").value) || 1);

  if (!problems.length) {
    appendLog("\n[pick at least one problem first]\n");
    return;
  }

  const batches = chunk(problems, batchSize);
  const runButton = document.getElementById("run-test");
  runButton.disabled = true;
  runButton.textContent = "Running…";

  // One row per problem (not one bar for the whole run) — a problem can take 30+ minutes, and a
  // single aggregate percentage can't tell "the whole run is stuck" apart from "problem #7 is
  // just slow". Each row tracks its own repeat count via runner.stream_campaign()'s per-run
  // "===== [i/N] problem=... repeat=r/R =====" banner and "recorded result for ..." line.
  const progressWrap = document.getElementById("progress-wrap");
  const summaryEl = document.getElementById("progress-summary");
  const rowsEl = document.getElementById("progress-rows");
  progressWrap.hidden = false;
  rowsEl.innerHTML = "";
  const rows = new Map();
  problems.forEach((p) => {
    const row = document.createElement("div");
    row.className = "progress-row";
    row.innerHTML = '<span class="p-dot"></span><span class="p-name"></span>'
      + '<span class="p-state">queued</span><span class="p-time"></span>';
    row.querySelector(".p-name").textContent = p;
    rowsEl.appendChild(row);
    rows.set(p, {
      el: row,
      stateEl: row.querySelector(".p-state"),
      timeEl: row.querySelector(".p-time"),
      startedAt: null,
      completed: 0,
    });
  });

  let totalCompleted = 0;
  const totalRuns = problems.length * repeatN;
  const updateSummary = () => { summaryEl.textContent = `${totalCompleted} of ${totalRuns} run(s) done`; };
  updateSummary();

  const tickInterval = setInterval(() => {
    rows.forEach((r) => {
      if (r.startedAt) r.timeEl.textContent = fmtElapsed(Date.now() - r.startedAt);
    });
  }, 1000);

  const startedRe = /\[(\d+)\/(\d+)\] problem=(\S+)(?: repeat=(\d+)\/(\d+))?/;
  const doneRe = /recorded result for (\S+): (parsed row found|no row parsed[^(]*)/;
  const onLine = (line) => {
    const started = startedRe.exec(line);
    if (started) {
      const [, , , problem, rep, repTotal] = started;
      const r = rows.get(problem);
      if (r) {
        r.el.className = "progress-row running";
        r.startedAt = Date.now();
        r.stateEl.textContent = rep ? `running ${rep}/${repTotal}` : "running";
      }
      return;
    }
    const done = doneRe.exec(line);
    if (done) {
      const [, problem, note] = done;
      const r = rows.get(problem);
      totalCompleted += 1;
      updateSummary();
      if (!r) return;
      r.completed += 1;
      r.startedAt = null;
      const ok = note.startsWith("parsed row found");
      if (r.completed >= repeatN) {
        r.el.className = "progress-row " + (ok ? "done" : "failed");
        r.stateEl.textContent = repeatN > 1 ? `done ${r.completed}/${repeatN}` : (ok ? "done" : "no row parsed");
        r.timeEl.textContent = "";
      } else {
        r.stateEl.textContent = `done ${r.completed}/${repeatN} — next…`;
      }
    }
  };

  for (let i = 0; i < batches.length; i++) {
    const batch = batches[i];
    const params = new URLSearchParams({
      agent: agent.id,
      model,
      judge_model: judgeModel,
      problems: batch.join(","),
      repeat_n: String(repeatN),
      force_build: forceBuild && i === 0 ? "true" : "false", // only the very first call needs to build the image
    });
    if (agent.action_space) params.set("action_space", agent.action_space);
    if (runBy) params.set("run_by", runBy);

    if (batches.length > 1) {
      appendLog(`\n===== batch ${i + 1}/${batches.length}: ${batch.length} problem(s) =====\n`);
    } else {
      appendLog(`\n[running ${batch.length} problem(s) with ${agent.label}]\n`);
    }
    await streamFrom("/api/run?" + params.toString(), onLine);
  }

  clearInterval(tickInterval);
  summaryEl.textContent = `Done: ${totalCompleted} of ${totalRuns} run(s)`;

  runButton.disabled = false;
  runButton.textContent = "Run test(s)";
  if (batches.length > 1) {
    appendLog(`\n===== all ${batches.length} batches done (${problems.length} problem(s) total) =====\n`);
  }
  // So the Results tab doesn't show stale data if the user flips over right after a run.
  loadResults();
});

document.getElementById("clear-log").addEventListener("click", () => {
  logEl.textContent = "";
});

// ---- Tabs ------------------------------------------------------------------
const tabRunBtn = document.getElementById("tab-btn-run");
const tabResultsBtn = document.getElementById("tab-btn-results");
const tabRunPanel = document.getElementById("tab-run");
const tabResultsPanel = document.getElementById("tab-results");
let resultsLoadedOnce = false;

function showTab(name) {
  const onResults = name === "results";
  tabRunPanel.hidden = onResults;
  tabResultsPanel.hidden = !onResults;
  tabRunBtn.classList.toggle("active", !onResults);
  tabResultsBtn.classList.toggle("active", onResults);
  if (onResults && !resultsLoadedOnce) {
    resultsLoadedOnce = true;
    loadResults();
  }
}
tabRunBtn.addEventListener("click", () => showTab("run"));
tabResultsBtn.addEventListener("click", () => showTab("results"));

// ---- Results dashboard ------------------------------------------------------
function fmtWhen(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch (e) { return iso; }
}

function passCell(value) {
  const td = document.createElement("td");
  if (value === true) { td.textContent = "✓ pass"; td.className = "r-pass"; }
  else if (value === false) { td.textContent = "✗ fail"; td.className = "r-fail"; }
  else { td.textContent = "—"; td.className = "r-unknown"; }
  return td;
}

function timeCell(seconds) {
  const td = document.createElement("td");
  td.textContent = typeof seconds === "number" ? `${seconds.toFixed(0)}s` : "—";
  return td;
}

async function loadResults() {
  const sourceEl = document.getElementById("results-source");
  const tbody = document.getElementById("results-tbody");
  const emptyEl = document.getElementById("results-empty");
  sourceEl.textContent = "loading…";
  let data;
  try {
    const res = await fetch("/api/results");
    data = await res.json();
  } catch (e) {
    sourceEl.textContent = "couldn't load results — is the server running?";
    return;
  }
  sourceEl.textContent = data.source === "firestore"
    ? "Showing the whole team's runs (via Firestore)."
    : "Showing this machine's own runs only — Firebase isn't connected, see the Run tab's Keys & Firebase section.";

  tbody.innerHTML = "";
  emptyEl.hidden = data.runs.length > 0;
  data.runs.forEach((r) => {
    const tr = document.createElement("tr");
    const when = document.createElement("td"); when.textContent = fmtWhen(r.timestamp_utc); tr.appendChild(when);
    const by = document.createElement("td"); by.textContent = r.run_by || "unknown"; tr.appendChild(by);
    const problem = document.createElement("td");
    problem.className = "r-problem";
    problem.textContent = r.repeat_n > 1 ? `${r.problem_id} (${r.repeat_index}/${r.repeat_n})` : (r.problem_id || "—");
    tr.appendChild(problem);
    const agent = document.createElement("td"); agent.textContent = r.agent_id || "—"; tr.appendChild(agent);
    const model = document.createElement("td"); model.textContent = r.model_id || "—"; tr.appendChild(model);
    tr.appendChild(passCell(r.diagnosis_pass));
    tr.appendChild(passCell(r.mitigation_pass));
    tr.appendChild(passCell(r.e2e_pass));
    tr.appendChild(timeCell(r.ttd_s));
    tr.appendChild(timeCell(r.ttm_s));
    tbody.appendChild(tr);
  });
}
document.getElementById("refresh-results").addEventListener("click", loadResults);

loadConfig();
