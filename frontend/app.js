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
  document.getElementById("reliops-dir-hint").textContent =
    "ReliOps clone: " + CONFIG.reliops_dir + " (created automatically on first Setup run)";

  // Pre-fill with the suggested shared-key location (README's "Shared results" section) so saving
  // the downloaded file there is all that's needed — nothing to type. A path already typed or
  // remembered from last time (localStorage) wins over this default.
  const fbPathEl = document.getElementById("fb-path");
  if (!fbPathEl.value) {
    let saved = "";
    try { saved = localStorage.getItem("reliops_lab_fb_path") || ""; } catch (e) { /* ignore */ }
    fbPathEl.value = saved || CONFIG.default_firebase_key_path;
  }
}

function renderAgentDesc() {
  const agent = selectedAgent();
  document.getElementById("agent-desc").textContent = agent && agent.desc ? agent.desc : "";
}

function renderKeyStatus() {
  const provider = document.getElementById("key-provider").value;
  const saved = CONFIG && CONFIG.saved_providers && CONFIG.saved_providers[provider];
  document.getElementById("key-status").textContent = saved
    ? `${provider} already has a key saved in ReliOps/.env — Save overwrites it.`
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

function chunk(arr, size) {
  if (!size || size <= 0 || size >= arr.length) return [arr];
  const out = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

document.getElementById("problem-set").addEventListener("change", renderProblemSet);
document.getElementById("agent").addEventListener("change", renderAgentDesc);
document.getElementById("key-provider").addEventListener("change", renderKeyStatus);

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
  const provider = document.getElementById("key-provider").value;
  const value = document.getElementById("key-value").value.trim();
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
      CONFIG.saved_providers = CONFIG.saved_providers || {};
      CONFIG.saved_providers[provider] = true;
      renderKeyStatus();
    }
  } else {
    appendLog(`\n[ERROR: ${data.message}]\n`);
  }
});

document.getElementById("connect-fb").addEventListener("click", async () => {
  const service_account_path = document.getElementById("fb-path").value.trim();
  if (!service_account_path) return;
  try { localStorage.setItem("reliops_lab_fb_path", service_account_path); } catch (e) { /* ignore */ }
  appendLog(`\n[connecting to Firebase using ${service_account_path} ...]\n`);
  const res = await fetch("/api/firebase", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ service_account_path }),
  });
  const data = await res.json();
  setFbStatus(data.status);
  appendLog(`\n[Firebase: ${data.status}]\n`);
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

  if (!problems.length) {
    appendLog("\n[pick at least one problem first]\n");
    return;
  }

  const batches = chunk(problems, batchSize);
  const runButton = document.getElementById("run-test");
  runButton.disabled = true;
  runButton.textContent = "Running…";

  // Progress bar: SREGym runs one problem per process, so runner.stream_campaign() yields a
  // "===== [i/N] problem=... =====" banner when a problem starts and a "recorded result for
  // ..." line when it finishes. Both lines carry the batch's own local index, not a global one,
  // so track overall progress ourselves by counting "recorded result" lines across every batch.
  const total = problems.length;
  let completed = 0;
  const progressWrap = document.getElementById("progress-wrap");
  const progressFill = document.getElementById("progress-fill");
  const progressLabel = document.getElementById("progress-label");
  progressWrap.hidden = false;
  progressFill.className = "progress-fill";
  progressFill.style.width = "0%";
  progressLabel.textContent = `Starting — 0 of ${total}`;

  const startedRe = /\[(\d+)\/(\d+)\] problem=(\S+)/;
  const doneRe = /recorded result for (\S+):/;
  const onLine = (line) => {
    const started = startedRe.exec(line);
    if (started) {
      progressLabel.textContent = `Running ${completed + 1} of ${total} — ${started[3]}`;
      return;
    }
    const done = doneRe.exec(line);
    if (done) {
      completed += 1;
      progressFill.style.width = `${Math.round((completed / total) * 100)}%`;
      progressLabel.textContent = `Done ${completed} of ${total} — ${done[1]}`;
    }
  };

  for (let i = 0; i < batches.length; i++) {
    const batch = batches[i];
    const params = new URLSearchParams({
      agent: agent.id,
      model,
      judge_model: judgeModel,
      problems: batch.join(","),
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

  progressFill.style.width = "100%";
  progressFill.className = "progress-fill done";
  progressLabel.textContent = `Done: ${completed} of ${total}`;

  runButton.disabled = false;
  runButton.textContent = "Run test(s)";
  if (batches.length > 1) {
    appendLog(`\n===== all ${batches.length} batches done (${problems.length} problem(s) total) =====\n`);
  }
});

document.getElementById("clear-log").addEventListener("click", () => {
  logEl.textContent = "";
});

loadConfig();
