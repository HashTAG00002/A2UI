"use strict";
/* TaskVM APP frontend — responsiveness stopgap layer (A9.1).
 *
 * Latency doctrine (A9.0 audit, eval_results/latency_audit_20260819/):
 * the remote user's round-trip is 4.5-6s per small request, so
 *   * every click shows a <100ms local pending state (optimistic ack,
 *     rollback + toast on failure — never a silent no-op);
 *   * screenshots move as ≤240px thumbnails with a content-hash dedup
 *     param (unchanged screen ⇒ zero-body 200 ⇒ zero transfer);
 *   * SSE frames are tiny server-side (composition strips artifact data
 *     URLs); the client still merges SSE bursts (150ms debounce) so a
 *     tick's event train costs ONE snapshot fetch, not N;
 *   * a broken SSE stream never blanks the UI: the last snapshot stays
 *     rendered, a "syncing" badge shows, snapshot polling backs the
 *     stream up, and reconnect uses exponential backoff.
 * Governance doctrine (workplan §2): bootstrap ends in Ready — the user
 * presses Start explicitly; this file NEVER calls /governance/start on
 * its own. */
let currentSid = null, sseSource = null, snapshot = null;
let appStatus = null;          // GET /api/app/status payload
let pollingGoal = null;        // goal_id while bootstrapping
let appStatusTimer = null, bootTimer = null, bootTicker = null;
let resyncTimer = null;        // snapshot fallback while SSE is down
let sseBackoff = 3000;

// ── live-shot state (dedup + thumbnail + object-URL hygiene) ──────────────
let shotHash = "";             // last X-Shot-Hash the client holds
let shotObjUrl = null;         // current object URL (revoked on replace)
let shotFails = 0;             // consecutive fetch failures

// ── tiny helpers ───────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const res = await fetch(`/api/sessions/${currentSid}${path}`, {
    headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = String(s ?? "");
  return d.innerHTML;
}

function mark(name) {          // performance marks (A9.0 instrumentation)
  try { performance.mark(`taskvm:${name}`); } catch { /* non-fatal */ }
}

let toastTimer = null;
function toast(msg, kind = "error") {
  const el = document.getElementById("toast");
  document.getElementById("toast-msg").textContent = msg;
  el.className = `toast toast-${kind}`;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 8000);
}

// every governance/local action gets a <100ms visible pending state:
// the button locks + labels itself instantly, the HTTP result confirms,
// a failure rolls the button back and surfaces the server's own error.
function withPending(btn, label, fn) {
  if (!btn || btn.classList.contains("pending")) return;
  const orig = btn.textContent;
  btn.classList.add("pending");
  btn.disabled = true;
  btn.textContent = `${label}…`;
  mark("click");
  const t0 = performance.now();
  Promise.resolve(fn()).then(() => {
    mark("ack");
    try {
      performance.measure("taskvm:click-to-ack",
        "taskvm:click", "taskvm:ack");
    } catch { /* marks may be missing on fast paths */ }
    btn.classList.remove("pending");
    btn.disabled = false;
    btn.textContent = orig;
  }).catch(e => {
    btn.classList.remove("pending");
    btn.disabled = false;
    btn.textContent = orig;
    toast(`操作失败（${Math.round(performance.now() - t0)}ms）：${e.message}`);
    throw e;                   // let callers observe too
  });
}

// ── APP shell (empty-state hero / goal bootstrap) ─────────────────────────

async function loadAppStatus() {
  try {
    const res = await fetch("/api/app/status");
    appStatus = res.ok ? await res.json() : null;
  } catch { appStatus = null; }
  return appStatus;
}

