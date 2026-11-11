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
               ans: {}, story: null, card: null, results: null, prob: "", cards: {}, manage: null, confirmDelete: false, editMsg: "", msg: "", owner: false, ownerOpen: false, ownerMsg: "", cfg: null, score: null, preview: "" };

  async function ownerCall(method, url, body) {
    const res = await fetch(url, { method, headers: { "Content-Type": "application/json", "x-owner-password": pw.get() }, body: body ? JSON.stringify(body) : undefined });
    let out = {};
    try { out = await res.json(); } catch (e) { /* not JSON */ }
    if (res.status === 403) { st.owner = false; pw.set(""); }
    return { ok: res.ok, data: out };
  }

  async function load(keep) {
    try { st.plans = (await jsonGet("/api/audit/plans")).plans || []; } catch (e) { st.plans = []; }
    if (!st.plans.find((p) => p.audit_id === st.audit)) st.audit = st.plans[0] ? st.plans[0].audit_id : "";
    const plan = st.plans.find((p) => p.audit_id === st.audit);
    if (plan && !(plan.labelers || []).some((l) => l.toLowerCase() === st.labeler.toLowerCase())) {
      const me = (typeof who === "function" ? who() : "").toLowerCase();
      st.labeler = (plan.labelers || []).find((l) => l.toLowerCase() === me) || "";
    }
    if (!st.audit) { st.mine = null; st.manage = null; }
    else if (keep && st.mine) {
      try {
        const d = await (await fetch(`/api/audit/mine?audit_id=${encodeURIComponent(st.audit)}&labeler=${encodeURIComponent(st.labeler)}`)).json();
        st.mine = d.items ? d : null;
        if (st.mine && !st.mine.items.find((i) => i.item_id === st.cur)) { const f = st.mine.items.find((i) => !i.done) || st.mine.items[0]; select(f ? f.item_id : null); }
      } catch (e) { /* keep the old list */ }
    } else await loadMine();
    if (pw.get() && !st.owner) await tryUnlock(pw.get(), true);
    if (st.owner && st.audit) await loadManage();
    render();
  }
  async function loadManage() {
    const r = await ownerCall("GET", `/api/audit/manage?audit_id=${encodeURIComponent(st.audit)}`);
    st.manage = r.ok ? r.data : null;
  }
  async function loadMine() {
    st.mine = null;
    if (!st.audit || !st.labeler) return;
    const r = await fetch(`/api/audit/mine?audit_id=${encodeURIComponent(st.audit)}&labeler=${encodeURIComponent(st.labeler)}`);
    const d = await r.json();
    if (!r.ok) { st.msg = d.error || "Could not load your items."; return; }
    st.mine = d;
    const open = d.items.find((i) => i.item_id === st.cur) || d.items.find((i) => !i.done) || d.items[0] || null;
    if (!(open && open.item_id === st.cur)) select(open ? open.item_id : null);
  }
  function select(id) {
    st.cur = id;
    const it = st.mine && st.mine.items.find((i) => i.item_id === id);
    if (it) st.prob = it.problem_id || "";
    st.card = null;
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
    if (!wasDone) { const next = st.mine.items.find((i) => !i.done && (i.problem_id || "?") === st.prob) || st.mine.items.find((i) => !i.done); if (next) select(next.item_id); }
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
  function cardBox(it) {
    const id = it.problem_id;
    const toggle = async () => {
      if (st.card !== null) { st.card = null; render(); return; }
      st.card = "loading…"; render();
      if (!(id in st.cards)) {
        try { const r = await fetch("/api/card/" + encodeURIComponent(id)); st.cards[id] = await r.text(); } catch (e) { st.cards[id] = "Could not load the card."; }
      }
      st.card = st.cards[id]; render();
    };
    return h("div", { class: "aa-q" },
      h("div", { class: "row" },
        h("button", { type: "button", class: "secondary", onclick: toggle, text: st.card === null ? "Show the full problem card" : "Hide the problem card" }),
        h("button", { type: "button", class: "secondary", onclick: () => window.openProblem && window.openProblem(id), text: "Open in the Problems tab" }),
        h("span", { class: "hint", text: "Your answers stay here when you come back." })),
      st.card === null ? null : h("pre", { class: "detail-body", style: "max-height:420px;overflow:auto", text: st.card }));
  }
  async function finish() {
    const r = await jsonPost("/api/audit/finish", { audit_id: st.audit, labeler: st.labeler });
    if (!r.ok) { st.msg = r.data.error || "Could not finish."; render(); return; }
    st.msg = ""; await loadResults(); await load(true);
  }
  async function loadResults() {
    const r = await fetch(`/api/audit/results?audit_id=${encodeURIComponent(st.audit)}&labeler=${encodeURIComponent(st.labeler)}`);
    st.results = r.ok ? await r.json() : null;
  }
  function finishBox() {
    const m = st.mine;
    if (!m) return h("span");
    if (m.done < m.total) return h("span");
    if (!m.finished) {
      return h("div", { class: "aa-q" }, h("p", { class: "hint", text: "All your items have an answer. When you are sure, finish: your answers get locked and you see the judge's verdict next to each of yours." }),
        h("button", { type: "button", class: "primary", onclick: finish, text: "Finish and see the judge's verdicts" }));
    }
    if (!st.results) { loadResults().then(render); return h("p", { class: "hint", text: "Loading your results…" }); }
    const R = st.results;
    return h("div", { class: "aa-q" }, h("h3", { text: "Your answers against the judge" }),
      h("p", { class: "hint", text: `You and the judge gave the same verdict on ${R.same} of ${R.total} items. Differences are listed first; they are the interesting ones (the judge may be too lenient or too strict, or you may have misread the card).` }),
      R.rows.map((x) => h("details", { class: "aa-q" }, h("summary", { text: `${x.same === false ? "✗ differ" : "✓ same"} · ${x.item_id} · ${x.problem_id || "?"} · judge: ${x.judge_pass === true ? "pass" : x.judge_pass === false ? "fail" : "?"} · you: ${x.you ? "correct" : "incorrect"}` }),
        h("div", { class: "hint", text: "Judge's reasoning" }), h("pre", { class: "detail-body", text: x.judge_reasoning || "(none stored)" }),
        x.your_reason ? h("div", { class: "hint", text: `Your reason: ${x.your_reason}` }) : null)));
  }
  function itemForm() {
    const it = st.mine.items.find((i) => i.item_id === st.cur);
    if (!it) return h("p", { class: "hint", text: "No items." });
    const a = st.ans;
    const overall = a.d1 === null || a.d2 === null ? "—" : (a.d1 && a.d2 ? "Correct" : "Incorrect");
    return h("div", {},
      h("div", { class: "cfg-label", text: `${it.item_id} · ${it.problem_id || "?"}` }),
      h("div", { class: "label-text", text: it.diagnosis_text }),
      cardBox(it),
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
        h("button", { type: "button", class: "primary", onclick: save, disabled: it.locked || a.d1 === null || a.d2 === null || a.d3 === null || !a.confidence, text: it.locked ? "Locked (you finished)" : it.done ? "Update" : "Save and next" })),
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
      body.push(h("p", { class: "hint", text: `${st.mine.done} of ${st.mine.total} done. Each problem below is yours to learn once: read its card, then label all its runs. Work alone; do not discuss items until everyone has finished.` }));
      const probs = [...new Set(st.mine.items.map((i) => i.problem_id || "?"))];
      body.push(h("div", { class: "row aa-chips" }, probs.map((p) => {
        const its = st.mine.items.filter((i) => (i.problem_id || "?") === p);
        const dn = its.filter((i) => i.done).length;
        return h("button", { type: "button", class: p === st.prob ? "primary" : "secondary", title: p,
          onclick: () => { const first = its.find((i) => !i.done) || its[0]; select(first.item_id); st.msg = ""; render(); }, text: `${p} ${dn}/${its.length}` });
      })));
      body.push(h("div", { class: "row aa-chips" }, st.mine.items.filter((i) => (i.problem_id || "?") === st.prob).map((i) =>
        h("button", { type: "button", class: i.item_id === st.cur ? "primary" : "secondary", onclick: () => { select(i.item_id); st.msg = ""; render(); }, text: `${i.done ? "✓ " : ""}${i.item_id}` }))));
      body.push(finishBox());
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
    const assign = h("select", { style: "width:auto" }, h("option", { value: "by_problem", text: "Whole problems per person (recommended)" }), h("option", { value: "spread", text: "Spread runs round-robin" }));
    const overlap = h("input", { type: "number", min: "0", max: "5", value: store.get("lab_audit_overlap", "1"), style: "width:80px", title: "problems each person also labels from the previous person's set" });
    const out = h("p", { class: "hint", text: st.preview || "" });
    const body = () => ({ name: name.value, labelers: people.value.split(",").map((s) => s.trim()).filter(Boolean), groups: groups.value, by: (typeof who === "function" ? who() : ""), assign: assign.value, overlap_problems: parseInt(overlap.value || "0", 10) || 0 });
    const remember = () => { store.set("lab_audit_name", name.value); store.set("lab_audit_people", people.value); store.set("lab_audit_groups", groups.value); store.set("lab_audit_overlap", overlap.value); };
    const go = async (preview) => {
      remember();
      const r = await ownerCall("POST", "/api/audit/draw", { ...body(), preview });
      st.preview = r.ok ? `${preview ? "Preview: " : "Drawn: "}${r.data.items} runs. ${(r.data.notes || []).join(" | ")}` : (r.data.error || "Failed.");
      if (r.ok && !preview) await load(); else render();
    };
    box.append(
      h("p", { class: "hint", text: "Draw once, on the final exports; the sample stores run ids, so later runs cannot change it. Half of every stratum is judge-pass and half judge-fail. With whole problems per person, nobody learns more problems than needed; the overlap problems (labelled by two people) are what measure how far the labelers agree. After labels arrive you can still redistribute, add runs or remove runs below, but not draw the same name again." }),
      h("div", { class: "row" }, name, people), h("p", { class: "hint", text: `Known names on this Lab: ${(cfg.people || []).join(", ") || "none yet"}. Type the names exactly as each person should appear.` }),
      h("div", { class: "hint", text: "Groups: one line per `prefix | split or pooled | runs`. split = one stratum per configuration starting with the prefix." }), groups,
      h("div", { class: "row", style: "margin-top:8px" }, assign, h("span", { class: "hint", text: "Overlap problems per person:" }), overlap),
      h("div", { class: "row", style: "margin-top:8px" },
        h("button", { type: "button", class: "secondary", text: "Preview", onclick: () => go(true) }),
        h("button", { type: "button", class: "primary", text: "Draw and assign", onclick: () => go(false) })), out,
      table(["Configuration", "Eligible runs", "Judge passed"], (cfg.configs || []).map((c) => [c.config_label || "(none)", c.eligible, c.judged_pass])),
      manageView(),
      h("div", { class: "row", style: "margin-top:12px" },
        h("button", { type: "button", class: "secondary", text: "Score the selected audit", onclick: async () => {
          const r = await ownerCall("GET", `/api/audit/score?audit_id=${encodeURIComponent(st.audit)}`);
          st.score = r.ok ? r.data : { error: r.data.error || "Failed." }; render(); } })),
      scoreView());
    return box;
  }
  function manageView() {
    const m = st.manage;
    if (!st.audit) return h("span");
    if (!m) return h("p", { class: "hint", text: "Draw an audit to manage it here." });
    const say = (t) => { st.editMsg = t; render(); };
    const edit = async (payload, okText) => {
      const r = await ownerCall("POST", "/api/audit/edit", { audit_id: st.audit, by: (typeof who === "function" ? who() : ""), ...payload });
      if (!r.ok) { st.editMsg = r.data.error || "Failed."; render(); return; }
      st.editMsg = typeof okText === "function" ? okText(r.data) : okText; st.confirmDelete = false;
      await load(true);
    };
    const people = h("input", { value: store.get("lab_audit_redeal", m.labelers.map((l) => l.labeler).join(", ")), style: "flex:1 1 320px" });
    const ov = h("input", { type: "number", min: "0", max: "5", value: "1", style: "width:80px" });
    const more = h("input", { value: "R3 | split | 2", style: "flex:1 1 240px", title: "same format as the groups above" });
    const rem = h("input", { placeholder: "item ids, e.g. I004, I010", style: "flex:1 1 240px" });
    const asList = (t) => t.split(",").map((x) => x.trim()).filter(Boolean);
    return h("div", { style: "margin-top:12px" }, h("h3", { text: "Manage this audit" }),
      table(["Labeler", "Items", "Done", "Problems (items, done, shared with another labeler)"],
        m.labelers.map((l) => [l.labeler, l.items, l.done, l.problems.map((p) => `${p.problem_id} (${p.items}, ${p.done}${p.overlap ? `, ${p.overlap} shared` : ""})`).join("; ")])),
      h("p", { class: "hint", text: `${m.items} items, ${m.unlabeled.length} with no label yet.` }),
      h("div", { class: "hint", text: "Redistribute: re-deal the items nobody has labelled yet among these people (whole problems stay together; every label already given is kept). Use it to add a person, drop a person or even out the load." }),
      h("div", { class: "row" }, people, h("span", { class: "hint", text: "Overlap problems:" }), ov,
        h("button", { type: "button", class: "secondary", text: "Redistribute", onclick: () => { store.set("lab_audit_redeal", people.value); edit({ action: "redistribute", labelers: asList(people.value), overlap_problems: parseInt(ov.value || "0", 10) || 0 }, (d) => `Re-dealt ${d.re_dealt} items, kept ${d.kept} that already have labels.`); } })),
      h("div", { class: "hint", text: "Add runs: draw more by the same rules (never a run already in the sample); a problem stays with whoever holds it." }),
      h("div", { class: "row" }, more,
        h("button", { type: "button", class: "secondary", text: "Add runs", onclick: () => edit({ action: "extend", groups: more.value, seed: Math.floor(Math.random() * 1e6) }, (d) => `${(d.notes || []).join(" | ")}. Now ${d.items} items.`) })),
      h("div", { class: "hint", text: "Remove runs: only items nobody has labelled." }),
      h("div", { class: "row" }, rem,
        h("button", { type: "button", class: "secondary", text: "Remove", onclick: () => edit({ action: "remove", item_ids: asList(rem.value) }, (d) => `Removed ${d.removed.length}${d.refused.length ? `; not removed (already labelled or unknown): ${d.refused.join(", ")}` : ""}. Now ${d.items} items.`) })),
      h("div", { class: "row", style: "margin-top:10px" },
        st.confirmDelete
          ? [h("button", { type: "button", class: "primary", text: "Yes, delete this audit", onclick: () => edit({ action: "delete" }, "Audit deleted (its labels are kept in the store).") }),
             h("button", { type: "button", class: "secondary", text: "Keep it", onclick: () => { st.confirmDelete = false; render(); } })]
          : h("button", { type: "button", class: "secondary", text: "Delete this audit", onclick: () => { st.confirmDelete = true; render(); } })),
      st.editMsg ? h("p", { class: "hint", text: st.editMsg }) : null,
      m.history.length ? h("details", {}, h("summary", { text: `History (${m.history.length})` }), h("pre", { class: "detail-body", text: m.history.map((e) => `${e.at}  ${e.by || "?"}  ${e.action}: ${e.detail}`).join("\n") })) : null);
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
  TAB_LOADERS.audit = () => { if (previous) previous(); load(true); };
})();
