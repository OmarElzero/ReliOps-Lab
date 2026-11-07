// ReliOps Lab — Tasks tab: what each person has to do now. The owner writes the tasks (needs the owner password of his Lab);
// everyone reads them, filters by their own name and marks their own progress. Loaded after app.js, v2.js and queue.js.

(() => {
  const root = document.getElementById("tk-root");
  if (!root) return;
  const pref = {
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
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    kids.flat().forEach((c) => { if (c != null && c !== false) n.append(c.nodeType ? c : document.createTextNode(String(c))); });
    return n;
  }
  const STATUS = { todo: "To do", doing: "Doing", done: "Done", blocked: "Blocked" };
  let data = { tasks: [], progress: [], locked_off: true };
  let mode = pref.get("lab_tasks_mode", "mine");       // "mine" | "all"
  let showDone = pref.get("lab_tasks_done", "0") === "1";
  let owner = false;                                      // the password was accepted by the server this session
  let queues = [];
  let msg = "";

  const me = () => (typeof who === "function" ? who() : "").trim();
  const isFor = (t, name) => !(t.assignees || []).length || (t.assignees || []).some((n) => n.toLowerCase() === (name || "").toLowerCase());
  const progressOf = (t) => data.progress.filter((p) => p.task_id === t.task_id);
  const myStatus = (t) => { const p = progressOf(t).find((x) => (x.who_key || "") === me().toLowerCase()); return p ? p.status : "todo"; };
  const openFor = (t) => isFor(t, me()) && me() && myStatus(t) !== "done";

  function badge() {
    const n = data.tasks.filter(openFor).length;
    const b = document.getElementById("tasks-badge");
    if (b) { b.hidden = !n; b.textContent = String(n); b.title = `${n} open task${n === 1 ? "" : "s"} for ${me()}`; }
  }
  async function load() {
    try {
      const r = await jsonGet("/api/tasks");
      data = r && Array.isArray(r.tasks) ? r : { tasks: [], progress: [], locked_off: true };
      if (data.locked_off) { owner = false; pw.set(""); }
    } catch (e) { msg = "The Lab did not answer."; }
    try { queues = (await jsonGet("/api/queues")).queues || []; } catch (e) { queues = []; }
    if (!owner && pw.get() && !data.locked_off) await tryUnlock(pw.get(), true);
    render(); badge();
  }
  async function tryUnlock(secret, quiet) {
    const res = await fetch("/api/tasks/unlock", { method: "POST", headers: { "x-owner-password": secret } });
    owner = res.ok;
    if (res.ok) pw.set(secret); else { pw.set(""); if (!quiet) { let e = {}; try { e = await res.json(); } catch (x) { /* ignore */ } msg = e.error || "Could not unlock."; } }
    return res.ok;
  }
  async function ownerCall(method, url, body) {
    const res = await fetch(url, { method, headers: { "Content-Type": "application/json", "x-owner-password": pw.get() }, body: body ? JSON.stringify(body) : undefined });
    let out = {};
    try { out = await res.json(); } catch (e) { /* not JSON */ }
    if (res.status === 403) { owner = false; pw.set(""); }
    return { ok: res.ok, data: out };
  }
  async function setStatus(t, status, note) {
    if (!me()) { msg = "Type your name in the box at the top first."; render(); return; }
    const r = await jsonPost(`/api/tasks/${encodeURIComponent(t.task_id)}/progress`, { who: me(), status, note: note || "" });
    msg = r.ok ? "" : (r.data.error || "Could not save.");
    await load();
  }
  function openQueueOf(t) {
    if (t.queue_id && t.experiment) pref.set("lab_exp_" + t.queue_id, t.experiment);
    showTab("queue");
    const sel = document.getElementById("q-open");
    if (sel && t.queue_id && [...sel.options].some((o) => o.value === t.queue_id)) { sel.value = t.queue_id; sel.dispatchEvent(new Event("change")); }
    else if (t.queue_id) { msg = "That queue is not on this machine yet. Reload the Lab, or ask the owner."; showTab("tasks"); }
  }

  // ---- the owner's form: add or edit a task -------------------------------------------------------------------
  function openForm(t) {
    const d = h("dialog", { class: "q-modal wide" });
    document.body.append(d);
    d.addEventListener("close", () => d.remove());
    const names = new Set();
    data.tasks.forEach((x) => (x.assignees || []).forEach((n) => names.add(n)));
    data.progress.forEach((p) => p.who && names.add(p.who));
    if (me()) names.add(me());
    const chosen = new Set(((t && t.assignees) || []).map((n) => n.toLowerCase()));
    const boxes = [...names].sort((a, b) => a.localeCompare(b)).map((n) => h("label", {}, h("input", { type: "checkbox", value: n, checked: chosen.has(n.toLowerCase()) }), n));
    const f = {
      title: h("input", { type: "text", value: t ? t.title : "", placeholder: "e.g. Run STRATUS on the 12 dev problems", "aria-label": "Title" }),
      detail: h("textarea", { rows: "4", "aria-label": "Details", placeholder: "What exactly to do, which settings, where to look, when to stop." }),
      phase: h("input", { type: "text", value: t ? t.phase : "", list: "tk-phases", placeholder: "e.g. Round 2 - main stage", "aria-label": "Phase" }),
      extra: h("input", { type: "text", placeholder: "Other names, separated by commas", "aria-label": "Other names" }),
      queue: h("select", { "aria-label": "Queue" }, h("option", { value: "", text: "No queue" }), ...queues.map((q) => h("option", { value: q.queue_id, text: q.name, selected: !!t && t.queue_id === q.queue_id }))),
      exp: h("input", { type: "text", value: t ? t.experiment : "", placeholder: "Experiment name the runs go under", "aria-label": "Experiment name" }),
      due: h("input", { type: "text", value: t ? t.due : "", placeholder: "e.g. 9 Nov evening", "aria-label": "Due" }),
    };
    f.detail.value = t ? t.detail : "";
    const err = h("p", { class: "step-issue", hidden: true });
    const phases = h("datalist", { id: "tk-phases" }, ...[...new Set(data.tasks.map((x) => x.phase).filter(Boolean))].map((p) => h("option", { value: p })));
    const row = (label, el) => h("label", { class: "tk-field" }, h("span", { text: label }), el);
    d.append(h("h3", { text: t ? "Edit task" : "Add a task" }), phases,
      row("Title", f.title), row("Details", f.detail), row("Phase", f.phase),
      h("div", { class: "tk-field" }, h("span", { text: "For (leave all empty = everyone)" }), h("div", { class: "pick-list" }, boxes.length ? boxes : h("span", { class: "hint", text: "No names yet: type them below." })), f.extra),
      h("div", { class: "tk-two" }, row("Saved queue (optional)", f.queue), row("Experiment name (optional)", f.exp)),
      row("Due (optional, free text)", f.due), err,
      h("div", { class: "row-end" }, h("button", { class: "secondary", type: "button", text: "Cancel", onclick: () => d.close() }),
        h("button", { class: "primary", type: "button", text: t ? "Save" : "Add task", onclick: save })));
    d.showModal();
    async function save() {
      const sel = [...d.querySelectorAll('.pick-list input:checked')].map((x) => x.value);
      f.extra.value.split(",").map((x) => x.trim()).filter(Boolean).forEach((x) => { if (!sel.some((y) => y.toLowerCase() === x.toLowerCase())) sel.push(x); });
      const body = { title: f.title.value, detail: f.detail.value, phase: f.phase.value, assignees: sel, queue_id: f.queue.value, experiment: f.exp.value, due: f.due.value, run_by: me() || null };
      const r = await ownerCall(t ? "PUT" : "POST", t ? `/api/tasks/${encodeURIComponent(t.task_id)}` : "/api/tasks", body);
      if (!r.ok) { err.hidden = false; err.textContent = r.data.error || "Could not save."; if (!owner) { d.close(); msg = r.data.error || ""; load(); } return; }
      d.close(); msg = ""; load();
    }
  }
  let sure = "";   // id of the task whose Delete was pressed once
  async function remove(t) {
    if (sure !== t.task_id) { sure = t.task_id; render(); return; }
    sure = "";
    const r = await ownerCall("DELETE", `/api/tasks/${encodeURIComponent(t.task_id)}`);
    msg = r.ok ? "" : (r.data.error || "Could not delete.");
    load();
  }

  // ---- rendering ---------------------------------------------------------------------------------------------
  function card(t) {
    const prog = progressOf(t), mine = isFor(t, me()) && me(), st = myStatus(t);
    const who = (t.assignees || []).length ? t.assignees : [];
    const chips = who.map((n) => { const p = prog.find((x) => (x.who_key || "") === n.toLowerCase()); const s = p ? p.status : "todo";
      return h("span", { class: "tk-chip s-" + s, title: p && p.note ? p.note : "", text: `${n} · ${STATUS[s]}` }); });
    const others = who.length ? [] : prog.map((p) => h("span", { class: "tk-chip s-" + p.status, title: p.note || "", text: `${p.who} · ${STATUS[p.status]}` }));
    const notes = prog.filter((p) => p.note).map((p) => h("div", { class: "tk-note", text: `${p.who}: ${p.note}` }));
    const noteIn = h("input", { type: "text", placeholder: "Note (optional): what you did, what blocks you", "aria-label": "Your note", value: (prog.find((x) => (x.who_key || "") === me().toLowerCase()) || {}).note || "" });
    const mineRow = mine ? h("div", { class: "tk-mine" }, h("span", { class: "hint", text: "Your status" }),
      ...Object.keys(STATUS).map((k) => h("button", { class: "small " + (st === k ? "primary" : "secondary"), type: "button", text: STATUS[k], onclick: () => setStatus(t, k, noteIn.value) })), noteIn) : null;
    const ownerRow = owner ? h("div", { class: "tk-owner-row" },
      h("button", { class: "ghost small", type: "button", text: "Edit", onclick: () => openForm(t) }),
      h("button", { class: "ghost small bad", type: "button", text: sure === t.task_id ? "Press again to delete" : "Delete", onclick: () => remove(t) })) : null;
    return h("article", { class: "tk-card" + (mine && st === "done" ? " done" : "") },
      h("div", { class: "tk-top" }, h("h3", { text: t.title }), t.due ? h("span", { class: "tk-due", text: "Due " + t.due }) : null),
      t.detail ? h("p", { class: "tk-detail", text: t.detail }) : null,
      h("div", { class: "tk-meta" }, h("span", { class: "hint", text: who.length ? "For" : "For everyone" }), ...chips, ...others,
        t.queue_id ? h("button", { class: "secondary small", type: "button", text: "Open queue", title: t.experiment ? `Opens the queue with the experiment name "${t.experiment}"` : "Opens the queue", onclick: () => openQueueOf(t) }) : null),
      ...notes, mineRow, ownerRow);
  }
  function render() {
    const name = me();
    drawnFor = name;
    const list = data.tasks.filter((t) => (mode === "all" || (name && isFor(t, name))) && (showDone || mode === "all" || myStatus(t) !== "done" || !name));
    const groups = new Map();
    list.forEach((t) => { const k = t.phase || "No phase"; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(t); });
    const seg = (k, text) => h("button", { class: "seg-b" + (mode === k ? " on" : ""), type: "button", text, onclick: () => { mode = k; pref.set("lab_tasks_mode", k); render(); } });
    const ownerBox = data.locked_off
      ? h("span", { class: "hint", text: "Only the project owner's Lab can add tasks (it needs LAB_OWNER_PASSWORD in its .env)." })
      : owner ? h("span", {}, h("span", { class: "tk-ok", text: "Owner mode" }), " ", h("button", { class: "primary small", type: "button", text: "Add task", onclick: () => openForm(null) }), " ",
          h("button", { class: "ghost small", type: "button", text: "Lock", onclick: () => { owner = false; pw.set(""); render(); } }))
        : h("form", { class: "tk-unlock", onsubmit: async (e) => { e.preventDefault(); const v = e.target.elements.pw.value; if (await tryUnlock(v)) msg = ""; render(); } },
            h("input", { type: "password", name: "pw", placeholder: "Owner password", autocomplete: "off", "aria-label": "Owner password" }), h("button", { class: "secondary small", type: "submit", text: "Unlock to add tasks" }));
    root.replaceChildren(...[
      h("div", { class: "tk-head" },
        h("div", { class: "tk-seg" }, seg("mine", name ? `My tasks (${data.tasks.filter(openFor).length} open)` : "My tasks"), seg("all", "Everyone's")),
        mode === "mine" ? h("label", { class: "hint" }, h("input", { type: "checkbox", checked: showDone, onchange: (e) => { showDone = e.target.checked; pref.set("lab_tasks_done", showDone ? "1" : "0"); render(); } }), " show the ones I finished") : null,
        h("div", { class: "tk-ownerbox" }, ownerBox)),
      msg ? h("div", { class: "warn-box tk-msg", text: msg }) : null,
      mode === "mine" && !name ? h("div", { class: "warn-box tk-msg", text: "Type your name in the box at the top right to see the tasks that are yours. Until then, \"Everyone's\" shows all of them." }) : null,
      !list.length ? h("p", { class: "hint tk-empty", text: data.tasks.length ? (mode === "mine" ? "Nothing open for you. Everyone's shows the whole list." : "No tasks.") : "No tasks yet. The owner adds them here." }) : null,
      ...[...groups].map(([phase, ts]) => h("section", { class: "tk-phase" }, h("h2", {}, phase, h("span", { class: "hint", text: ` · ${ts.length} task${ts.length === 1 ? "" : "s"}` })), ...ts.map(card)))].filter((x) => x != null && x !== false));
  }

  TAB_LOADERS.tasks = () => load();
  const nameBox = document.getElementById("run-by");
  // Redraw while the name is typed (not only when the box loses focus): a redraw on blur would replace the button the
  // person is just clicking, and that first click would be lost.
  let drawnFor = null, typing = 0;
  const redraw = () => { if (me() === drawnFor) return; render(); badge(); };
  if (nameBox) { nameBox.addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(redraw, 250); }); nameBox.addEventListener("change", redraw); }
  load();
  setInterval(() => { if (!document.hidden) load(); }, 90000);   // light: one change-stamp read at most per refresh
})();