function renderHero() {
  const hero = document.getElementById("hero");
  if (!appStatus) { hero.classList.add("hidden"); return; }
  const w = appStatus.world || {};
  const surfaces = (w.surfaces || []).map(s => s.name || s.id).join(" / ");
  document.getElementById("hero-world").textContent =
    `已连接任务世界${surfaces ? `（${surfaces}）` : ""} · 模型 ${w.model || "?"}` +
    (w.offline ? " · 离线 CUA" : "") +
    (!w.openai_configured && !w.offline ? " · ⚠ OPENAI_API_KEY 未配置" : "");
  document.getElementById("hero-hint").innerHTML =
    `任务将驱动 <a href="${esc(w.sim_url)}" target="_blank">手机模拟器 ↗</a> 里的真实 GUI 完成 · ` +
    `提交后先编译（可看到分阶段进度），编译完成进入 Ready，由你决定何时 Start`;
  hero.classList.remove("hidden");
}

function showView(which) {   // "hero" | "boot" | "dashboard"
  document.getElementById("hero").classList.toggle("hidden", which !== "hero");
  document.getElementById("boot-panel").classList.toggle("hidden", which !== "boot");
  document.getElementById("main-grid").classList.toggle("hidden", which !== "dashboard");
  document.getElementById("governance-bar").classList.toggle(
    "hidden", which !== "dashboard" || !snapshot);
}

// ── progressive task surface (stopgap): the goal card renders the instant
//    the goal is submitted — generic skeleton, no fabricated content ──────
function renderBootSkeleton(goalText) {
  document.getElementById("boot-goal-text").textContent = goalText;
  document.getElementById("boot-node-label").textContent = "正在观察屏幕…";
  document.getElementById("boot-node").classList.remove("failed");
  document.getElementById("boot-error").classList.add("hidden");
  document.getElementById("boot-error").textContent = "";
  document.getElementById("boot-back").classList.add("hidden");
  for (const li of document.querySelectorAll("#boot-stages li")) {
    li.className = "";
    li.querySelector(".st-time").textContent = "";
  }
  document.getElementById("boot-timer").textContent = "0.0s";
}

function startBootTicker(g) {
  if (bootTicker) clearInterval(bootTicker);
  bootTicker = setInterval(() => {
    const el = document.getElementById("boot-timer");
    if (!el || el.closest(".hidden")) return;
    el.textContent = `${(Date.now() / 1000 - g.created_at).toFixed(1)}s`;
  }, 200);
}

// stage derivation is honest and signal-driven: the ledger's per-role
// call counts are the ONLY inputs (server stamps transitions), the
// active stage gets a live timer, finished stages get ✓ + duration.
function renderBootStages(g) {
  const counts = g.ledger_counts || {};
  const stages = g.stages || {};
  const now = Date.now() / 1000;
  const node = document.getElementById("boot-node");
  const nodeLabel = document.getElementById("boot-node-label");
  const items = {
    compile: document.querySelector('#boot-stages li[data-stage="compile"]'),
    plan: document.querySelector('#boot-stages li[data-stage="plan"]'),
    ready: document.querySelector('#boot-stages li[data-stage="ready"]'),
  };
  const t0 = g.created_at;
  const tCompile = stages.compiler_done_at;
  const tPlan = stages.architect_done_at;
  const tReady = g.status === "ready" ? (g.finished_at || now) : null;
  const tFailed = g.status === "failed" ? (g.finished_at || now) : null;

  const setDone = (li, dur) => {
    li.className = "done";
    li.querySelector(".st-time").textContent = `✓ ${dur.toFixed(1)}s`;
  };
  const setActive = (li) => {
    li.className = "active";
    li.querySelector(".st-time").textContent =
      `${(now - (li._startedAt ??= now)).toFixed(1)}s`;
  };

  if (counts.state_compiler >= 1 && tCompile) {
    setDone(items.compile, tCompile - t0);
  } else {
    items.compile._startedAt = t0;
    setActive(items.compile);
  }
  if (counts.task_architect >= 1 && tPlan) {
    setDone(items.plan, tPlan - (tCompile || t0));
  } else if (counts.state_compiler >= 1) {
    items.plan._startedAt = tCompile || t0;
    setActive(items.plan);
  }
  if (tReady) {
    setDone(items.ready, tReady - (tPlan || tCompile || t0));
  } else if (counts.task_architect >= 1) {
    items.ready._startedAt = tPlan || tCompile || t0;
    setActive(items.ready);
  }

  if (g.status === "failed") {
    node.classList.add("failed");
    nodeLabel.textContent = "编排失败（诚实报错，无兜底计划）";
  } else if (tReady) {
    nodeLabel.textContent = "编译完成，等待开始";
  } else if (counts.task_architect >= 1) {
    nodeLabel.textContent = "正在校验计划…";
  } else if (counts.state_compiler >= 1) {
    nodeLabel.textContent = "正在生成执行计划…";
  } else {
    nodeLabel.textContent = "正在编译任务世界…";
  }
}

