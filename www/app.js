/* pimesh ui v2 — vanilla js, no deps */
"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
let CFG = { refresh_s: 5, route_hints: true, fast_thinking: false };
let statusTimer = null;
let chatHistory = [];   // {role, content}
let chatAbort = null;

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ms);
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()) || r.status);
  return r.json();
}
function gb(bytes) { return bytes ? (bytes / 1073741824).toFixed(1) : "?"; }
function pill(ok, up = "UP", down = "DOWN", loading = null) {
  return `<span class="pill ${ok ? "up" : "down"}">${ok ? up : down}</span>`;
}

/* ---------- navigation ---------- */
$$(".nav").forEach(a => a.onclick = () => {
  $$(".nav").forEach(x => x.classList.remove("active"));
  a.classList.add("active");
  $$(".view").forEach(v => v.classList.remove("active"));
  $("#view-" + a.dataset.view).classList.add("active");
  if (a.dataset.view === "models") refreshModels();
  if (a.dataset.view === "workers") refreshWorkers();
  if (a.dataset.view === "settings") refreshSettings();
});

/* ---------- overview + sidebar status ---------- */
function renderStatus(st) {
  $("#dot-fast").className = "dot " + (st.fast.up ? "on" : "off");
  $("#dot-deep").className = "dot " + (st.deep.running ? "on" : "off");

  const n = st.npu, s = st.sys;
  const npuTxt = n.present ? `${n.temp}°C · CMM ${n.cmm_used} / ${n.cmm_total}` : "card absent";
  const winfo = (st.workers_file && st.workers_file.workers) || [];
  const cards = [];

  cards.push(`<div class="card"><div class="card-title">gateway</div>
    <div class="metric">henry:8000</div>
    <div>uptime ${Math.floor(st.uptime_s / 60)} min · load ${(s.load || []).join(" ")}</div>
    <div class="dim small">openai endpoint: /v1 · models: axera-fast, mesh-30b, auto</div></div>`);

  cards.push(`<div class="card"><div class="card-title">axera-fast ${pill(st.fast.up)}</div>
    <div>LLM8850 NPU · ${npuTxt}</div>
    <div class="dim small">Qwen3-0.6B on the 8850 — always-on lane${st.fast.last_tps ? ` · last ${st.fast.last_tps} t/s` : ""}</div></div>`);

  const d = st.deep;
  const nameByIp = Object.fromEntries((st.workers || []).map(x => [x.ip, x.name]));
  const pool = (st.pool || "").split(",").filter(Boolean).map(ip => nameByIp[ip] || ip);
  const poolTxt = pool.length ? pool.join(" + ") : "none";
  cards.push(`<div class="card"><div class="card-title">mesh-30b ${pill(d.running, "UP", d.loading ? "LOADING" : "DOWN")}</div>
    <div class="small mono">${(d.model || "").split("/").pop()}</div>
    <div>ctx ${d.ctx} · pool: ${poolTxt}${d.last_tps ? ` · ${d.last_tps} t/s` : ""}</div>
    <div class="dim small">pooled-ram lane via llama.cpp rpc</div></div>`);

  const hpct = s.ram_total_gb ? Math.round(100 - (s.ram_avail_gb / s.ram_total_gb) * 100) : 0;
  cards.push(`<div class="card"><div class="card-title">henry · coordinator</div>
    <div class="metric">RAM ${hpct}% used</div>
    <div class="bar"><div style="width:${hpct}%"></div></div>
    <div>${s.ram_avail_gb} / ${s.ram_total_gb} GB free · temp ${s.temp}°C</div></div>`);

  for (const w of winfo) {
    const pct = w.mem_total ? Math.round(100 - (w.mem_avail / w.mem_total) * 100) : 0;
    cards.push(`<div class="card"><div class="card-title">${w.name} <span class="dim small">${w.ip}</span></div>
      <div class="metric">${w.ssh ? "online" : "offline"}</div>
      <div class="bar"><div style="width:${pct}%"></div></div>
      <div>RAM ${pct}% used${w.temp ? ` · ${w.temp}°C` : ""} ${w.rpc ? `· rpc ✓` : "· rpc ✗"}</div></div>`);
  }
  $("#ov-cards").innerHTML = cards.join("");

  const tb = $("#ov-reqlog tbody");
  tb.innerHTML = (st.history || []).map(h =>
    `<tr><td>${h.t}</td><td>${h.model}</td><td>${h.backend}</td><td>${h.status}</td><td class="dim">${h.note || ""}</td></tr>`).join("")
    || `<tr><td colspan="5" class="dim">no requests yet</td></tr>`;
}

