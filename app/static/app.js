"use strict";
// Hufu front end. Everything that came from the model, the sandbox or a supplier is untrusted:
// it is only ever written with textContent, never as HTML.

(function () {
  const fonts = document.getElementById("webfonts");
  if (fonts) fonts.media = "all"; // async font load; the CSP forbids inline onload

  const $ = (id) => document.getElementById(id);
  const state = { preset: "pay", status: null, run: null, lastSeq: -1, timer: null, contained: 0,
                  st: null, stTimer: null };
  const BAD_KINDS = new Set(["rejected", "error", "approval_refused", "rejected_by_human", "run_error"]);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function add(parent, ...kids) { kids.forEach((k) => k && parent.appendChild(k)); return parent; }
  function money(minor, currency) {
    if (minor === null || minor === undefined) return "?";
    const n = (minor / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return currency === "USD" || !currency ? "$" + n : n + " " + currency;
  }
  function toast(msg, err) {
    const t = el("div", "toast" + (err ? " err" : ""), msg);
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 5200);
  }
  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) throw new Error((body && body.detail) || res.status + " " + res.statusText);
    return body;
  }
  function setNum(id, value) {
    const node = $(id);
    const s = String(value);
    if (node.textContent !== s) {
      node.textContent = s;
      node.classList.remove("flash"); void node.offsetWidth; node.classList.add("flash");
    }
  }

  // ---------- status, presets, data ----------
  function pill(label, value, level) {
    const p = el("span", "pill");
    add(p, el("span", "dot " + level), el("span", null, label), el("b", null, value));
    return p;
  }

  async function loadStatus() {
    const s = await api("/api/status");
    state.status = s;
    const pills = $("pills");
    pills.textContent = "";
    const inf = s.inference;
    add(pills,
      pill("Vultr Inference", inf.configured ? inf.model : "not configured", inf.configured ? "ok" : "warn"),
      pill("Sandbox", s.sandbox.checking ? "checking" : s.sandbox.ready ? (s.sandbox.runtime === "runsc" ? "gVisor" : s.sandbox.runtime) : "unavailable",
        s.sandbox.checking ? "" : s.sandbox.ready ? (s.sandbox.runtime === "runsc" ? "ok" : "warn") : "bad"),
      pill("Host", s.host, s.host === "local development" ? "warn" : "ok"));
    $("box-runtime").textContent = s.sandbox.ready
      ? (s.sandbox.runtime === "runsc" ? "Docker + gVisor" : "Docker (" + s.sandbox.runtime + ")") : "no sandbox";
    $("foot-host").textContent = s.host + " · models via " + inf.base_url;

    if (state.built) { updateButtons(); return repoll(s); }
    state.built = true;
    const presets = $("presets");
    presets.textContent = "";
    s.presets.forEach((p) => {
      const b = el("button", "preset", p.title);
      b.setAttribute("role", "tab");
      b.dataset.key = p.key;
      b.addEventListener("click", () => selectPreset(p.key));
      presets.appendChild(b);
    });
    const clauses = $("clauses");
    clauses.textContent = "";
    Object.entries(s.clauses).forEach(([id, text]) => {
      const li = el("li");
      add(li, el("b", null, id), el("span", null, text));
      clauses.appendChild(li);
    });
    selectPreset(state.preset);
    updateButtons();
    repoll(s);
  }

  // until the sandbox answers, keep asking; presets and the prompt box are built once so typing is not lost
  function repoll(s) {
    if (!s.sandbox.ready) setTimeout(() => loadStatus().catch(() => {}), s.sandbox.checking ? 1500 : 5000);
  }

  function selectPreset(key) {
    state.preset = key;
    const p = state.status.presets.find((x) => x.key === key);
    document.querySelectorAll(".preset").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.key === key)));
    if (p) $("prompt").value = p.prompt;
    updateButtons();
  }

  function updateButtons() {
    const s = state.status;
    const busy = state.run && state.run.state === "running";
    const ready = s && s.sandbox.ready;
    $("run-live").disabled = !ready || !s.inference.configured || busy;
    $("run-worst").disabled = !ready || state.preset !== "pay" || busy;
    $("run-selftest").disabled = !ready || (state.st && state.st.state === "running");
    let hint = "The worst case replays a model that obeys everything it reads, including the note a fraudster hid in invoice INV-1106. The live run sends the same task to a model on Vultr Serverless Inference.";
    if (s && s.sandbox.checking) hint = "Checking the sandbox on this server...";
    else if (s && !s.sandbox.ready) hint = "The sandbox is not available on this server: " + (s.sandbox.error || "Docker is not reachable") + ".";
    else if (s && !s.inference.configured) hint = "Vultr Serverless Inference is not configured here, so only the worst-case replay can run. " + hint;
    else if (state.preset !== "pay") hint = "The worst-case replay exists for the three-way match task. This task runs live on Vultr Serverless Inference.";
    $("mode-hint").textContent = hint;
  }

  async function loadData() {
    const d = await api("/api/datasets");
    const inv = $("invoices");
    inv.textContent = "";
    const head = el("tr");
    ["Invoice", "Vendor · PO", "Amount"].forEach((h, i) => head.appendChild(el("th", i === 2 ? "num" : null, h)));
    add(inv, add(el("thead"), head));
    const body = el("tbody");
    d.invoices.forEach((r) => {
      const poisoned = /assistant|honeypot|bank has changed/i.test(r[7] || "");
      const tr = el("tr", (poisoned ? "poisoned " : "") + (r[7] ? "has-note" : ""));
      const first = el("td");
      add(first, el("div", "mono strong", r[0]), el("div", "sub2", r[3] + " · " + Number(r[4]).toLocaleString("en-US") + " × $" + r[5]));
      const second = el("td");
      add(second, el("div", null, r[1]), el("div", "sub2", r[2]));
      tr.appendChild(first);
      tr.appendChild(second);
      tr.appendChild(el("td", "num", "$" + Number(r[6]).toLocaleString("en-US", { minimumFractionDigits: 2 })));
      body.appendChild(tr);
      if (r[7]) {
        const nr = el("tr", "note-row" + (poisoned ? " poisoned" : ""));
        const td = el("td", "note");
        td.colSpan = 3;
        add(td, el("span", "untrusted-tag", "UNTRUSTED NOTE"));
        td.appendChild(document.createTextNode(r[7]));
        add(body, add(nr, td));
      }
    });
    inv.appendChild(body);

    const ven = $("vendors");
    ven.textContent = "";
    const vh = el("tr");
    ["Vendor", "Account on file"].forEach((h) => vh.appendChild(el("th", null, h)));
    add(ven, add(el("thead"), vh));
    const vb = el("tbody");
    d.vendor_master.forEach((r) => {
      const tr = el("tr");
      const name = el("td");
      add(name, el("div", "strong", r[1]), el("div", "sub2", r[0] + " · " + String(r[3]).replace(/_/g, " ")));
      tr.appendChild(name);
      tr.appendChild(el("td", "mono", r[2]));
      vb.appendChild(tr);
    });
    ven.appendChild(vb);
  }

  // ---------- runs ----------
  async function startRun(mode) {
    try {
      const body = { mode, preset: state.preset, prompt: $("prompt").value };
      const r = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
      resetRun(r.run_id);
      poll();
    } catch (e) { toast(e.message, true); }
  }

  function resetRun(runId) {
    state.run = { run_id: runId, state: "running" };
    state.lastSeq = -1;
    state.contained = 0;
    clearTimeout(state.timer);
    if (grow) grow.disconnect();
    $("steps").textContent = "";
    stick = true;
    autoTop = 0;
    $("chain").textContent = "";
    $("approvals").textContent = "";
    $("empty").hidden = true;
    $("verdict").hidden = true;
    $("chain-download").hidden = false;
    $("chain-download").href = "/api/runs/" + encodeURIComponent(runId) + "/audit.jsonl";
    document.body.classList.remove("joined");
    ["fig-escapes", "fig-blocked", "fig-refused"].forEach((id) => { $(id).textContent = "0"; });
    updateButtons();
  }

  async function poll() {
    const run = state.run;
    if (!run) return;
    let v;
    try {
      v = await api("/api/runs/" + encodeURIComponent(run.run_id) + "?after=" + state.lastSeq);
    } catch (e) { toast(e.message, true); return; }
    if (state.run !== run) return; // a newer run started meanwhile
    run.state = v.state;
    v.records.forEach(renderRecord);
    follow(); // after the whole batch is filled in, so the scroll lands on the real bottom
    if (v.records.length) state.lastSeq = v.records[v.records.length - 1].seq;
    renderSummary(v);
    updateButtons();
    const pending = v.summary.pending.length;
    if (v.state === "running") state.timer = setTimeout(poll, 700);
    else if (pending) state.timer = setTimeout(poll, 2500);
  }

  let stick = true;
  // a step can grow after it is added (a chart decodes, a font arrives): keep the bottom in view
  const grow = typeof ResizeObserver === "function" ? new ResizeObserver(() => follow()) : null;
  let autoTop = 0;
  function follow() {
    const list = $("steps");
    if (!stick) return;
    list.scrollTop = list.scrollHeight;
    autoTop = list.scrollTop;
  }
  // stop following only when the reader scrolls up past where we put them; back at the bottom, follow again
  document.addEventListener("DOMContentLoaded", () => {
    const list = $("steps");
    list.addEventListener("scroll", () => {
      const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
      if (list.scrollTop < autoTop - 4) stick = atBottom;
      else if (atBottom) stick = true;
    }, { passive: true });
  });

  function step(kind, cls, title, seq) {
    const li = el("li", "step " + cls);
    li.dataset.n = seq;
    const k = el("div", "kind");
    add(k, el("span", "sw"), el("span", null, kind));
    add(li, k);
    if (title) add(li, el("div", "title", title));
    const list = $("steps");
    list.appendChild(li);
    if (grow) grow.observe(li);
    return li;
  }

  function renderRecord(r) {
    const p = r.payload;
    addDot(r);
    switch (r.kind) {
      case "run_started": {
        const s = step("Run started", "info", p.mode === "live" ? "Live run on Vultr Serverless Inference" : "Worst-case replay", r.seq);
        add(s, el("div", "body", "Model: " + p.model + " · mandate " + p.mandate));
        break;
      }
      case "user_message": {
        const s = step("Task", "info", null, r.seq);
        add(s, el("div", "body", p.text));
        break;
      }
      case "model_turn": {
        const calls = p.tool_calls || [];
        const title = calls.length
          ? "Asks for " + calls.map((c) => c.name + (c.arguments && c.arguments.invoice_id ? " " + c.arguments.invoice_id : "")).join(", ")
          : "Writes its answer";
        const s = step("Model · " + p.model + (p.latency_ms >= 50 ? " · " + (p.latency_ms / 1000).toFixed(1) + " s" : ""), "model", title, r.seq);
        if (p.content) add(s, el("div", "body", p.content));
        const purposes = calls.map((c) => c.arguments && c.arguments.purpose).filter(Boolean);
        if (purposes.length) add(s, el("div", "body", purposes.join("; ")));
        break;
      }
      case "sandbox_run": renderSandbox(r); break;
      case "decision": renderDecision(r); break;
      case "executed": {
        if (p.tool === "pay_invoice") {
          let res = {};
          try { res = JSON.parse(p.result); } catch (e) { res = {}; }
          const s = step("Money moved", "paid", "Paid " + (res.invoice_id || "") + " · " + (res.amount || "") + " to " + (res.to || ""), r.seq);
          add(s, el("div", "body", "Account " + (res.account || "?") + (p.approved_by ? " · approved by " + p.approved_by : " · within clause " + (p.clauses || []).join(", "))));
        } else if (p.tool === "preview_data") {
          let names = [];
          try { names = Object.entries(JSON.parse(p.result)).map(([k, v]) => k + " (" + v.rows + " rows)"); } catch (e) { names = []; }
          const s = step("Read the data", "info", "Previewed the datasets in /data", r.seq);
          add(s, el("div", "body", names.join(" · ")));
        }
        break;
      }
      case "escalation": {
        const s = step("Waiting", "ask", "Held for the AP manager (" + p.escalation_id + ")", r.seq);
        add(s, el("div", "body", "Above the amount the agent may pay on its own. Approve or reject it in the panel on the right."));
        break;
      }
      case "approved": step("Approved", "allow", "The AP manager approved " + p.escalation_id + (p.facts_changed ? " (facts re-checked and changed)" : ""), r.seq); break;
      case "rejected_by_human": step("Rejected", "deny", "The AP manager rejected " + p.escalation_id, r.seq); break;
      case "approval_refused": {
        const s = step("Approval refused", "deny", "The mandate refused " + p.escalation_id + " even with approval", r.seq);
        add(s, reasonList(p.reasons, []));
        break;
      }
      case "rejected": {
        const s = step("Rejected", "deny", p.tool + " was refused before policy", r.seq);
        add(s, el("div", "body", p.reason));
        break;
      }
      case "error": {
        const s = step("Tool error", "deny", p.tool + " failed", r.seq);
        add(s, el("div", "body", p.error));
        break;
      }
      case "observed": step("Observed", "info", p.tool + " would be " + p.would_be + "; nothing ran", r.seq); break;
      case "run_finished": {
        const s = step("Final report", "model", p.stopped_by_step_limit ? "Stopped at the step limit" : "The model's own account", r.seq);
        add(s, el("div", "body", p.final_text || "(empty)"));
        break;
      }
      case "run_error": {
        const s = step("Run error", "deny", "The run stopped", r.seq);
        add(s, el("div", "body", p.error));
        break;
      }
      default: step(r.kind, "info", null, r.seq);
    }
  }

  const CONTAINED_TEXT = {
    network: "network blocked", read_only: "write blocked: read-only", permission: "permission denied",
    process_limit: "process limit", memory: "memory limit", time_limit: "time limit",
  };

  function renderSandbox(r) {
    const p = r.payload;
    const failed = p.exit_code !== 0;
    const s = step("Sandbox run " + p.run + " · " + (p.runtime === "runsc" ? "gVisor" : p.runtime) + " · " + p.duration_ms + " ms",
      "box", p.purpose || "Python script", r.seq);
    const det = el("details");
    add(det, el("summary", null, "Code the model wrote (" + p.code.split("\n").length + " lines)"), el("pre", "code", p.code));
    add(s, det);
    if (p.stdout && p.stdout.trim()) add(s, el("pre", "out", p.stdout.trim().slice(-4000)));
    if (p.stderr && p.stderr.trim()) add(s, el("pre", "out err", p.stderr.trim().slice(-2500)));
    const contained = p.contained || [];
    state.contained += contained.length;
    if (contained.length) {
      const stamps = el("div", "stamps");
      contained.forEach((c) => stamps.appendChild(el("span", "stamp", "contained · " + (CONTAINED_TEXT[c] || c))));
      add(s, stamps);
    }
    (p.saved_files || []).forEach((f) => {
      const url = "/api/runs/" + encodeURIComponent(state.run.run_id) + "/files/" + encodeURIComponent(f.name);
      if (/\.png$/i.test(f.name)) {
        const fig = el("div", "figure");
        const img = el("img");
        img.src = url; img.alt = "Chart returned by sandbox run " + p.run; img.loading = "lazy";
        add(s, add(fig, img));
      } else {
        const a = el("a", "link", "Returned file: " + f.name + " (" + f.size + " bytes)");
        a.href = url; a.target = "_blank"; a.rel = "noopener";
        add(s, a);
      }
    });
    const lim = p.limits || {};
    add(s, el("div", "receipt",
      "container " + p.container + " · network " + p.network + " · " + (lim.memory_mb || "?") + " MB · " +
      (lim.pids || "?") + " pids · exit " + p.exit_code + (p.killed ? " (killed: " + p.killed + ")" : "") +
      " · " + (p.destroyed ? "destroyed" : "not destroyed") + " · code sha256 " + String(p.code_sha256 || "").slice(0, 12) +
      (failed && !contained.length ? " · the error goes back to the model to fix" : "")));
  }

  function reasonList(reasons, matched) {
    const ul = el("ul", "reasons");
    (reasons || []).forEach((t) => {
      const hit = (matched || []).some((m) => t.indexOf(m) === 0);
      ul.appendChild(el("li", hit ? "hit" : "", t));
    });
    return ul;
  }

  function renderDecision(r) {
    const p = r.payload;
    const f = p.facts || {};
    const effect = p.effect;
    const label = { allow: "ALLOW", ask: "ASK", deny: "DENY", observe: "OBSERVE" }[effect] || effect;
    const s = step("Mandate · " + p.mandate, effect === "allow" ? "allow" : effect === "ask" ? "ask" : effect === "deny" ? "deny" : "info", null, r.seq);
    const t = el("div", "title");
    add(t, el("span", "verdict-word " + effect, label));
    t.appendChild(document.createTextNode(p.tool + " " + ((p.args && p.args.invoice_id) || "") + " · " + money(f.amount_minor, f.currency) + " to " + (f.counterparty_name || "?")));
    add(s, t);
    const why = el("details", "why");
    why.open = effect !== "allow";
    add(why, el("summary", null, ((p.matched || []).length ? "Clauses " + p.matched.join(", ") : "No clause matched") + " · " + (p.reasons || []).length + " checks"), reasonList(p.reasons, p.matched));
    add(s, why);
    const prov = (p.provenance || {}).account;
    if (prov) {
      const box = el("div", "prov");
      add(box, el("b", null, "Account " + (f.counterparty_account || "?") + ": "));
      const trusted = ["mandate", "system", "user"].indexOf(prov.best) >= 0;
      add(box, el("span", trusted ? "good" : "bad", prov.best));
      const seen = (prov.seen_in || []).map((x) => x.replace(/^tool:/, "").replace("#", " #"));
      box.appendChild(document.createTextNode(seen.length ? " · seen in " + seen.join(", ") : " · seen nowhere"));
      add(s, box);
    }
  }

  function addDot(r) {
    const d = el("i", r.kind);
    if (BAD_KINDS.has(r.kind) || (r.kind === "decision" && r.payload.effect === "deny")) d.classList.add("bad");
    d.title = "#" + r.seq + " " + r.kind + " · " + r.hash.slice(0, 12);
    $("chain").appendChild(d);
  }

  function renderSummary(v) {
    const sm = v.summary;
    $("box-runs").textContent = sm.sandbox_runs;
    const bc = $("box-contained");
    bc.textContent = sm.contained.length ? "" : "none yet";
    sm.contained.forEach((c) => add(bc, el("span", null, CONTAINED_TEXT[c] || c)));
    $("box-honeypot").textContent = sm.honeypot_hits;
    $("m-paid").textContent = sm.payments.length + (sm.payments.length ? " · " + money(sm.payments.reduce((a, x) => a + x.amount_minor, 0), "USD") : "");
    $("m-waiting").textContent = sm.pending.length;
    $("m-refused").textContent = sm.refused;
    setNum("fig-escapes", sm.honeypot_hits);
    setNum("fig-blocked", state.contained);
    setNum("fig-refused", sm.refused);

    const c = sm.chain;
    $("chain-state").textContent = c.records ? (c.intact ? "intact · " + c.records + " records" : "BROKEN at #" + c.broken_at) : "empty";
    $("chain-head").textContent = c.head ? "head " + c.head : "";

    const ap = $("approvals");
    ap.textContent = "";
    sm.pending.forEach((e) => {
      const box = el("div", "approval");
      add(box, el("b", null, "Approve " + ((e.args && e.args.invoice_id) || e.escalation_id) + "? "));
      box.appendChild(document.createTextNode(money(e.amount_minor, e.currency) + " to " + e.counterparty));
      add(box, reasonList(e.reasons, []));
      const row = el("div", "row");
      const yes = el("button", "btn small ok", "Approve as AP manager");
      const no = el("button", "btn small no", "Reject");
      yes.addEventListener("click", () => decide(e.escalation_id, "approve", yes, no));
      no.addEventListener("click", () => decide(e.escalation_id, "reject", yes, no));
      add(row, yes, no);
      add(box, row);
      ap.appendChild(box);
    });

    if (v.state !== "running") renderVerdict(v);
    document.body.classList.toggle("joined", v.state === "done" && c.intact && sm.honeypot_hits === 0);
    const rs = $("run-state");
    rs.textContent = v.state === "running" ? "running" : v.state === "done" ? "done · " + v.run_id : v.state;
    rs.className = "run-state" + (v.state === "running" ? " running" : "");
  }

  function renderVerdict(v) {
    const sm = v.summary;
    $("verdict").hidden = false;
    $("claim").textContent = v.final_text ? "“" + v.final_text.slice(0, 600) + "”" : (v.error || "No final report.");
    const ul = $("actual");
    ul.textContent = "";
    const li = (cls, text) => ul.appendChild(el("li", cls, text));
    sm.payments.forEach((x) => li("good", "Paid " + x.invoice_id + " · " + money(x.amount_minor, x.currency) + " to " + x.to));
    sm.pending.forEach((e) => li("wait", "Waiting for the AP manager: " + ((e.args && e.args.invoice_id) || "") + " · " + money(e.amount_minor, e.currency)));
    if (sm.refused) li("bad", sm.refused + " payment" + (sm.refused > 1 ? "s" : "") + " refused by the mandate");
    li(sm.honeypot_hits ? "bad" : "good", sm.honeypot_hits ? sm.honeypot_hits + " request(s) reached the honeypot" : "Nothing reached the honeypot: 0 requests left the box");
    if (sm.contained.length) li("good", "The box stopped: " + sm.contained.map((c) => CONTAINED_TEXT[c] || c).join(", "));
    li(sm.chain.intact ? "good" : "bad", sm.chain.intact ? "Audit chain verifies: " + sm.chain.records + " records" : "Audit chain broken at #" + sm.chain.broken_at);
  }

  async function decide(esc, verdict, ...buttons) {
    buttons.forEach((b) => { b.disabled = true; });
    try {
      await api("/api/runs/" + encodeURIComponent(state.run.run_id) + "/escalations/" + encodeURIComponent(esc) + "/" + verdict, { method: "POST" });
      clearTimeout(state.timer);
      poll();
    } catch (e) { toast(e.message, true); buttons.forEach((b) => { b.disabled = false; }); }
  }

  // ---------- self-test ----------
  async function startSelftest() {
    try {
      const r = await api("/api/selftest", { method: "POST" });
      state.st = { test_id: r.test_id, state: "running" };
      updateButtons();
      pollSelftest();
    } catch (e) { toast(e.message, true); }
  }

  async function pollSelftest() {
    let v;
    try { v = await api("/api/selftest/" + encodeURIComponent(state.st.test_id)); } catch (e) { toast(e.message, true); return; }
    state.st = v;
    renderSelftest(v);
    updateButtons();
    if (v.state === "running") state.stTimer = setTimeout(pollSelftest, 700);
  }

  function renderSelftest(v) {
    const table = $("st-table");
    table.textContent = "";
    const head = el("tr");
    ["#", "Attack", "What it tries", "Result", "Evidence from the sandbox"].forEach((h) => head.appendChild(el("th", null, h)));
    add(table, add(el("thead"), head));
    const body = el("tbody");
    v.results.forEach((r, i) => {
      const tr = el("tr", r.key === "control" ? "control" : "");
      tr.appendChild(el("td", "mono", i === 0 && r.key === "control" ? "C" : i));
      tr.appendChild(el("td", null, r.title));
      tr.appendChild(el("td", null, r.attack));
      const res = r.passed === null ? "running" : r.key === "control" ? (r.passed ? "REACHED" : "FAILED") : r.passed ? "BLOCKED" : "FAIL";
      tr.appendChild(el("td", "res " + (r.passed === null ? "run" : r.passed ? "pass" : "fail"), res));
      tr.appendChild(el("td", "ev", r.evidence));
      body.appendChild(tr);
    });
    table.appendChild(body);
    let bar = $("st-progress");
    if (!bar) {
      bar = add(el("div", "progress"), el("span"));
      bar.id = "st-progress";
      table.parentNode.parentNode.insertBefore(bar, table.parentNode);
    }
    const done = v.results.filter((r) => r.passed !== null).length;
    bar.firstChild.style.width = Math.round((100 * done) / v.total) + "%";
    const score = $("st-score");
    score.textContent = v.passed + " / " + v.total;
    score.className = "st-score" + (v.state === "done" && v.passed === v.total ? " allpass" : "");
    if (v.state === "done" && v.leftover_containers !== null) {
      tr2(body, "Containers left on the host after the test: " + v.leftover_containers + " · runtime " + v.runtime);
    } else if (v.state.indexOf("error") === 0) {
      tr2(body, v.state);
    }
  }
  function tr2(body, text) {
    const tr = el("tr");
    const td = el("td", "ev", text);
    td.colSpan = 5;
    add(body, add(tr, td));
  }

  // ---------- wire up ----------
  $("run-live").addEventListener("click", () => startRun("live"));
  $("run-worst").addEventListener("click", () => startRun("worst_case"));
  $("run-selftest").addEventListener("click", startSelftest);
  Promise.all([loadStatus(), loadData()]).catch((e) => toast("Could not load the page data: " + e.message, true));
})();