async function submitGoal(goal) {
  goal = (goal || "").trim();
  if (!goal) return;
  mark("goal-submit");
  // the goal card renders IMMEDIATELY (progressive task surface, T0)
  renderBootSkeleton(goal);
  showView("boot");
  const sendBtn = document.getElementById("hero-send");
  sendBtn.disabled = true;
  sendBtn.textContent = "提交中…";
  let data = {};
  try {
    const res = await fetch("/api/app/goals", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal })
    });
    data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  } catch (e) {
    sendBtn.disabled = false;
    sendBtn.textContent = "发送 ⏎";
    const errEl = document.getElementById("boot-error");
    errEl.textContent = `提交失败：${e.message}`;
    errEl.classList.remove("hidden");
    document.getElementById("boot-back").classList.remove("hidden");
    return;
  }
  sendBtn.disabled = false;
  sendBtn.textContent = "发送 ⏎";
  pollingGoal = data.goal.goal_id;
  if (bootTimer) clearInterval(bootTimer);
  bootTimer = setInterval(pollGoal, 1500);
  startBootTicker(data.goal);
  refreshLiveShot();
}

async function pollGoal() {
  if (!pollingGoal || document.hidden) return;
  let data;
  try {
    const res = await fetch(`/api/app/goals/${pollingGoal}`);
    data = await res.json();
  } catch { return; }          // transient network error — next tick retries
  const g = data.goal;
  if (!g) return;
  renderBootStages(g);
  if (g.status === "bootstrapping") return;
  clearInterval(bootTimer); bootTimer = null;
  if (bootTicker) { clearInterval(bootTicker); bootTicker = null; }
  if (g.status === "failed") {
    document.getElementById("boot-error").textContent = g.error;
    document.getElementById("boot-error").classList.remove("hidden");
    document.getElementById("boot-back").classList.remove("hidden");
    pollingGoal = null;
    toast("任务编排失败——已显示具体原因（无兜底计划）", "error");
    return;
  }
  // ready → dashboard. NO autostart: governance over autonomy — the user
  // reviews the compiled task surface and presses Start themselves.
  pollingGoal = null;
  await loadSessions();
  const sel = document.getElementById("session-select");
  sel.value = g.sid;
  onSessionChange();
  showView("dashboard");
}

// ── live phone: thumbnail + content-hash dedup + retry + placeholders ────
function refreshLiveShot() {
  if (document.hidden) return;
  let url = "/api/app/screenshot?thumb=1&w=240";
  if (shotHash) url += `&h=${encodeURIComponent(shotHash)}`;
  fetch(url).then(r => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const h = r.headers.get("X-Shot-Hash");
    if (h) shotHash = h;
    if (r.headers.get("X-Shot-Same") === "1") return null; // screen unchanged
    return r.blob();
  }).then(blob => {
    if (!blob || !blob.size) {
      document.getElementById("phone-shot-stale").classList.add("hidden");
      return;                  // dedup hit — nothing to swap
    }
    const old = shotObjUrl;
    shotObjUrl = URL.createObjectURL(blob);
    const img = document.getElementById("phone-shot");
    img.src = shotObjUrl;
    img.classList.remove("hidden");
    document.getElementById("phone-shot-placeholder").classList.add("hidden");
    const full = document.getElementById("phone-shot-full");
    full.href = "/api/app/screenshot";   // full-size, click to open
    full.classList.remove("hidden");
    if (old) URL.revokeObjectURL(old);
    shotFails = 0;
    document.getElementById("phone-shot-stale").classList.add("hidden");
  }).catch(() => {
    shotFails++;
    if (shotFails >= 2)        // keep the old frame; flag it honestly
      document.getElementById("phone-shot-stale").classList.remove("hidden");
  });
  if (appStatus) {
    const link = document.getElementById("sim-link");
    if (appStatus.world && appStatus.world.sim_url) link.href = appStatus.world.sim_url;
  }
}