async function pollStatus() {
  try {
    const st = await api("/api/status");
    CFG = st.config || CFG;
    renderStatus(st);
  } catch (e) { console.warn("status poll failed", e); }
}
function startPolling() {
  clearInterval(statusTimer);
  pollStatus();
  statusTimer = setInterval(pollStatus, Math.max(2, CFG.refresh_s || 5) * 1000);
}

/* ---------- chat ---------- */
function refreshModelSelect(st) {
  const sel = $("#chat-model");
  const cur = sel.value;
  const opts = [];
  if (st.fast.up) opts.push(["axera-fast", "axera-fast · NPU"]);
  if (st.deep.running) opts.push(["mesh-30b", "mesh-30b · pooled"]);
  opts.push(["auto", "auto (route by prompt)"]);
  sel.innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
}

function addMsg(role, text, meta) {
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
  if (meta) { const m = document.createElement("div"); m.className = "meta"; m.textContent = meta; div.appendChild(m); }
  $("#chat-log").appendChild(div);
  $("#chat-log").scrollTop = 1e9;
  return div;
}

async function sendChat() {
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  $("#chat-stats").textContent = "";
  addMsg("user", text);
  chatHistory.push({ role: "user", content: text });

  const model = $("#chat-model").value || "auto";
  const body = {
    model, messages: chatHistory.slice(-24),
    temperature: parseFloat($("#chat-temp").value),
    max_tokens: parseInt($("#chat-max").value),
  };
  if ($("#chat-sys").value.trim())
    body.messages = [{ role: "system", content: $("#chat-sys").value.trim() }, ...body.messages];
  const stream = $("#chat-stream").checked;
  body.stream = stream;

  const el = addMsg("assistant", "…");
  $("#chat-send").disabled = true; $("#chat-stop").style.display = "";
  chatAbort = new AbortController();
  const t0 = performance.now();
  let out = "", ntok = 0;
  try {
    const r = await fetch("/v1/chat/completions", {
      method: "POST", signal: chatAbort.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    if (stream) {
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (; ;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx).trim(); buf = buf.slice(idx + 1);
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (data === "[DONE]") continue;
          try {
            const j = JSON.parse(data);
            const delta = j.choices?.[0]?.delta?.content;
            if (delta) { out += delta; ntok++; el.textContent = out; $("#chat-log").scrollTop = 1e9; }
          } catch (e) { /* partial line */ }
        }
      }
    } else {
      const j = await r.json();
      out = j.choices?.[0]?.message?.content || "(empty)";
      el.textContent = out;
    }
  } catch (e) {
    if (e.name === "AbortError") { el.textContent = out + "\n(stopped)"; }
    else { el.className = "msg error"; el.textContent = "error: " + e.message; }
  }
  const dt = (performance.now() - t0) / 1000;
  const tps = ntok > 0 && dt > 0 ? (ntok / dt).toFixed(1) : null;
  el.querySelector(".meta")?.remove();
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `${model} · ${dt.toFixed(1)}s${tps ? ` · ${tps} tok/s` : ""}`;
  el.appendChild(meta);
  chatHistory.push({ role: "assistant", content: out });
  $("#chat-send").disabled = false; $("#chat-stop").style.display = "none";
  $("#chat-stats").textContent = `${chatHistory.length} messages in context`;
}
$("#chat-send").onclick = sendChat;
$("#chat-stop").onclick = () => chatAbort?.abort();
$("#chat-input").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
$("#chat-clear").onclick = () => { chatHistory = []; $("#chat-log").innerHTML = ""; $("#chat-stats").textContent = ""; };
$("#chat-temp").oninput = e => $("#val-temp").textContent = e.target.value;
$("#chat-max").oninput = e => $("#val-max").textContent = e.target.value;

