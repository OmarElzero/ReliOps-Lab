const logEl = document.getElementById("log");
const fbStatusEl = document.getElementById("fb-status");

function appendLog(line) {
  logEl.textContent += line;
  logEl.scrollTop = logEl.scrollHeight;
}

function setFbStatus(text) {
  fbStatusEl.textContent = "Firebase: " + text;
  fbStatusEl.className = "pill" + (text === "connected" ? " ok" : /failed|error/i.test(text) ? " warn" : "");
}

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

  setFbStatus(CONFIG.firebase_status);
  document.getElementById("reliops-dir-hint").textContent =
    "ReliOps clone: " + CONFIG.reliops_dir + " (created automatically on first Setup run)";
}

function renderProblemSet() {
  const idx = Number(document.getElementById("problem-set").value);
  const set = CONFIG.problem_sets[idx];
  const checklist = document.getElementById("problem-checks");
  const customWrap = document.getElementById("custom-problem-wrap");
  checklist.innerHTML = "";
  if (set.id === "custom") {
    customWrap.style.display = "block";
    checklist.style.display = "none";
    return;
  }
  customWrap.style.display = "none";
  checklist.style.display = "flex";
  set.problems.forEach((p) => {
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = p;
    cb.checked = true;
    label.appendChild(cb);
    label.appendChild(document.createTextNode(p));
    checklist.appendChild(label);
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
  return Array.from(document.querySelectorAll("#problem-checks input:checked")).map((cb) => cb.value);
}

function streamFrom(url) {
  const es = new EventSource(url);
  es.onmessage = (ev) => {
    if (ev.data === "[[DONE]]") {
      es.close();
      return;
    }
    appendLog(JSON.parse(ev.data));
  };
  es.onerror = () => {
    appendLog("\n[stream ended or connection lost]\n");
    es.close();
  };
}

document.getElementById("problem-set").addEventListener("change", renderProblemSet);

document.getElementById("save-key").addEventListener("click", async () => {
  const provider = document.getElementById("key-provider").value;
  const value = document.getElementById("key-value").value.trim();
  if (!value) return;
  await fetch("/api/key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, value }),
  });
  document.getElementById("key-value").value = "";
  appendLog(`\n[saved ${provider} to your local ReliOps/.env]\n`);
});

document.getElementById("connect-fb").addEventListener("click", async () => {
  const service_account_path = document.getElementById("fb-path").value.trim();
  if (!service_account_path) return;
  const res = await fetch("/api/firebase", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ service_account_path }),
  });
  const data = await res.json();
  setFbStatus(data.status);
});

document.getElementById("run-setup").addEventListener("click", () => {
  appendLog("\n[starting setup — clone + patches + cluster build; first run can take ~10 min]\n");
  streamFrom("/api/setup");
});

document.getElementById("run-demo").addEventListener("click", () => {
  appendLog("\n[starting demo mode — simulated output, no cluster needed]\n");
  streamFrom("/api/demo");
});

document.getElementById("run-test").addEventListener("click", () => {
  const agent = selectedAgent();
  const model = document.getElementById("model").value.trim();
  const judgeModel = document.getElementById("judge-model").value.trim();
  const problems = selectedProblems();
  const forceBuild = document.getElementById("force-build").checked;
  if (!problems.length) {
    appendLog("\n[pick at least one problem first]\n");
    return;
  }
  const params = new URLSearchParams({
    agent: agent.id,
    model,
    judge_model: judgeModel,
    problems: problems.join(","),
    force_build: forceBuild ? "true" : "false",
  });
  if (agent.action_space) params.set("action_space", agent.action_space);
  appendLog(`\n[running ${problems.length} problem(s) with ${agent.label}]\n`);
  streamFrom("/api/run?" + params.toString());
});

document.getElementById("clear-log").addEventListener("click", () => {
  logEl.textContent = "";
});

loadConfig();