// ── sessions / SSE / dashboard (projection plane) ──────────────────────────

async function loadSessions() {
  const data = await fetch("/api/sessions").then(r => r.json());
  const sel = document.getElementById("session-select");
  sel.innerHTML = '<option value="">— select session —</option>';
  for (const sid of data.sessions || []) {
    const opt = document.createElement("option");
    opt.value = sid; opt.textContent = sid;
    sel.appendChild(opt);
  }
}

function onSessionChange() {
  const sel = document.getElementById("session-select");
  currentSid = sel.value;
  if (!currentSid) return;
  connectSSE();
  loadSnapshot();
}

// SSE bursts merge: a driver tick lands as a train of frames; one debounced
// snapshot fetch per 150ms window answers all of them.
let snapTimer = null;
function scheduleSnapshot() {
  if (snapTimer) return;
  snapTimer = setTimeout(() => { snapTimer = null; loadSnapshot(); }, 150);
}

async function loadSnapshot() {
  try { snapshot = await api("/snapshot"); renderAll(); }
  catch (e) { console.error("snapshot error:", e); }
}

function closeSSE() {
  if (sseSource) { sseSource.close(); sseSource = null; }
}

function showSyncBadge(on) {
  document.getElementById("sync-badge").classList.toggle("hidden", !on);
}

function connectSSE() {
  closeSSE();
  if (!currentSid) return;
  sseSource = new EventSource(`/api/sessions/${currentSid}/sse`);
  sseSource.onopen = () => {
    sseBackoff = 3000;         // healthy again — reset the backoff
    showSyncBadge(false);
    loadSnapshot();            // definitive state on (re)connect
    if (resyncTimer) { clearInterval(resyncTimer); resyncTimer = null; }
  };
  sseSource.onmessage = (ev) => {
    const env = JSON.parse(ev.data);
    if (env.sse_type === "snapshot") { snapshot = env.detail; renderAll(); return; }
    const t = env.sse_type || "";
    if (t.startsWith("governance") || t.startsWith("state.") ||
        t.startsWith("plan.") || t.startsWith("action.") ||
        t.startsWith("verification.") || t.startsWith("node.") ||
        t.startsWith("checkpoint.") || t.startsWith("conflict.") ||
        t.startsWith("compensation.") || t.startsWith("loop.")) {
      scheduleSnapshot();
    }
  };
  sseSource.onerror = async () => {
    // never blank the UI: the last snapshot stays rendered and a snapshot
    // poll keeps the dashboard alive behind the badge while reconnecting.
    showSyncBadge(true);
    if (!resyncTimer) resyncTimer = setInterval(loadSnapshot, 5000);
    try {
      const data = await fetch("/api/sessions").then(r => r.json());
      if (!currentSid || !(data.sessions || []).includes(currentSid)) {
        closeSSE();
        return;                // replaced by a new goal — stop reconnecting
      }
    } catch { /* fall through to reconnect */ }
    console.warn(`SSE error, reconnecting in ${sseBackoff / 1000}s…`);
    setTimeout(() => { if (currentSid) connectSSE(); }, sseBackoff);
    sseBackoff = Math.min(sseBackoff * 2, 15000);
  };
}

