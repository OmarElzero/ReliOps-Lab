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
    d1: ["Right place?", "Does it name the broken component given under Place?"],
    d2: ["Right cause?", "Does it state the mechanism given under Cause, not only a symptom?"],
    d3: ["Nothing wrongly blamed?", "Does it avoid blaming what Not to blame lists, and avoid claims that contradict the key?"],
  };
  const CONF = [[1, "Unsure"], [2, "Fairly sure"], [3, "Sure"]];
  const st = { plans: [], audit: store.get("lab_audit_id", ""), labeler: store.get("lab_audit_labeler", ""), mine: null, cur: null,
               drafts: {}, stories: {}, itemMsg: {}, saving: "", howOpen: store.get("lab_audit_how", "1") === "1", card: null, results: null, rjLog: "", prob: "", cards: {}, manage: null, confirmDelete: false, editMsg: "", msg: "", owner: false, ownerOpen: false, ownerMsg: "", cfg: null, score: null, preview: "" };

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
        pickProblem();
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
    pickProblem();
  }
  const probOf = (i) => i.problem_id || "?";
  function problems() {
    if (!st.mine) return [];
    const order = [];
    for (const i of st.mine.items) if (!order.includes(probOf(i))) order.push(probOf(i));
    return order.map((p) => { const its = st.mine.items.filter((i) => probOf(i) === p); return { id: p, items: its, done: its.filter((i) => i.done).length, brief: its[0].brief }; });
  }
  function pickProblem() {
    const ps = problems();
    if (!ps.find((p) => p.id === st.prob)) { const open = ps.find((p) => p.done < p.items.length) || ps[0]; st.prob = open ? open.id : ""; }
  }
  function select(id) {  // kept for older callers: jump to the item's problem
    const it = st.mine && st.mine.items.find((i) => i.item_id === id);
    if (it) st.prob = probOf(it);
  }
  function draft(it) {
    if (!st.drafts[it.item_id]) st.drafts[it.item_id] = it.mine ? { ...it.mine, dirty: false } : { d1: null, d2: null, d3: null, confidence: null, reason: "", dirty: false };
    return st.drafts[it.item_id];
  }
  async function tryUnlock(secret, quiet) {
    const res = await fetch("/api/tasks/unlock", { method: "POST", headers: { "x-owner-password": secret } });
    st.owner = res.ok;
    if (res.ok) { pw.set(secret); if (!st.cfg) { try { st.cfg = await jsonGet("/api/audit/configs"); } catch (e) { /* ignore */ } } }
    else { pw.set(""); if (!quiet) st.ownerMsg = "Wrong owner password, or none is set on this machine (see the Tasks tab help)."; }
    return res.ok;
  }

  function missing(d) {
    const m = [];
    if (d.d1 === null) m.push("right place"); if (d.d2 === null) m.push("right cause"); if (d.d3 === null) m.push("nothing wrongly blamed");
    if (!d.confidence) m.push("how sure");
    if ((d.d1 === false || d.d2 === false || d.confidence === 1) && !(d.reason || "").trim()) m.push("a one-line reason (needed for Incorrect or Unsure)");
    return m;
  }
  async function save(it) {
    const d = draft(it), miss = missing(d);
    if (miss.length) { st.itemMsg[it.item_id] = "Still needed: " + miss.join(", ") + "."; render(); return; }
    st.saving = it.item_id; st.itemMsg[it.item_id] = ""; render();
    const r = await jsonPost("/api/audit/label", { audit_id: st.audit, item_id: it.item_id, labeler: st.labeler, d1: d.d1, d2: d.d2, d3: d.d3, confidence: d.confidence, reason: d.reason || "", used_story: !!d.used_story });
    st.saving = "";
    if (!r.ok) { st.itemMsg[it.item_id] = r.data.error || "Could not save. Try again."; render(); return; }
    d.dirty = false;
    if (st.howOpen) { st.howOpen = false; store.set("lab_audit_how", "0"); }
    await loadMine();
    try { st.plans = (await jsonGet("/api/audit/plans")).plans || []; } catch (e) { /* ignore */ }
    render();
  }
  function seg(label, options, value, onPick, disabled) {
    return h("div", { class: "aa-seg", role: "group", "aria-label": label },
      options.map(([v, t, tone]) => h("button", { type: "button", class: value === v ? `on ${tone || ""}` : "", "aria-pressed": value === v ? "true" : "false", disabled, onclick: () => onPick(v), text: t })));
  }
  const fmt = (t) => { const out = []; String(t || "").split(/(`[^`]+`)/).forEach((x) => out.push(x.startsWith("`") && x.endsWith("`") && x.length > 1 ? h("code", { text: x.slice(1, -1) }) : x)); return out; };
  function briefPanel(p) {
    const b = p.brief, it0 = p.items[0];
    const key = it0.reference ? h("details", { class: "aa-key", open: !b },
        h("summary", { text: "Answer key: SREGym's reference text, word for word" }),
        h("p", { class: "aa-small", text: "This is exactly what both judges graded against. The summary above is written from it; where they seem to differ, this text wins." }),
        h("pre", { text: it0.reference }))
      : it0.expected ? h("div", { class: "aa-key" }, h("div", { class: "aa-label", text: "What a correct diagnosis must say (problem card)" }), h("pre", { text: it0.expected }))
      : h("p", { class: "aa-small", text: "No answer key for this problem on this machine. Pull the Lab and restart, then reload." });
    if (!b) return h("aside", { class: "aa-brief" }, h("div", { class: "aa-label", text: `${p.items.length} ${p.items.length === 1 ? "diagnosis" : "diagnoses"}` }), h("h3", { class: "aa-pname", text: p.id }), key, cardBox(it0));
    return h("aside", { class: "aa-brief" },
      h("div", { class: "aa-label", text: `${b.app} · ${p.items.length} ${p.items.length === 1 ? "diagnosis" : "diagnoses"}` }),
      h("h3", { class: "aa-pname", text: b.name }),
      h("div", { class: "aa-pid", text: p.id }),
      h("p", {}, fmt(b.what)),
      b.like ? h("p", { class: "aa-like" }, fmt(b.like)) : null,
      h("div", { class: "aa-label", text: "A correct diagnosis says" }),
      h("dl", { class: "aa-must" },
        h("dt", { text: "Place" }), h("dd", {}, fmt(b.place)),
        h("dt", { text: "Cause" }), h("dd", {}, fmt(b.cause)),
        b.notblame ? [h("dt", { text: "Not to blame" }), h("dd", {}, fmt(b.notblame))] : null),
      key);
  }
  function storyLink(it) {
    const d = draft(it), open = st.stories[it.item_id];
    const go = async () => {
      d.used_story = true; d.dirty = true; st.stories[it.item_id] = "Loading…"; render();
      try {
        const r = await fetch(`/api/audit/story?audit_id=${encodeURIComponent(st.audit)}&labeler=${encodeURIComponent(st.labeler)}&item_id=${encodeURIComponent(it.item_id)}`);
        st.stories[it.item_id] = await r.text();
      } catch (e) { st.stories[it.item_id] = "Could not load the run."; }
      render();
    };
    if (open) return h("details", { class: "aa-story", open: true }, h("summary", { text: "The agent's run (opened; noted with your label)" }), h("pre", { text: open }));
    return h("button", { type: "button", class: "ghost aa-storybtn", onclick: go, text: "Open the agent's run (only if you cannot decide; it is noted)" });
  }
  function itemCard(it) {
    const d = draft(it), locked = it.locked;
    const set = (k) => (v) => { d[k] = v; d.dirty = true; st.itemMsg[it.item_id] = ""; render(); };
    const verdict = d.d1 === null || d.d2 === null ? null : d.d1 && d.d2;
    const chip = d.dirty && it.done ? ["Changed, not saved", "warn"] : it.done ? [verdict ? "Saved · Correct" : "Saved · Incorrect", verdict ? "ok" : "bad"]
      : verdict === null ? ["Not labelled", ""] : [`${verdict ? "Correct" : "Incorrect"} · not saved`, "warn"];
    const needReason = (d.d1 === false || d.d2 === false || d.confidence === 1) && !(d.reason || "").trim();
    return h("article", { class: `aa-item${it.done && !d.dirty ? " done" : ""}`, id: `aa-${it.item_id}` },
      h("div", { class: "aa-itemtop" }, h("span", { class: "aa-iid", text: it.item_id }), h("span", { class: `aa-chip ${chip[1]}`, text: chip[0] })),
      h("p", { class: "aa-diag", text: it.diagnosis_text }),
      h("div", { class: "aa-qs" }, ["d1", "d2", "d3"].map((k) => h("div", { class: "aa-qrow", title: Q[k][1] },
        h("span", { class: "aa-qt" }, h("b", { text: Q[k][0] }), h("span", { class: "aa-small", text: " " + Q[k][1] })),
        seg(Q[k][0], [[true, "Yes", "yes"], [false, "No", "no"]], d[k], set(k), locked)))),
      h("div", { class: "aa-qrow" }, h("span", { class: "aa-qt" }, h("b", { text: "How sure?" })), seg("How sure", CONF.map(([v, t]) => [v, t, "acc"]), d.confidence, set("confidence"), locked)),
      h("input", { type: "text", class: `aa-reason${needReason ? " need" : ""}`, placeholder: needReason ? "Why? One line (needed for Incorrect or Unsure)" : "Why? One line (optional when Correct and sure)", value: d.reason || "", disabled: locked,
        oninput: (e) => { d.reason = e.target.value; d.dirty = true; e.target.classList.toggle("need", (d.d1 === false || d.d2 === false || d.confidence === 1) && !e.target.value.trim()); } }),
      h("div", { class: "aa-itembar" },
        h("button", { type: "button", class: "primary", disabled: locked || st.saving === it.item_id || (it.done && !d.dirty), onclick: () => save(it),
          text: locked ? "Locked (you finished)" : st.saving === it.item_id ? "Saving…" : it.done ? (d.dirty ? "Save change" : "Saved") : "Save" }),
        verdict === null ? null : h("span", { class: "aa-small", text: `Overall: ${verdict ? "Correct" : "Incorrect"} (correct only when place and cause are both Yes)` }),
        storyLink(it)),
      st.itemMsg[it.item_id] ? h("p", { class: "aa-err", text: st.itemMsg[it.item_id] }) : null);
  }
  function howTo() {
    return h("details", { class: "aa-how", open: st.howOpen, ontoggle: (e) => { st.howOpen = e.currentTarget.open; store.set("lab_audit_how", st.howOpen ? "1" : "0"); } },
      h("summary", { text: "How to label (read once)" }),
      h("ol", {},
        h("li", { text: "Pick a problem below. Read its brief on the left once: what broke, and what a correct diagnosis has to say." }),
        h("li", { text: "For each diagnosis on the right, answer three questions. It counts as correct only when Right place and Right cause are both Yes." }),
        h("li", { text: "Judge only what the text says. Do not credit what the agent may have meant, and do not diagnose the fault yourself." }),
        h("li", { text: "If a problem leaves room for two readings, decide your rule on its first diagnosis and keep to it for the rest of that problem." }),
        h("li", { text: "Say how sure you are, and write one line of reason for every No or Unsure." }),
        h("li", { text: "Work alone. You never see the configuration or either judge's verdict. Do not discuss any item until everyone has finished." })));
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
        h("button", { type: "button", class: "secondary", onclick: () => window.openProblem && window.openProblem(id), text: "Open in the Problems tab" })),
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
  function labelerPanel() {
    const plan = st.plans.find((p) => p.audit_id === st.audit);
    if (!plan) return h("div", { class: "aa-how" }, h("b", { text: "No audit has been drawn yet." }),
      h("p", { class: "hint", text: "The owner draws it once, in the panel below: unlock with the owner password, type the labelers' names, choose \"Everyone labels every item\", set the groups line to \"F30 | pooled | 40\", the second judge to openai/gpt-5.4 and the seed to 20261118, press Preview, then Draw and assign. After that, each labeler pulls the Lab, restarts it, opens this tab and picks their name." }));
    const sel = h("select", { "aria-label": "Audit", onchange: async (e) => { st.audit = e.target.value; store.set("lab_audit_id", st.audit); st.labeler = ""; st.drafts = {}; st.msg = ""; st.rjNext = undefined; await load(); } },
      st.plans.map((p) => h("option", { value: p.audit_id, selected: p.audit_id === st.audit, text: `${p.name} (${p.items} diagnoses)` })));
    const me = h("select", { "aria-label": "Your name", onchange: async (e) => { st.labeler = e.target.value; store.set("lab_audit_labeler", st.labeler); st.drafts = {}; st.msg = ""; await loadMine(); render(); } },
      h("option", { value: "", text: "Choose your name…" }), (plan.labelers || []).map((l) => h("option", { value: l, selected: l === st.labeler, text: l })));
    const others = (plan.progress || []).map((p) => `${p.labeler} ${p.done}/${p.total}`).join(" · ");
    const m = st.mine;
    const pct = m && m.total ? Math.round(100 * m.done / m.total) : 0;
    const top = h("div", { class: "aa-top" },
      h("div", { class: "row" }, sel, me),
      m ? h("div", { class: "aa-prog" }, h("span", { class: "aa-count", text: `${m.done} of ${m.total} labelled` }),
        h("span", { class: "aa-meter", "aria-hidden": "true" }, h("i", { style: `width:${pct}%` }))) : null,
      others ? h("span", { class: "aa-small", text: `Everyone: ${others}` }) : null);
    const body = [top];
    if (!st.labeler) { body.push(h("p", { class: "hint", text: "Choose your name to see your diagnoses." })); return h("div", {}, body); }
    if (!m) { if (st.msg) body.push(h("p", { class: "aa-err", text: st.msg })); return h("div", {}, body); }
    pickProblem();
    if (m.items.length && !("brief" in m.items[0])) body.push(h("div", { class: "aa-how" }, h("b", { text: "The Lab's server is still running older code." }),
      h("p", { class: "hint", text: "The page updated but the server did not, so the problem briefs are missing. Stop the Lab completely (close its window or press Ctrl+C where it runs), start it again, then refresh this page." })));
    const ps = problems(), cur = ps.find((p) => p.id === st.prob);
    body.push(howTo(), finishBox());
    body.push(h("nav", { class: "aa-probs", "aria-label": "Problems" }, ps.map((p, i) => h("button", { type: "button",
      class: `aa-pchip${p.id === st.prob ? " cur" : ""}${p.done === p.items.length ? " full" : ""}`, title: p.id,
      onclick: () => { st.prob = p.id; render(); const t = document.querySelector(".aa-work"); if (t) t.scrollIntoView({ block: "start", behavior: "smooth" }); } },
      h("span", { text: `${i + 1}. ${p.brief ? p.brief.name : p.id}` }), h("b", { text: `${p.done}/${p.items.length}` })))));
    if (cur) {
      const idx = ps.indexOf(cur), next = ps.slice(idx + 1).concat(ps.slice(0, idx)).find((p) => p.done < p.items.length);
      body.push(h("div", { class: "aa-work" }, briefPanel(cur),
        h("div", { class: "aa-items" }, cur.items.map(itemCard),
          h("div", { class: "aa-nextbar" },
            cur.done === cur.items.length ? h("span", { class: "aa-small", text: "All diagnoses of this problem are saved." }) : h("span", { class: "aa-small", text: `${cur.items.length - cur.done} left in this problem.` }),
            next ? h("button", { type: "button", class: "secondary", onclick: () => { st.prob = next.id; render(); const t = document.querySelector(".aa-work"); if (t) t.scrollIntoView({ block: "start", behavior: "smooth" }); }, text: `Next problem: ${next.brief ? next.brief.name : next.id} →` }) : null))));
    }
    return h("div", {}, body);
  }

  function table(head, rows) {
    return h("div", { class: "results-table-wrap" }, h("table", { class: "results-table" },
      h("thead", {}, h("tr", {}, head.map((t) => h("th", { text: t })))),
      h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", { text: String(c) })))))));
  }
  const kap = (x) => (typeof x === "number" ? x.toFixed(2) : "—");
  function ownerPanel() {
    const box = h("details", { class: "card", style: "margin-top:12px", open: st.ownerOpen || !st.plans.length, ontoggle: (e) => { st.ownerOpen = e.currentTarget.open; } }, h("summary", { text: "Owner: draw the sample and read the score" }));
    if (!st.owner) {
      const inp = h("input", { type: "password", placeholder: "owner password", value: pw.get() });
      box.append(h("div", { class: "row", style: "margin-top:8px" }, inp,
        h("button", { type: "button", class: "secondary", text: "Unlock", onclick: async () => { st.ownerMsg = ""; await tryUnlock(inp.value, false); render(); } })),
        ...(st.ownerMsg ? [h("p", { class: "hint", text: st.ownerMsg })] : []));
      return box;
    }
    const cfg = st.cfg || { configs: [], people: [], default_groups: "R3 | split | 12\nR2 | pooled | 12" };
    const name = h("input", { placeholder: "audit name, e.g. Final audit", value: store.get("lab_audit_name", "Final audit") });
    const people = h("input", { placeholder: "labelers, comma separated", value: store.get("lab_audit_people", "Omar Betawy, Noura, Zeinab Mohamed"), style: "flex:1 1 320px" });
    const groups = h("textarea", { rows: "3", style: "width:100%" , value: store.get("lab_audit_groups", cfg.default_groups) });
    const asg = store.get("lab_audit_assign", "all");
    const assign = h("select", { style: "width:auto", title: "Everyone labels every item: what the frozen-30 hand label needs (two labelers, each on all items)" },
      h("option", { value: "all", text: "Everyone labels every item (frozen-30 hand label)", selected: asg === "all" }),
      h("option", { value: "by_problem", text: "Whole problems per person", selected: asg === "by_problem" }),
      h("option", { value: "spread", text: "Spread runs round-robin", selected: asg === "spread" }));
    const disputed = h("input", { placeholder: "only runs this second judge disputes, e.g. openai/gpt-5.4 (empty = all)", value: store.get("lab_audit_disputed", ""), style: "flex:1 1 320px", title: "Keeps only diagnoses where this second judge's stored verdict differs from the first judge's, and draws them at random instead of half pass / half fail." });
    const seed = h("input", { type: "number", value: store.get("lab_audit_seed", "20261112"), style: "width:130px", title: "random seed; write it in the pre-registration before drawing" });
    const overlap = h("input", { type: "number", min: "0", max: "5", value: store.get("lab_audit_overlap", "1"), style: "width:80px", title: "problems each person also labels from the previous person's set" });
    const out = h("p", { class: "hint", text: st.preview || "" });
    const body = () => ({ name: name.value, labelers: people.value.split(",").map((s) => s.trim()).filter(Boolean), groups: groups.value, by: (typeof who === "function" ? who() : ""), assign: assign.value, overlap_problems: parseInt(overlap.value || "0", 10) || 0, seed: parseInt(seed.value || "20261112", 10) || 20261112, disputed_with: disputed.value.trim(), ...(disputed.value.trim() ? { max_per_problem: 99 } : {}) });
    const remember = () => { store.set("lab_audit_name", name.value); store.set("lab_audit_people", people.value); store.set("lab_audit_groups", groups.value); store.set("lab_audit_overlap", overlap.value); store.set("lab_audit_disputed", disputed.value); store.set("lab_audit_seed", seed.value); store.set("lab_audit_assign", assign.value); };
    const go = async (preview) => {
      remember();
      const r = await ownerCall("POST", "/api/audit/draw", { ...body(), preview });
      st.preview = r.ok ? `${preview ? "Preview: " : "Drawn: "}${r.data.items} runs; ${assign.value === "all" ? "every labeler gets all of them" : "split between the labelers"}. ${(r.data.notes || []).join(" | ")}` : (r.data.error || "Failed.");
      if (r.ok && !preview) { st.audit = r.data.audit_id; store.set("lab_audit_id", st.audit); st.labeler = ""; st.rjNext = undefined; st.score = null; await load(); } else render();
    };
    box.append(
      h("p", { class: "hint", text: "Draw once, on the final exports; the sample stores run ids, so later runs cannot change it. Half of every stratum is judge-pass and half judge-fail. With whole problems per person, nobody learns more problems than needed; the overlap problems (labelled by two people) are what measure how far the labelers agree. After labels arrive you can still redistribute, add runs or remove runs below, but not draw the same name again." }),
      h("div", { class: "row" }, name, people), h("p", { class: "hint", text: `Known names on this Lab: ${(cfg.people || []).join(", ") || "none yet"}. Type the names exactly as each person should appear.` }),
      h("div", { class: "hint", text: "Groups: one line per `prefix | split or pooled | runs`. split = one stratum per configuration starting with the prefix." }), groups,
      h("div", { class: "row", style: "margin-top:8px" }, assign, h("span", { class: "hint", text: "Overlap problems per person:" }), overlap, h("span", { class: "hint", text: "Seed:" }), seed),
      h("div", { class: "row", style: "margin-top:8px" }, disputed),
      h("div", { class: "row", style: "margin-top:8px" },
        h("button", { type: "button", class: "secondary", text: "Preview", onclick: () => go(true) }),
        h("button", { type: "button", class: "primary", text: "Draw and assign", onclick: () => go(false) })), out,
      table(["Configuration", "Eligible runs", "Judge passed"], (cfg.configs || []).map((c) => [c.config_label || "(none)", c.eligible, c.judged_pass])),
      manageView(),
      secondJudgeBox(),
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
      m.labelers.some((l) => l.items < m.items) ? h("div", { class: "aa-how" },
        h("b", { text: `Each labeler holds only part of the ${m.items} items.` }),
        h("p", { class: "hint", text: "For the frozen-30 hand label every labeler must label every item. This keeps the same items and every label already given." }),
        h("button", { type: "button", class: "primary", text: "Give every labeler all items", onclick: () => edit({ action: "assign_all" }, (d) => `Done: every labeler now holds all ${d.items} items.`) })) : null,
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
  let rjTimer = null;
  async function pollRejudge(reset) {
    if (reset) { st.rjLog = ""; st.rjNext = 0; }
    try {
      const d = await jsonGet(`/api/audit/rejudge/status?audit_id=${encodeURIComponent(st.audit)}&since=${st.rjNext || 0}`);
      st.rjLog = (st.rjLog || "") + d.lines.join("");
      st.rjNext = d.next;
      st.rjRunning = d.running;
      st.rjVerdicts = d.verdicts || {};
      st.rjModel = d.model;
    } catch (e) { /* server busy: try again on the next tick */ }
    const p = document.getElementById("aa-rj-log");
    if (p) { p.textContent = st.rjLog; p.scrollTop = p.scrollHeight; }
    const c = document.getElementById("aa-rj-count");
    if (c) c.textContent = rjCountText();
    clearTimeout(rjTimer);
    if (st.rjRunning) rjTimer = setTimeout(() => pollRejudge(false), 2500);
    else if (st.rjWasRunning) { st.rjWasRunning = false; render(); }
    if (st.rjRunning) st.rjWasRunning = true;
  }
  function rjCountText() {
    const v = st.rjVerdicts || {};
    const parts = Object.entries(v).map(([m, x]) => `${m}: ${x.done} of ${x.of} have a verdict${x.errors ? `, ${x.errors} errors (retried next time)` : ""}`);
    return (st.rjRunning ? `Running (${st.rjModel}). It keeps going if you close or refresh this page. ` : "") + (parts.join(" · ") || "No second-judge verdicts yet.");
  }
  function secondJudgeBox() {
    if (!st.audit) return h("span");
    if (st.rjNext === undefined) { st.rjNext = 0; pollRejudge(true); }
    const model = h("input", { placeholder: "judge model, as SREGym's judge takes it (e.g. openai/<model>)", value: store.get("lab_audit_judge2", ""), style: "flex:1 1 360px" });
    const go = async () => {
      const m = model.value.trim();
      store.set("lab_audit_judge2", m);
      const r = await ownerCall("POST", "/api/audit/rejudge/start", { audit_id: st.audit, judge_model: m });
      if (!r.ok) { st.rjLog = (st.rjLog || "") + (r.data.error || "Could not start.") + "\n"; render(); return; }
      st.rjRunning = true; render(); pollRejudge(false);
    };
    const log = h("pre", { class: "detail-body", style: "max-height:220px;overflow:auto", text: st.rjLog || "" });
    log.id = "aa-rj-log";
    const count = h("p", { class: "hint", text: rjCountText() });
    count.id = "aa-rj-count";
    return h("div", { style: "margin-top:12px" }, h("h3", { text: "Second judge on this sample" }),
      h("p", { class: "hint", text: "Re-grades the same diagnoses with another model through SREGym's own judge (same reference answer, only the judge model changes). Runs in the background on this machine, inside SREGym's environment, with that model's API key set; each verdict is saved as it arrives, and runs that already have a verdict from the same model are skipped, so pressing it again resumes. This measures agreement between two judges, not correctness: it complements a human audit, it does not replace one." }),
      h("div", { class: "row" }, model, h("button", { type: "button", class: "secondary", disabled: !!st.rjRunning, text: st.rjRunning ? "Re-judging…" : "Re-judge this sample", onclick: go })),
      count, st.rjLog ? log : null);
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
      (s.second_judges || []).map((sj) => h("div", {}, h("h3", { text: `Second judge: ${sj.model} (${sj.rejudged} of ${sj.of} re-judged)` }),
        table(["Group", "n", "Agree", "95% Wilson", "Kappa", "Original pass, second fail", "Original fail, second pass", "Pass rate original / second"],
          sj.rows.map((r) => [r.group, r.n, `${r.agree} (${Math.round(100 * r.agree / r.n)}%)`, lo(r.wilson), kap(r.kappa), r.orig_pass_second_fail, r.orig_fail_second_pass, `${r.orig_pass}/${r.n} vs ${r.second_pass}/${r.n}`])),
        sj.vs_human ? h("p", { class: "hint", text: `Against the human labels: ${sj.vs_human.agree} of ${sj.vs_human.n} agree, kappa ${kap(sj.vs_human.kappa)}.` }) : null,
        sj.disagreements.length ? table(["Item", "Stratum", "Problem", "Original judge", "Second judge"], sj.disagreements.map((d) => [d.item_id, d.stratum, d.problem_id, d.original ? "pass" : "fail", d.second ? "pass" : "fail"])) : null)),
      s.disagreements.length ? table(["Item", "Stratum", "Problem", "Judge", "Human"], s.disagreements.map((d) => [d.item_id, d.stratum, d.problem_id, d.judge ? "pass" : "fail", d.human ? "correct" : "incorrect"])) : null);
  }

  function render() {
    root.replaceChildren(h("section", { class: "card aa" }, h("h2", { text: "Hand label: was the agent's diagnosis right?" }),
      h("p", { class: "hint", text: "A blind sample drawn by the owner. For each diagnosis you see the problem and its answer key, never the configuration or either judge's verdict." }),
      labelerPanel()), ownerPanel());
  }
  const previous = TAB_LOADERS.audit;
  TAB_LOADERS.audit = () => {
    if (previous) previous();
    load(true).catch((e) => root.replaceChildren(h("section", { class: "card" }, h("h2", { text: "Hand label" }),
      h("p", { class: "aa-err", text: "This tab hit an error: " + (e && e.message ? e.message : e) + ". Pull the Lab, restart it and refresh with Ctrl+Shift+R; if it stays, send a screenshot of this message." }))));
  };
})();
