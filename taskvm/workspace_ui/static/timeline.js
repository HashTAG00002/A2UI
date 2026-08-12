/* timeline.js — honesty-based rollback 'progress bar 拖不回去' (E9.2) +
 * FF.1 §2.2 G (locked-drag shake) + FF.6 §7.2 celebrateCheckpoint.
 *
 * The saga-rollback timeline's handle is the literal embodiment of the user's
 * analogy: a progress bar you drag LEFT to undo. It moves freely through GREEN
 * (reverted / reversible) segments but SNAPS to a hard stop at any 🔒
 * (irreversible) segment — '拖不回这一步'. When it hits the stop the handle
 * flashes red ('.blocked') AND shakes (FF.1 §2.2 G — re-triggered each time
 * the user pushes against the lock), while the locked segment briefly
 * brightens (CSS `.saga-bar:has(.handle.blocked) .seg.lock`).
 *
 * celebrateCheckpoint(name) (FF.6 §7.2): fires confetti from both sides +
 * pops a spring-animated green badge naming the checkpoint. Called by the
 * page JS when the /<sid>/checkpoint (or /adopt_milestone) response carries
 * `milestone_reached` / `checkpoint_reached`.
 *
 * Pure vanilla JS (no framework — handoff §4.3). No-roles: the static render
 * (segments + colors + honest one-liner) is the primary honest artifact the
 * gate/audit greps; this script only adds the interactive drag + celebration.
 */