function renderAll() {
  if (!snapshot) return;
  renderGovernance();
  renderVariables();
  renderWorkflow();
  renderSurfaces();
  renderCheckpoints();
  renderConflicts();
}

function renderGovernance() {
  const g = snapshot.governance || {};
  document.getElementById("governance-bar").classList.remove("hidden");
  document.getElementById("goal-text").textContent = g.goal || "";
  const badge = document.getElementById("autonomy-badge");
  badge.textContent = g.autonomy || "idle";
  badge.className = `badge badge-${autonomyClass(g.autonomy)}`;
  document.getElementById("epoch-badge").textContent = `epoch ${g.epoch || 0}`;
  const mc = document.getElementById("model-calls");
  if (g.model_calls != null) {
    mc.textContent = `model: ${g.model_calls}`;
    mc.className = g.model_calls === 0 ? "badge badge-ok" : "badge badge-warn";
  } else { mc.textContent = "model: —"; }
  const isPaused = g.autonomy === "paused" || g.autonomy === "stopped";
  document.getElementById("btn-pause").classList.toggle("hidden", isPaused);
  document.getElementById("btn-resume").classList.toggle("hidden", !isPaused);
  document.getElementById("btn-start").classList.toggle("hidden",
    g.autonomy === "running");
  // Ready banner: compiled but never started — the explicit-Start contract
  document.getElementById("ready-banner").classList.toggle(
    "hidden", g.autonomy !== "idle");
}

function autonomyClass(a) {
  if (a === "running") return "ok";
  if (a === "paused" || a === "replanning") return "warn";
  if (a === "stopped" || a === "blocked") return "danger";
  return "idle";
}

function renderVariables() {
  const list = document.getElementById("variables-list");
  const vars = snapshot.variables || [];
  if (!vars.length) { list.innerHTML = '<p class="placeholder">No variables.</p>'; return; }
  list.innerHTML = "";
  for (const v of vars) {
    const row = document.createElement("div");
    row.className = `var-row${v.diverged ? " diverged" : ""}`;
    let html = `<div class="var-label">${esc(v.label || v.key)}</div>`;
    html += `<div class="var-observed">observed: ${esc(JSON.stringify(v.observed))}</div>`;
    html += `<div class="var-observed">desired: ${esc(JSON.stringify(v.desired))}</div>`;
    if (v.editable) {
      html += `<div class="var-input">`;
      html += `<input type="text" data-key="${esc(v.key)}" value="${esc(String(v.desired ?? ""))}">`;
      html += `<button class="btn btn-ok" data-key="${esc(v.key)}">Set</button>`;
      html += `</div>`;
    } else {
      html += `<div class="var-observed">(readonly)</div>`;
    }
    row.innerHTML = html;
    list.appendChild(row);
  }
  list.querySelectorAll("button[data-key]").forEach(btn => {
    btn.onclick = () => {
      const key = btn.dataset.key;
      const input = list.querySelector(`input[data-key="${key}"]`);
      withPending(btn, "应用", () =>
        govCommand("local_patch",
                   { updates: { [key]: input.value }, rationale: "UI edit" }));
    };
  });
}

function renderWorkflow() {
  const map = document.getElementById("workflow-map");
  const wf = snapshot.workflow || {};
  if (!wf.has_plan) { map.innerHTML = '<p class="placeholder">No plan yet.</p>'; return; }
  map.innerHTML = "";
  for (const node of wf.nodes || []) {
    const row = document.createElement("div");
    row.className = `wf-row status-${node.status_label || node.status}`;
    row.style.marginLeft = `${(node.depth || 0) * 1.5}em`;
    let html = `<span class="wf-kind">${esc(node.kind_label)}</span>`;
    html += `<span class="wf-label">${esc(node.label)}</span>`;
    if (node.progress) html += `<span class="wf-progress">${node.progress.committed}/${node.progress.total}</span>`;
    if (node.loop) html += `<span class="wf-loop">iter ${node.loop.iteration ?? "?"}/${node.loop.max_iterations ?? "?"}</span>`;
    if (node.action && node.action.irreversible) html += `<span class="wf-loop">irreversible</span>`;
    html += `<span class="wf-status">${esc(node.status_label)}</span>`;
    if (node.rollback_boundary) html += `<span class="wf-progress">checkpoint</span>`;
    row.innerHTML = html;
    map.appendChild(row);
  }
  if (wf.progress) {
    const s = document.createElement("div");
    s.className = "wf-progress";
    s.textContent = `${wf.progress.committed}/${wf.progress.total} verified`;
    map.appendChild(s);
  }
}