/* ---------- models ---------- */
let modelsPolled = 0;
async function refreshModels() {
  try {
    const [st, disk] = await Promise.all([api("/api/status"), api("/api/models/disk")]);
    $("#m-fast-pill").className = "pill " + (st.fast.up ? "up" : "down");
    $("#m-fast-pill").textContent = st.fast.up ? "UP" : "DOWN";
    const dp = $("#m-deep-pill");
    dp.className = "pill " + (st.deep.running ? (st.deep.loading ? "load" : "up") : "down");
    dp.textContent = st.deep.running ? (st.deep.loading ? "LOADING" : "UP") : "DOWN";
    $("#m-deep-model").textContent = (st.deep.model || "").split("/").pop();
    $("#m-deep-ctx").textContent = st.deep.ctx + " tokens";
    const nameByIp = Object.fromEntries((st.workers || []).map(x => [x.ip, x.name]));
    const poolIps = (st.pool || "").split(",").filter(Boolean).map(s => s.split(":")[0]);
    const wn = poolIps.map(ip => nameByIp[ip] || ip);
    $("#m-deep-workers").textContent = wn.length ? wn.join(" + ") : "none";
    $("#m-deep-tps").textContent = st.deep.last_tps ? ` · last ${st.deep.last_tps} t/s` : "";
    $("#m-ctx").value = st.desired.ctx;
    const cur = (st.deep.model || "").split("/").pop();
    const tb = $("#m-disk tbody");
    tb.innerHTML = (disk.models || []).map(m => {
      const isCurrent = m.name === cur && m.path === st.deep.model;
      return `<tr><td class="${isCurrent ? "ok" : ""}">${m.name}${isCurrent ? " ●" : ""}</td>
        <td>${m.gb} GB</td><td class="dim">${new Date(m.mtime * 1000).toLocaleString()}</td>
        <td><button class="btn ${isCurrent ? "" : "primary"}" data-path="${m.path}"
          ${isCurrent || !st.fast.up && false ? "disabled" : ""}>${isCurrent ? "loaded" : "Load"}</button></td></tr>`;
    }).join("");
    $$("#m-disk tbody button[data-path]").forEach(b => b.onclick = () => loadModel(b.dataset.path, b));
  } catch (e) { toast("models refresh failed: " + e.message); }
}
async function loadModel(path, btn) {
  const ctx = parseInt($("#m-ctx").value);
  btn.disabled = true; btn.textContent = "loading…";
  try {
    await api("/api/desired", { method: "POST", body: JSON.stringify({ model: path, ctx }) });
    $("#m-apply-state").textContent = `queued ${path.split("/").pop()} @ ${ctx} — watchdog applies within 60s`;
    toast("load queued — the watchdog is reloading the deep lane");
    waitApplied();
  } catch (e) { toast("failed: " + e.message); btn.disabled = false; btn.textContent = "Load"; }
}
async function waitApplied() {
  const t0 = Date.now();
  const iv = setInterval(async () => {
    try {
      const st = await api("/api/status");
      const want = (st.desired.model || "") + String(st.desired.ctx);
      const have = (st.deep.model || "") + String(st.deep.ctx);
      if (st.deep.running && want === have && !st.deep.loading) {
        clearInterval(iv);
        $("#m-apply-state").textContent = "applied ✓";
        $("#m-apply-state").className = "ok small";
        toast("deep lane reloaded");
        refreshModels();
        return;
      }
    } catch (e) { }
    if (Date.now() - t0 > 300000) { clearInterval(iv); $("#m-apply-state").textContent = "still applying… (check Workers → log)"; }
  }, 5000);
}
$("#m-apply").onclick = async () => {
  const st = await api("/api/status");
  await loadModel(st.desired.model, $("#m-apply"));
};