(function (window) {
  "use strict";

  function initTimeline(tl) {
    var bar = tl.querySelector(".saga-bar");
    var handle = tl.querySelector(".handle");
    if (!bar || !handle) return;

    var locks = Array.prototype.slice.call(bar.querySelectorAll(".seg.lock"));
    function width() { return bar.clientWidth || 1; }
    // each lock's right edge as a fraction of bar width (0..1)
    function lockRightFraction(seg) {
      return (seg.offsetLeft + seg.offsetWidth) / width();
    }
    // the handle cannot move left past the right edge of any 🔒 segment
    var minPos = 0.0;
    locks.forEach(function (s) { minPos = Math.max(minPos, lockRightFraction(s)); });

    // position state (fraction from left). Start far-right = action complete.
    var pos = 1.0;
    // partial_failure: the saga already undid as far as it could — park the
    // handle at minPos (the lock's right edge = 'as far back as it got').
    if (tl.classList.contains("partial")) pos = minPos;

    function render() {
      var w = width();
      var hw = handle.offsetWidth || 8;
      var left = pos * w - hw / 2;
      handle.style.left = Math.max(0, Math.min(w - hw, left)) + "px";
      handle.style.right = "auto";
      handle.setAttribute("aria-valuenow", String(Math.round(pos * 100)));
    }
    render();

    // FF.1 §2.2 G: re-trigger the shake each time the user pushes against the
    // 🔒 stop. CSS .handle.blocked carries the shake animation; toggling the
    // class off→reflow→on restarts it so repeated pushes keep shaking.
    var shakeTimer = null;
    function triggerShake() {
      handle.classList.remove("blocked");
      // force reflow so the animation restarts cleanly
      void handle.offsetWidth;
      handle.classList.add("blocked");
      // clear any stale timer (don't let the class linger if the user stops)
      if (shakeTimer) clearTimeout(shakeTimer);
      shakeTimer = setTimeout(function () {
        if (!atStop()) handle.classList.remove("blocked");
      }, 350);
    }

    // nothing locked → the bar is fully reversible; no drag stop to demonstrate
    if (tl.classList.contains("full") || locks.length === 0) return;

    var dragging = false;

    function clamp(p) { return Math.max(minPos, Math.min(1.0, p)); }
    function atStop() { return pos <= minPos + 0.002; }

    handle.addEventListener("pointerdown", function (e) {
      dragging = true;
      try { handle.setPointerCapture(e.pointerId); } catch (_) {}
      handle.classList.remove("blocked");
      if (shakeTimer) { clearTimeout(shakeTimer); shakeTimer = null; }
      e.preventDefault();
    });
    document.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      var rect = bar.getBoundingClientRect();
      var w = width();
      var p = (e.clientX - rect.left) / w;
      pos = clamp(p);
      // FF.1 §2.2 G: hitting the 🔒 stop → shake + brighten the locked seg.
      // Re-trigger only when the user is actively pushing past the stop (not
      // just resting on it) so it doesn't fire on every render.
      if (p < minPos - 0.001) {
        if (!handle.classList.contains("blocked") || atStop()) triggerShake();
      } else if (p > minPos + 0.01) {
        handle.classList.remove("blocked");
      }
      render();
    });
    function endDrag() {
      if (!dragging) return;
      dragging = false;
      if (atStop()) { pos = minPos; handle.classList.add("blocked"); }
      else handle.classList.remove("blocked");
      render();
    }
    document.addEventListener("pointerup", endDrag);
    document.addEventListener("pointercancel", endDrag);

    // keyboard: ← tries to undo (blocked at the 🔒 + shakes); → re-applies
    handle.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      var step = 0.04;
      var np = (e.key === "ArrowLeft") ? pos - step : pos + step;
      var before = pos;
      pos = clamp(np);
      if (np < minPos && before <= minPos + 0.002) {
        // already at the stop, pushing harder → shake (FF.1 §2.2 G)
        triggerShake();
      } else if (pos > minPos) {
        handle.classList.remove("blocked");
      }
      render();
    });
  }

  function init() {
    var tls = document.querySelectorAll(".saga-timeline");
    for (var i = 0; i < tls.length; i++) {
      try { initTimeline(tls[i]); } catch (e) { /* never let JS break the render */ }
    }
  }

  // ── FF.6 §7.2: checkpoint celebration (confetti from both sides + badge) ──
  function celebrateCheckpoint(checkpointName) {
    checkpointName = checkpointName || "checkpoint";
    // 1. confetti from both sides (uses the local confetti.min.js — no CDN)
    if (typeof window.confetti === "function") {
      try {
        window.confetti({ particleCount: 120, spread: 70, origin: { x: 0.2, y: 0.6 } });
        window.confetti({ particleCount: 120, spread: 70, origin: { x: 0.8, y: 0.6 } });
      } catch (e) { /* confetti is best-effort — never break the page */ }
    }
    // 2. central green badge with the checkpoint name — spring scale 0→1.1→1
    var badge = document.createElement("div");
    badge.className = "checkpoint-badge";
    badge.innerHTML = '<span class="ckpt-icon">✅</span>'
      + '<span class="ckpt-name"></span>'
      + '<span class="ckpt-sub">里程碑达成</span>';
    badge.querySelector(".ckpt-name").textContent = String(checkpointName);
    document.body.appendChild(badge);
    // spring animation: scale 0 → 1.1 → 1
    badge.style.transform = "translate(-50%, -50%) scale(0)";
    requestAnimationFrame(function () {
      badge.style.transition = "transform .4s cubic-bezier(.34,1.56,.64,1)";
      badge.style.transform = "translate(-50%, -50%) scale(1.1)";
      setTimeout(function () {
        badge.style.transform = "translate(-50%, -50%) scale(1)";
      }, 180);
    });
    // 3. 2.5s after the pop, fade out + remove
    setTimeout(function () {
      badge.style.transition = "opacity .3s, transform .3s";
      badge.style.opacity = "0";
      badge.style.transform = "translate(-50%, -50%) scale(0.9)";
      setTimeout(function () {
        if (badge.parentNode) document.body.removeChild(badge);
      }, 300);
    }, 2500);
  }

  // expose globally so the page's inline JS (or a fetch handler) can call it
  window.celebrateCheckpoint = celebrateCheckpoint;

  // ── wire celebration triggers: listen for JSON responses on the
  // /<sid>/checkpoint + /<sid>/adopt_milestone POSTs (FF.6 + FF.3). When the
  // server returns `milestone_reached` / `checkpoint_reached: true`, fire.
  // The form posts follow a redirect to the HTML page, so we ALSO read a
  // query hint (?celebrate=<name>) the server sets on the redirect; this
  // guarantees the celebration fires for human (browser) form submits too.
  function maybeCelebrateFromQuery() {
    try {
      var qs = new URLSearchParams(window.location.search);
      var name = qs.get("celebrate");
      if (name) celebrateCheckpoint(decodeURIComponent(name));
    } catch (e) { /* URL parsing best-effort */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(); maybeCelebrateFromQuery(); initCheckpointTimeline(); });
  } else { init(); maybeCelebrateFromQuery(); initCheckpointTimeline(); }

  // ── GG.5: checkpoint-timeline drag → /<sid>/rollback_to ──────────────────
  // Each .cp-tick (a checkpoint刻度, draggable) posts rollback_to on drop /
  // Enter. An irreversible span (the server's rollback outcome has n_locked>0)
  // → the刻度 gets class="locked" + a shake (reuse triggerShake). The sid is
  // read from the page URL (/<sid>).
  function initCheckpointTimeline() {
    var tl = document.querySelector(".cp-timeline");
    if (!tl) return;
    var sid = window.location.pathname.replace(/^\/+/, "");
    function rollbackTo(cpId, tick) {
      fetch("/" + sid + "/rollback_to", {
        method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: "target_checkpoint_id=" + encodeURIComponent(cpId)
      }).then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (d) {
          if (d && d.partial_failure) {
            // an irreversible saga in the span → lock this刻度 + shake
            if (tick) { tick.classList.add("locked"); triggerShake(tick); }
            alert("部分步骤不可逆（" + (d.n_locked || 0) + " 步 🔒），无法完全回退到 " + cpId +
                  "。该刻度已锁死。");
          }
          // reload the page to reflect the new state (re-rendered ro-zone + saga timeline)
          window.location.reload();
        }).catch(function (e) {
          // fetch failed (e.g. server down) — fall back to a form-style reload
          window.location.reload();
        });
    }
    tl.querySelectorAll(".cp-tick").forEach(function (tick) {
      tick.addEventListener("click", function (ev) {
        // click = rollback to this checkpoint (a simple, accessible affordance)
        if (tick.classList.contains("locked")) { triggerShake(tick); return; }
        rollbackTo(tick.getAttribute("data-cp"), tick);
      });
      tick.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          if (tick.classList.contains("locked")) { triggerShake(tick); return; }
          rollbackTo(tick.getAttribute("data-cp"), tick);
        }
      });
      // drag support (HTML5 draggable) — drop anywhere = rollback to this cp
      tick.addEventListener("dragend", function (ev) {
        if (tick.classList.contains("locked")) { triggerShake(tick); return; }
        rollbackTo(tick.getAttribute("data-cp"), tick);
      });
    });
  }
})(typeof window !== "undefined" ? window : this);
