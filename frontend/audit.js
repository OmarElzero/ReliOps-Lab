// ReliOps Lab: assigned judge audit. Each labeler sees only the items assigned to them, blind (no configuration,
// no judge verdict). The owner draws the sample and reads the score (owner password). Loaded after v2.js.

(() => {
  const root = document.getElementById("aa-root");
  if (!root) return;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked: fine */ } },
  };
  const pw = {
    get() { try { return sessionStorage.getItem("lab_owner_pw") || ""; } catch (e) { return ""; } },
    set(v) { try { v ? sessionStorage.setItem("lab_owner_pw", v) : sessionStorage.removeItem("lab_owner_pw"); } catch (e) { /* ignore */ } },
  };
  function h(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k === "value") n.value = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    kids.flat().forEach((c) => { if (c != null && c !== false) n.append(c.nodeType ? c : document.createTextNode(String(c))); });
    return n;
  }
  const Q = {
    d1: "D1 Localization: does it name the faulty component(s) listed under D1 in the card?",
    d2: "D2 Characterization: does it state the mechanism listed under D2 (not just a symptom)?",
    d3: "D3 Scope: does it avoid blaming what the card says not to blame, and avoid contradicting claims?",
  };
  const st = { plans: [], audit: store.get("lab_audit_id", ""), labeler: store.get("lab_audit_labeler", ""), mine: null, cur: null,
               ans: {}, story: null, msg: "", owner: false, ownerOpen: false, ownerMsg: "", cfg: null, score: null, preview: "" };

  async function ownerCall(method, url, body) {
    const res = await fetch(url, { method, headers: { "Content-Type": "application/json", "x-owner-password": pw.get() }, body: body ? JSON.stringify(body) : undefined });
    let out = {};
    try { out = await res.json(); } catch (e) { /* not JSON */ }
    if (res.status === 403) { st.owner = false; pw.set(""); }
    return { ok: res.ok, data: out };
  }

  async function load() {
    try { st.plans = (await jsonGet("/api/audit/plans")).plans || []; } catch (e) { st.plans = []; }
    if (!st.plans.find((p) => p.audit_id === st.audit)) st.audit = st.plans[0] ? st.plans[0].audit_id : "";
    const plan = st.plans.find((p) => p.audit_id === st.audit);
    if (plan && !(plan.labelers || []).some((l) => l.toLowerCase() === st.labeler.toLowerCase())) {
      const me = (typeof who === "function" ? who() : "").toLowerCase();
      st.labeler = (plan.labelers || []).find((l) => l.toLowerCase() === me) || "";
    }
    await loadMine();
    if (pw.get() && !st.owner) await tryUnlock(pw.get(), true);
    render();
  }
  async function loadMine() {
    st.mine = null;
    if (!st.audit || !st.labeler) return;
    const r = await fetch(`/api/audit/mine?audit_id=${encodeURIComponent(st.audit)}&labeler=${encodeURIComponent(st.labeler)}`);
    const d = await r.json();
    if (!r.ok) { st.msg = d.error || "Could not load your items."; return; }
    st.mine = d;
    const open = d.items.find((i) => i.item_id === st.cur) || d.items.find((i) => !i.done) || d.items[0] || null;
    select(open ? open.item_id : null);
  }
  function select(id) {
    st.cur = id;
    const it = st.mine && st.mine.items.find((i) => i.item_id === id);
    st.ans = it && it.mine ? { ...it.mine } : { d1: null, d2: null, d3: null, confidence: null, reason: "" };
    st.story = null;
  }
  async function tryUnlock(secret, quiet) {
    const res = await fetch("/api/tasks/unlock", { method: "POST", headers: { "x-owner-password": secret } });
    st.owner = res.ok;
    if (res.ok) { pw.set(secret); if (!st.cfg) { try { st.cfg = await jsonGet("/api/audit/configs"); } catch (e) { /* ignore */ } } }
    else { pw.set(""); if (!quiet) st.ownerMsg = "Wrong owner password, or none is set on this machine (see the Tasks tab help)."; }
    return res.ok;
  }

  async function save() {
    const a = st.ans, it = st.mine.items.find((i) => i.item_id === st.cur);
    const r = await jsonPost("/api/audit/label", { audit_id: st.audit, item_id: st.cur, labeler: st.labeler, d1: a.d1, d2: a.d2, d3: a.d3, confidence: a.confidence, reason: a.reason || "", used_story: !!a.used_story });
    if (!r.ok) { st.msg = r.data.error || "Could not save."; render(); return; }
    st.msg = "";
    const wasDone = it.done;
    await loadMine();
    if (!wasDone) { const next = st.mine.items.find((i) => !i.done); if (next) select(next.item_id); }
    try { st.plans = (await jsonGet("/api/audit/plans")).plans || []; } catch (e) { /* ignore */ }
    render();
  }

  function yn(key) {
    const set = (v) => { st.ans[key] = v; render(); };
    const on = (v) => (st.ans[key] === v ? "primary" : "secondary");
    return h("div", { class: "aa-q" }, h("div", { class: "hint", text: Q[key] }),
      h("div", { class: "row" }, h("button", { type: "button", class: on(true), onclick: () => set(true), text: "Yes" }),
        h("button", { type: "button", class: on(false), onclick: () => set(false), text: "No" })));
  }
  function storyBox(it) {
    const open = async () => {
      st.ans.used_story = true;
      st.story = "loading…"; render();
      try {
        const r = await fetch(`/api/audit/story?audit_id=${encodeURIComponent(st.audit)}&labeler=${encodeURIComponent(st.labeler)}&item_id=${encodeURIComponent(it.item_id)}`);
        st.story = await r.text();
      } catch (e) { st.story = "Could not load the story."; }
      render();
    };
    return h("div", { class: "aa-q" },
      st.story === null
        ? h("div", { class: "row" }, h("button", { type: "button", class: "secondary", onclick: open, text: "Show the agent's story (optional)" }),
            h("span", { class: "hint", text: "The judge only saw the diagnosis text above, so judge that first. Opening the story is recorded with your label." }))
        : h("div", {}, h("div", { class: "hint", text: "Agent story (opened; recorded with your label)" }), h("pre", { class: "detail-body", style: "max-height:420px;overflow:auto", text: st.story })));
  }
  function itemForm() {
    const it = st.mine.items.find((i) => i.item_id === st.cur);
    if (!it) return h("p", { class: "hint", text: "No items." });
    const a = st.ans;
    const overall = a.d1 === null || a.d2 === null ? "—" : (a.d1 && a.d2 ? "Correct" : "Incorrect");
    return h("div", {},
      h("div", { class: "cfg-label", text: `${it.item_id} · ${it.problem_id || "?"}` }),
      h("div", { class: "label-text", text: it.diagnosis_text }),
      storyBox(it),
      h("div", { class: "label-expected", text: it.expected || "No problem card for this problem yet. Label against the problem's rubric and say so in the reason." }),
      yn("d1"), yn("d2"), yn("d3"),
      h("p", { class: "hint", text: `Overall (D1 and D2 both Yes): ${overall}` }),
      h("div", { class: "aa-q" }, h("div", { class: "hint", text: "Confidence" }),
        h("div", { class: "row" }, [[1, "1 unsure"], [2, "2 fairly sure"], [3, "3 sure"]].map(([v, t]) =>
          h("button", { type: "button", class: a.confidence === v ? "primary" : "secondary", onclick: () => { a.confidence = v; render(); }, text: t })))),
      h("textarea", { rows: "2", placeholder: "Reason, one sentence (needed when Incorrect or unsure)", value: a.reason || "", style: "width:100%;margin-top:8px",
        oninput: (e) => { a.reason = e.target.value; } }),
      h("div", { class: "row", style: "margin-top:10px" },
        h("button", { type: "button", class: "primary", onclick: save, disabled: a.d1 === null || a.d2 === null || a.d3 === null || !a.confidence, text: it.done ? "Update" : "Save and next" })),
      st.msg ? h("p", { class: "hint", style: "color:var(--bad)", text: st.msg }) : null);
  }
  function labelerPanel() {
    const plan = st.plans.find((p) => p.audit_id === st.audit);
    if (!plan) return h("p", { class: "hint", text: "No audit has been drawn yet. The owner draws one in the panel below, then it shows up here for each assigned person." });
    const sel = h("select", { onchange: async (e) => { st.audit = e.target.value; store.set("lab_audit_id", st.audit); st.labeler = ""; st.msg = ""; await load(); } },
      st.plans.map((p) => h("option", { value: p.audit_id, selected: p.audit_id === st.audit, text: `${p.name} (${p.items} runs)` })));
    const me = h("select", { onchange: async (e) => { st.labeler = e.target.value; store.set("lab_audit_labeler", st.labeler); st.msg = ""; await loadMine(); render(); } },
      h("option", { value: "", text: "I am…" }), (plan.labelers || []).map((l) => h("option", { value: l, selected: l === st.labeler, text: l })));
    const prog = (plan.progress || []).map((p) => `${p.labeler} ${p.done}/${p.total}`).join(" · ");
    const body = [h("div", { class: "row" }, sel, me), h("p", { class: "hint", text: prog })];
    if (!st.labeler) body.push(h("p", { class: "hint", text: "Pick your name to see your items." }));
    else if (st.mine) {
      body.push(h("p", { class: "hint", text: `${st.mine.done} of ${st.mine.total} done. Work alone; do not discuss items until everyone has finished.` }));
      body.push(h("div", { class: "row aa-chips" }, st.mine.items.map((i) =>
        h("button", { type: "button", class: i.item_id === st.cur ? "primary" : "secondary", onclick: () => { select(i.item_id); st.msg = ""; render(); }, text: `${i.done ? "✓ " : ""}${i.item_id}` }))));
      body.push(itemForm());
    } else if (st.msg) body.push(h("p", { class: "hint", style: "color:var(--bad)", text: st.msg }));
    return h("div", {}, body);
  }

  function table(head, rows) {
    return h("div", { class: "results-table-wrap" }, h("table", { class: "results-table" },
      h("thead", {}, h("tr", {}, head.map((t) => h("th", { text: t })))),
      h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", { text: String(c) })))))));
  }
  const kap = (x) => (typeof x === "number" ? x.toFixed(2) : "—");
  function ownerPanel() {
    const box = h("details", { class: "card", style: "margin-top:12px", open: st.ownerOpen, ontoggle: (e) => { st.ownerOpen = e.currentTarget.open; } }, h("summary", { text: "Owner: draw the sample and read the score" }));
    if (!st.owner) {
      const inp = h("input", { type: "password", placeholder: "owner password", value: pw.get() });
      box.append(h("div", { class: "row", style: "margin-top:8px" }, inp,
        h("button", { type: "button", class: "secondary", text: "Unlock", onclick: async () => { st.ownerMsg = ""; await tryUnlock(inp.value, false); render(); } })),
        st.ownerMsg ? h("p", { class: "hint", text: st.ownerMsg }) : null);
      return box;
    }
    const cfg = st.cfg || { configs: [], people: [], default_groups: "R3 | split | 12\nR2 | pooled | 12" };
    const name = h("input", { placeholder: "audit name, e.g. Final audit", value: store.get("lab_audit_name", "Final audit") });
    const people = h("input", { placeholder: "labelers, comma separated", value: store.get("lab_audit_people", "Omar Betawy, Noura, Zeinab Mohamed"), style: "flex:1 1 320px" });
    const groups = h("textarea", { rows: "3", style: "width:100%" , value: store.get("lab_audit_groups", cfg.default_groups) });
    const out = h("p", { class: "hint", text: st.preview || "" });
    const body = () => ({ name: name.value, labelers: people.value.split(",").map((s) => s.trim()).filter(Boolean), groups: groups.value, by: (typeof who === "function" ? who() : "") });
    const remember = () => { store.set("lab_audit_name", name.value); store.set("lab_audit_people", people.value); store.set("lab_audit_groups", groups.value); };
    const go = async (preview) => {
      remember();
      const r = await ownerCall("POST", "/api/audit/draw", { ...body(), preview });
      st.preview = r.ok ? `${preview ? "Preview: " : "Drawn: "}${r.data.items} runs. ${(r.data.notes || []).join(" | ")}` : (r.data.error || "Failed.");
      if (r.ok && !preview) await load(); else render();
    };
    box.append(
      h("p", { class: "hint", text: "Draw once, on the final exports. The sample stores run ids, so later runs cannot change it, and it freezes when the first label arrives. Half of every stratum is judge-pass and half judge-fail; the first 3 runs of each stratum are labelled by everyone." }),
      h("div", { class: "row" }, name, people), h("p", { class: "hint", text: `Known names on this Lab: ${(cfg.people || []).join(", ") || "none yet"}. Type the names exactly as each person should appear.` }),
      h("div", { class: "hint", text: "Groups: one line per `prefix | split or pooled | runs`. split = one stratum per configuration starting with the prefix." }), groups,
      h("div", { class: "row", style: "margin-top:8px" },
        h("button", { type: "button", class: "secondary", text: "Preview", onclick: () => go(true) }),
        h("button", { type: "button", class: "primary", text: "Draw and assign", onclick: () => go(false) })), out,
      table(["Configuration", "Eligible runs", "Judge passed"], (cfg.configs || []).map((c) => [c.config_label || "(none)", c.eligible, c.judged_pass])),
      h("div", { class: "row", style: "margin-top:12px" },
        h("button", { type: "button", class: "secondary", text: "Score the selected audit", onclick: async () => {
          const r = await ownerCall("GET", `/api/audit/score?audit_id=${encodeURIComponent(st.audit)}`);
          st.score = r.ok ? r.data : { error: r.data.error || "Failed." }; render(); } })),
      scoreView());
    return box;
  }
  function scoreView() {
    const s = st.score;
    if (!s) return h("span");
    if (s.error) return h("p", { class: "hint", text: s.error });
    const lo = (w) => (w ? `${Math.round(100 * w[0])}–${Math.round(100 * w[1])}%` : "—");
    return h("div", {}, h("p", { class: "hint", text: s.note }),
      table(["Labeler", "Done", "Total"], s.progress.map((p) => [p.labeler, p.done, p.total])),
      table(["Labeler A", "Labeler B", "Anchors", "Agree", "Kappa"], s.labeler_pairs.map((p) => [p.a, p.b, p.n, p.agree, kap(p.kappa)])),
      table(["Group", "n", "Agree", "95% Wilson", "Kappa", "Judge pass, human wrong", "Judge fail, human right"],
        s.judge_vs_human.map((r) => [r.group, r.n, `${r.agree} (${Math.round(100 * r.agree / r.n)}%)`, lo(r.wilson), kap(r.kappa), `${r.fp}/${r.judge_pass}`, `${r.fn}/${r.judge_fail}`])),
      s.ties.length ? h("p", { class: "hint", text: `Tied labels, left out: ${s.ties.join(", ")}` }) : null,
      s.disagreements.length ? table(["Item", "Stratum", "Problem", "Judge", "Human"], s.disagreements.map((d) => [d.item_id, d.stratum, d.problem_id, d.judge ? "pass" : "fail", d.human ? "correct" : "incorrect"])) : null);
  }

  function render() {
    root.replaceChildren(h("section", { class: "card" }, h("h2", { text: "Assigned audit" }),
      h("p", { class: "hint", text: "A blind sample drawn by the owner. You see the problem, what the card says a correct diagnosis must say, and the diagnosis; never the configuration or the judge's verdict." }),
      labelerPanel(), ownerPanel()));
  }
  const previous = TAB_LOADERS.audit;
  TAB_LOADERS.audit = () => { if (previous) previous(); load(); };
})();
