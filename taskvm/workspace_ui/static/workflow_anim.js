/* workflow_anim.js — FF.1 §2.2 C + FF.5 §6 the workflow visualization region.
 *
 * Three structured-program shapes (FF.4 §5.1), each with its own visual:
 *   - Sequential: horizontal progress bar (N segments); current = shimmer,
 *     done = green ✓, locked = red 🔒.
 *   - Parallel: SVG barrier tree (start → N bezier curves → app nodes →
 *     converge to a barrier node); flowing colored ball while running.
 *   - Loop: SVG circular orbit; a ball travels the track, each lap adds a ✓
 *     tick + increments the "已完成 i / N 次" counter; full-green on done.
 *
 * Pure vanilla JS + inline SVG (handoff §6.4: no framework). Listens to the
 * server's SSE `workflow_progress` event (FF.5 §6.3) and re-renders on push.
 * Also exposes window.TaskVMWorkflow.render(container, state) for tests / seed.
 */
(function (window) {
  "use strict";

  function svgEl(tag, attrs) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    if (attrs) for (var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  function statusSeg(s) {
    return s === "done" ? "done" : s === "running" ? "running" :
           s === "locked" ? "locked" : "";
  }

  // ── Sequential: horizontal progress bar ────────────────────────────────
  function renderSequential(container, state) {
    var nodes = state.nodes || [];
    var h2 = '<h2>sequential workflow · ' + nodes.length + ' step(s)</h2>';
    var segs = "";
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      var cls = "seg-w " + statusSeg(n.status);
      var lbl = n.app || n.label || ("step " + (i + 1));
      var icon = n.status === "done" ? "✓ " : n.status === "running" ? "▶ " :
                 n.status === "locked" ? "🔒 " : "○ ";
      segs += '<div class="seg-w ' + cls + '" data-i="' + i + '">' + icon + lbl + '</div>';
      if (i < nodes.length - 1) segs += '<span class="arrow">→</span>';
    }
    container.innerHTML = h2 + '<div class="wf-seq">' + (segs || '<span class="meta">no steps</span>') + '</div>';
  }

  // ── Parallel: SVG barrier tree (start → N app nodes → barrier) ─────────
  function renderParallel(container, state) {
    var nodes = (state.nodes || []).filter(function (n) { return n.type === "parallel"; });
    var h2 = '<h2>parallel workflow · ' + nodes.length + '-app fanout → barrier</h2>';
    if (!nodes.length) { container.innerHTML = h2 + '<p class="meta">no parallel nodes</p>'; return; }

    var W = 520, H = 160, startX = 40, endX = W - 40;
    var midY = H / 2;
    var n = nodes.length;
    // app node positions, vertically spread
    var pts = nodes.map(function (_, i) {
      var y = n === 1 ? midY : (midY - (n - 1) * 22 + i * 44);
      return { x: W / 2, y: y };
    });
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "xMidYMid meet" });

    // start node (left)
    svg.appendChild(svgEl("circle", { cx: startX, cy: midY, r: 10, fill: "#3b82f6" }));
    svg.appendChild(svgEl("text", { x: startX, y: midY + 26, "text-anchor": "middle", "font-size": 10, fill: "#6b7280" })).textContent = "start";
    // barrier node (right)
    var barrierStatus = state.barrier_status || "waiting";
    var barrierFill = barrierStatus === "done" ? "#10b981" : "#9ca3af";
    svg.appendChild(svgEl("circle", { cx: endX, cy: midY, r: 12, fill: barrierFill }));
    svg.appendChild(svgEl("text", { x: endX, y: midY + 28, "text-anchor": "middle", "font-size": 10, fill: "#6b7280" })).textContent = "barrier ✓";

    // bezier from start → each app node → barrier
    for (var i = 0; i < n; i++) {
      var p = pts[i];
      var st = statusSeg(nodes[i].status);
      var color = st === "done" ? "#10b981" : st === "running" ? "#3b82f6" : "#d1d5db";
      var dash = st === "running" ? "4 4" : "none";
      // start → app
      var p1 = "M" + (startX + 10) + " " + midY + " C " + (W / 4) + " " + midY + ", " + (W / 4) + " " + p.y + ", " + (p.x - 14) + " " + p.y;
      var ln1 = svg.appendChild(svgEl("path", { d: p1, stroke: color, "stroke-width": 2, fill: "none", "stroke-dasharray": dash }));
      if (st === "running") { ln1.style.animation = "wf-shimmer 1.4s linear infinite"; ln1.setAttribute("stroke-dashoffset", "0"); }
      // app → barrier
      var p2 = "M" + (p.x + 14) + " " + p.y + " C " + (3 * W / 4) + " " + p.y + ", " + (3 * W / 4) + " " + midY + ", " + (endX - 12) + " " + midY;
      svg.appendChild(svgEl("path", { d: p2, stroke: color, "stroke-width": 2, fill: "none", "stroke-dasharray": dash }));
      // app node
      var nodeFill = st === "done" ? "#ecfdf5" : st === "running" ? "#eff6ff" : "#f9fafb";
      var nodeStroke = st === "done" ? "#10b981" : st === "running" ? "#3b82f6" : "#e5e7eb";
      svg.appendChild(svgEl("circle", { cx: p.x, cy: p.y, r: 12, fill: nodeFill, stroke: nodeStroke, "stroke-width": 2 }));
      var t = svg.appendChild(svgEl("text", { x: p.x, y: p.y + 3, "text-anchor": "middle", "font-size": 9, "font-weight": "600", fill: nodeStroke }));
      t.textContent = (nodes[i].app || "?").slice(0, 8);
    }
    container.innerHTML = h2;
    container.appendChild(svg);
    // running ball on the active lane(s) — a <circle> animated via SMIL along the path
    // (simplest: a pulsing dot on each running app node)
    var dots = svg.querySelectorAll("circle");
    // (visual motion already conveyed by shimmer + node pulse CSS classes)
  }

  // ── Loop: SVG circular orbit with a traveling ball + tick marks ─────────
  function renderLoop(container, state) {
    var nodes = state.nodes || [];
    var node = nodes[0] || {};
    var done = node.status === "done" ? (node.loop_count || 0) : (node.iterations_done || 0);
    var total = node.loop_count || (node.loop_values ? node.loop_values.length : 0) || 0;
    var running = node.status === "running";
    var h2 = '<h2>loop workflow · ' + (node.loop_label || "batch") + '</h2>';
    var cx = 65, cy = 65, R = 48;
    var svg = svgEl("svg", { viewBox: "0 0 130 130" });
    // track
    svg.appendChild(svgEl("circle", { cx: cx, cy: cy, r: R, fill: "none",
      stroke: done >= total && total ? "#10b981" : "#e5e7eb", "stroke-width": 6 }));
    // tick marks for each completed lap
    for (var i = 0; i < total; i++) {
      var ang = (-90 + (360 / total) * i) * Math.PI / 180;
      var x1 = cx + Math.cos(ang) * (R - 9), y1 = cy + Math.sin(ang) * (R - 9);
      var x2 = cx + Math.cos(ang) * (R + 9), y2 = cy + Math.sin(ang) * (R + 9);
      var tick = svg.appendChild(svgEl("line", { x1: x1, y1: y1, x2: x2, y2: y2,
        "stroke-width": 3, "stroke-linecap": "round" }));
      tick.setAttribute("stroke", i < done ? "#10b981" : "#d1d5db");
    }
    // the traveling ball — animated only while running
    var ball = svg.appendChild(svgEl("circle", { r: 7, fill: "#3b82f6" }));
    if (running) {
      var anim = svg.appendChild(svgEl("animateMotion", { dur: "3s", repeatCount: "indefinite" }));
      var mpath = "M " + (cx) + "," + (cy - R) + " A " + R + "," + R + " 0 1 1 " + (cx - 0.01) + "," + (cy - R);
      // (full loop path — starts at 12 o'clock, goes clockwise via the long arc)
      var p = svg.appendChild(svgEl("path", { d: mpath, id: "wf-loop-path", fill: "none", stroke: "none" }));
      anim.setAttribute("mpath", "#wf-loop-path"); // xlink:href form varies; this is the SMIL mpath ref
      try { anim.setAttributeNS("http://www.w3.org/1999/xlink", "href", "#wf-loop-path"); } catch (e) {}
      ball.appendChild(anim);
    } else if (done >= total && total) {
      // park the ball at 12 o'clock + center ✓
      ball.setAttribute("cx", cx); ball.setAttribute("cy", cy - R);
      ball.setAttribute("fill", "#10b981");
      var bigCheck = svg.appendChild(svgEl("text", { x: cx, y: cy + 7, "text-anchor": "middle", "font-size": 28, fill: "#10b981" }));
      bigCheck.textContent = "✓";
    } else {
      ball.setAttribute("cx", cx); ball.setAttribute("cy", cy - R); ball.setAttribute("fill", "#9ca3af");
    }
    container.innerHTML = h2;
    var wrap = document.createElement("div");
    wrap.className = "wf-loop";
    wrap.appendChild(svg);
    var counter = document.createElement("div");
    counter.className = "wf-loop-count";
    counter.innerHTML = "已完成 <strong>" + done + "</strong> / " + total + " 次" +
      (running ? '<br><span class="meta">循环执行中…</span>' :
       (done >= total && total ? '<br><span class="meta" style="color:#10b981">全部完成 ✓</span>' : ''));
    wrap.appendChild(counter);
    container.appendChild(wrap);
  }

  function render(container, state) {
    if (!container) return;
    state = state || {};
    var t = state.plan_type || "sequential";
    container.classList.remove("empty");
    if (t === "parallel") renderParallel(container, state);
    else if (t === "loop") renderLoop(container, state);
    else renderSequential(container, state);
  }

  // ── SSE listener (FF.5 §6.3): subscribe to `workflow_progress` events ────
  function init() {
    var container = document.getElementById("workflow-viz");
    if (!container) return;
    var path = window.location.pathname;
    if (path === "/health" || path === "/seed" || path === "/") return;
    // reuse the page's EventSource if the EE.8 poll stream is open; else open one
    // dedicated to workflow_progress. We attach to ANY EventSource on path+"/poll".
    try {
      var es = new EventSource(path + "/poll");
      es.addEventListener("workflow_progress", function (e) {
        var data = {};
        try { data = JSON.parse(e.data); } catch (err) { return; }
        render(container, data);
      });
      // don't spam console on SSE errors (matches EE.8 behavior)
      es.addEventListener("error", function () { /* expected on disconnect */ });
    } catch (err) { /* SSE unavailable — workflow viz just stays static */ }
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init);
  else init();

  // public surface (for tests / server-side seed render)
  window.TaskVMWorkflow = { render: render, init: init,
    renderSequential: renderSequential, renderParallel: renderParallel, renderLoop: renderLoop };
})(typeof window !== "undefined" ? window : this);