/* ---------- workers ---------- */
async function refreshWorkers() {
  try {
    const st = await api("/api/status");
    const wf = (st.workers_file || {}).workers || [];
    const defs = st.workers || [];
    const cards = [];
    cards.push(`<div class="card"><div class="card-title">henry <span class="pill up">coordinator</span></div>
      <div class="metric">RAM ${st.sys.ram_avail_gb} / ${st.sys.ram_total_gb} GB free</div>
      <div>temp ${st.sys.temp}°C · load ${(st.sys.load || []).join(" ")}</div>
      <div class="dim small">fast lane ${st.fast.up ? "✓" : "✗"} · deep lane ${st.deep.running ? "✓" : "✗"}</div>
      <div class="small mono">${st.fast.url}</div></div>`);
    for (const w of wf) {
      const pct = w.mem_total ? Math.round(100 - (w.mem_avail / w.mem_total) * 100) : 0;
      cards.push(`<div class="card"><div class="card-title">${w.name} <span class="pill ${w.ssh ? "up" : "down"}">${w.ssh ? "online" : "offline"}</span></div>
        <div class="small dim">${w.ip}</div>
        <div class="metric">RAM ${pct}% used</div>
        <div class="bar"><div style="width:${pct}%"></div></div>
        <div>rpc ${w.rpc ? `<span class="ok">listening :50052</span>` : `<span class="bad">down</span>`}${w.temp ? ` · ${w.temp}°C` : ""}</div>
        <div class="row gap" style="margin-top:8px">
          <button class="btn" data-w="${w.ip}">Restart rpc</button>
        </div></div>`);
    }
    $("#w-cards").innerHTML = cards.join("");
    $$("#w-cards button[data-w]").forEach(b => b.onclick = async () => {
      b.disabled = true; b.textContent = "restarting…";
      try {
        await api("/api/actions/worker_restart", { method: "POST", body: JSON.stringify({ ip: b.dataset.w }) });
        toast("worker rpc restarted");
      } catch (e) { toast("failed: " + e.message); }
      setTimeout(refreshWorkers, 4000);
    });
    try {
      const log = await (await fetch("/deeplog")).text();
      $("#w-deeplog").textContent = log.slice(-3000) || "(empty)";
    } catch (e) { $("#w-deeplog").textContent = "(log unavailable)"; }
  } catch (e) { toast("workers refresh failed: " + e.message); }
}
$("#w-restart-coord").onclick = async () => {
  if (!confirm("Restart the mesh-30b coordinator? Active requests will fail.")) return;
  try { await api("/api/actions/coordinator_restart", { method: "POST", body: "{}" }); toast("coordinator restarting"); }
  catch (e) { toast("failed: " + e.message); }
};

/* ---------- settings ---------- */
async function refreshSettings() {
  const c = await api("/api/config");
  $("#s-hints").checked = !!c.route_hints;
  $("#s-think").checked = !!c.fast_thinking;
  $("#s-refresh").value = c.refresh_s || 5;
  $("#s-ref-val").textContent = c.refresh_s || 5;
}
async function saveSetting(patch) {
  await api("/api/config", { method: "POST", body: JSON.stringify(patch) });
  $("#s-saved").textContent = "saved ✓";
  setTimeout(() => $("#s-saved").textContent = "", 1500);
  startPolling();
}
$("#s-hints").onchange = e => saveSetting({ route_hints: e.target.checked });
$("#s-think").onchange = e => saveSetting({ fast_thinking: e.target.checked });
$("#s-refresh").oninput = e => $("#s-ref-val").textContent = e.target.value;
$("#s-refresh").onchange = e => saveSetting({ refresh_s: parseInt(e.target.value) });

/* ---------- boot ---------- */
(async () => {
  try { CFG = await api("/api/config"); } catch (e) { }
  pollStatus();
  refreshModelSelect({ fast: { up: true }, deep: { running: false } });
  startPolling();
  // enrich model select once status flows in
  const iv = setInterval(async () => {
    try { refreshModelSelect(await api("/api/status")); } catch (e) { }
  }, 15000);
})();
