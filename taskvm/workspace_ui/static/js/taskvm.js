"use strict";
let currentSid = null, sseSource = null, snapshot = null;

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
  if (sseSource) sseSource.close();
  connectSSE();
  loadSnapshot();
}

async function loadSnapshot() {
  try { snapshot = await api("/snapshot"); renderAll(); }
  catch (e) { console.error("snapshot error:", e); }
}

function connectSSE() {
  sseSource = new EventSource(`/api/sessions/${currentSid}/sse`);
  sseSource.onmessage = (ev) => {
    const env = JSON.parse(ev.data);
    if (env.sse_type === "snapshot") { snapshot = env.detail; renderAll(); return; }
    const t = env.sse_type || "";
    if (t.startsWith("governance") || t.startsWith("state.") ||
        t.startsWith("plan.") || t.startsWith("action.") ||
        t.startsWith("verification.") || t.startsWith("node.") ||
        t.startsWith("checkpoint.") || t.startsWith("conflict.") ||
        t.startsWith("compensation.") || t.startsWith("loop.")) {
      loadSnapshot();
    }
  };
  sseSource.onerror = () => {
    console.warn("SSE error, reconnecting in 3s…");
    setTimeout(() => { if (currentSid) connectSSE(); }, 3000);
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
      govCommand("local_patch", { updates: { [key]: input.value }, rationale: "UI edit" });
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
      html += `<img class="artifact-thumb" src="/api/sessions/${currentSid}/artifacts/${esc(card.latest_artifact_ref)}" alt="screenshot">`;
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
      govCommand("resolve_conflict", {
        conflict_id: btn.dataset.conflict,
        resolution: "keep_desired",
        detail: "UI resolve" });
    };
  });
}

async function govCommand(cmd, body) {
  try {
    await api(`/governance/${cmd}`, { method: "POST", body: JSON.stringify(body) });
  } catch (e) {
    alert(`Governance error: ${e.message}`);
  }
}

// ── wire up ───────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  loadSessions();
  document.getElementById("session-select").onchange = onSessionChange;
  document.getElementById("btn-start").onclick = () => govCommand("start", { rationale: "UI start" });
  document.getElementById("btn-pause").onclick = () => govCommand("pause", { rationale: "UI pause" });
  document.getElementById("btn-resume").onclick = () => govCommand("resume", { rationale: "UI resume" });
  document.getElementById("btn-stop").onclick = () => govCommand("stop", { rationale: "UI stop" });
  document.getElementById("btn-checkpoint").onclick = () => {
    const label = prompt("Checkpoint label:");
    if (label) govCommand("checkpoint", { label });
  };
  // goal patch modal (recompose): open / apply / cancel
  document.getElementById("btn-recompose").onclick = () => {
    document.getElementById("goal-modal").classList.remove("hidden");
  };
  document.getElementById("gp-cancel").onclick = () => {
    document.getElementById("goal-modal").classList.add("hidden");
  };
  document.getElementById("gp-submit").onclick = () => {
    const goal = document.getElementById("gp-goal").value.trim();
    if (!goal) return;
    const constraints = document.getElementById("gp-constraints").value
      .split("\n").map(s => s.trim()).filter(Boolean);
    const rationale = document.getElementById("gp-rationale").value.trim()
      || "UI goal patch";
    govCommand("goal_patch", { goal, constraints, rationale });
    document.getElementById("goal-modal").classList.add("hidden");
  };
});