function renderSurfaces() {
  const container = document.getElementById("surface-cards");
  const cards = snapshot.surfaces || [];
  if (!cards.length) { container.innerHTML = '<p class="placeholder">No surfaces.</p>'; return; }
  container.innerHTML = "";
  for (const card of cards) {
    const div = document.createElement("div");
    div.className = `surface-card status-${card.status || "unknown"}`;
    let html = `<div class="surface-name">${esc(card.display_name)}</div>`;
    html += `<div class="surface-status">${esc(card.status || "unknown")}`;
    if (card.last_observed_at) html += ` · epoch ${card.last_observed_at}`;
    html += `</div>`;
    if (card.latest_artifact_ref) {
      // artifact tokens are opaque, immutable and served with a thumbnail
      // pipeline — the full-size original stays one click away
      const ref = encodeURIComponent(card.latest_artifact_ref);
      html += `<img class="artifact-thumb" loading="lazy" alt="screenshot"` +
        ` src="/api/app/screenshot?ref=${ref}&thumb=1&w=240"` +
        ` onclick="window.open('/api/app/screenshot?ref=${ref}', '_blank')">`;
    }
    div.innerHTML = html;
    container.appendChild(div);
  }
}

function renderCheckpoints() {
  const container = document.getElementById("checkpoint-timeline");
  const ckpts = snapshot.checkpoints || [];
  if (!ckpts.length) { container.innerHTML = '<p class="placeholder">No checkpoints.</p>'; return; }
  container.innerHTML = "";
  for (const ck of ckpts) {
    const row = document.createElement("div");
    row.className = "ckpt-row";
    let html = `<span class="ckpt-label">${esc(ck.label)}</span>`;
    html += `<span class="wf-progress">epoch ${ck.epoch} · ${ck.committed_nodes} nodes</span>`;
    if (ck.rollback_available) {
      html += `<span class="ckpt-rollback" data-cid="${esc(ck.checkpoint_id)}">rollback</span>`;
    }
    row.innerHTML = html;
    container.appendChild(row);
  }
  container.querySelectorAll(".ckpt-rollback").forEach(el => {
    el.onclick = () => {
      const cid = el.dataset.cid;
      if (confirm(`Rollback to checkpoint ${cid}?`)) {
        govCommand("rollback", { target_checkpoint_id: cid, rationale: "UI rollback" });
      }
    };
  });
}

function renderConflicts() {
  const container = document.getElementById("conflicts-list");
  const conflicts = snapshot.conflicts || [];
  if (!conflicts.length) { container.innerHTML = '<p class="placeholder">No conflicts.</p>'; return; }
  container.innerHTML = "";
  for (const c of conflicts) {
    const row = document.createElement("div");
    row.className = "conflict-row";
    let html = `<div>${esc(c.description)}</div><div class="surface-status">keys: ${esc((c.semantic_keys || []).join(", "))}</div>`;
    if (c.conflict_id) {
      html += `<button class="btn btn-default" data-conflict="${esc(c.conflict_id)}">Resolve</button>`;
    }
    row.innerHTML = html;
    container.appendChild(row);
  }
  container.querySelectorAll("button[data-conflict]").forEach(btn => {
    btn.onclick = () => {
      const body = {
        conflict_id: btn.dataset.conflict,
        resolution: "keep_desired",
        detail: "UI resolve"
      };
      withPending(btn, "解决", () => govCommand("resolve_conflict", body));
    };
  });
}

async function govCommand(cmd, body) {
  await api(`/governance/${cmd}`, { method: "POST", body: JSON.stringify(body) });
  loadSnapshot();              // confirm with the authoritative state
}

// ── wire up ───────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  await loadAppStatus();
  renderHero();
  await loadSessions();

  // empty APP (no sessions) → hero; otherwise resume the dashboard
  const sids = (await (await fetch("/api/sessions")).json()).sessions || [];
  if (sids.length) {
    const sel = document.getElementById("session-select");
    sel.value = sids[0];
    onSessionChange();
    showView("dashboard");
  } else {
    showView("hero");
  }

  // periodic world-status + live shot (cheap reads; skipped while hidden)
  appStatusTimer = setInterval(async () => {
    if (document.hidden) return;
    await loadAppStatus();
    refreshLiveShot();
  }, 2500);

  document.getElementById("toast-close").onclick = () =>
    document.getElementById("toast").classList.add("hidden");

  document.getElementById("session-select").onchange = onSessionChange;
  document.getElementById("btn-start").onclick = (ev) =>
    withPending(ev.currentTarget, "启动",
      () => govCommand("start", { rationale: "UI start" }));
  document.getElementById("btn-pause").onclick = (ev) =>
    withPending(ev.currentTarget, "暂停",
      () => govCommand("pause", { rationale: "UI pause" }));
  document.getElementById("btn-resume").onclick = (ev) =>
    withPending(ev.currentTarget, "恢复",
      () => govCommand("resume", { rationale: "UI resume" }));
  document.getElementById("btn-stop").onclick = (ev) =>
    withPending(ev.currentTarget, "停止",
      () => govCommand("stop", { rationale: "UI stop" }));
  document.getElementById("btn-checkpoint").onclick = (ev) => {
    const label = prompt("Checkpoint label:");
    if (!label) return;
    withPending(ev.currentTarget, "存档", () =>
      govCommand("checkpoint", { label }));
  };
  // goal patch modal (recompose): open / apply / cancel
  document.getElementById("btn-recompose").onclick = () => {
    document.getElementById("goal-modal").classList.remove("hidden");
  };
  document.getElementById("gp-cancel").onclick = () => {
    document.getElementById("goal-modal").classList.add("hidden");
  };
  document.getElementById("gp-submit").onclick = (ev) => {
    const goal = document.getElementById("gp-goal").value.trim();
    if (!goal) return;
    const constraints = document.getElementById("gp-constraints").value
      .split("\n").map(s => s.trim()).filter(Boolean);
    const rationale = document.getElementById("gp-rationale").value.trim()
      || "UI goal patch";
    document.getElementById("goal-modal").classList.add("hidden");
    withPending(ev.currentTarget, "应用",
      () => govCommand("goal_patch", { goal, constraints, rationale }));
  };

  // APP shell: hero submit (goal-only — app/surface is a runtime capability)
  document.getElementById("hero-send").onclick = () =>
    submitGoal(document.getElementById("hero-goal").value);
  document.getElementById("hero-goal").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) {
      submitGoal(document.getElementById("hero-goal").value);
    }
  });
  document.getElementById("btn-newtask").onclick = () => {
    closeSSE();
    pollingGoal = null;
    if (bootTimer) { clearInterval(bootTimer); bootTimer = null; }
    if (bootTicker) { clearInterval(bootTicker); bootTicker = null; }
    if (resyncTimer) { clearInterval(resyncTimer); resyncTimer = null; }
    showSyncBadge(false);
    document.getElementById("hero-goal").value = "";
    renderHero();
    showView("hero");
  };
  document.getElementById("boot-back").onclick = () => {
    pollingGoal = null;
    if (bootTimer) { clearInterval(bootTimer); bootTimer = null; }
    if (bootTicker) { clearInterval(bootTicker); bootTicker = null; }
    showView("hero");
  };
});
