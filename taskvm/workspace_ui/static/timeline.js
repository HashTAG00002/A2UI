/* timeline.js — honesty-based rollback 'progress bar 拖不回去' (E9.2).
 *
 * The saga-rollback timeline's handle is the literal embodiment of the user's
 * analogy: a progress bar you drag LEFT to undo. It moves freely through GREEN
 * (reverted / reversible) segments but SNAPS to a hard stop at any 🔒
 * (irreversible) segment — '拖不回这一步'. When it hits the stop the handle
 * flashes red ('.blocked') and the honest message below (rendered server-side
 * from SagaResult.partial_failure) already names the irreversible step.
 *
 * Pure vanilla JS (no framework — handoff §4.3). No-roles: the static render
 * (segments + colors + honest one-liner) is the primary honest artifact the
 * gate/audit greps; this script only adds the interactive drag on top.
 */
(function () {
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

    // nothing locked → the bar is fully reversible; no drag stop to demonstrate
    if (tl.classList.contains("full") || locks.length === 0) return;

    var dragging = false;

    function clamp(p) { return Math.max(minPos, Math.min(1.0, p)); }
    function atStop() { return pos <= minPos + 0.002; }

    handle.addEventListener("pointerdown", function (e) {
      dragging = true;
      try { handle.setPointerCapture(e.pointerId); } catch (_) {}
      handle.classList.remove("blocked");
      e.preventDefault();
    });
    document.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      var rect = bar.getBoundingClientRect();
      var w = width();
      var p = (e.clientX - rect.left) / w;
      pos = clamp(p);
      // show the 'blocked' flash only when the user is actively pushing past
      if (p < minPos - 0.001) handle.classList.add("blocked");
      else handle.classList.remove("blocked");
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

    // keyboard: ← tries to undo (blocked at the 🔒); → re-applies
    handle.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      var step = 0.04;
      var np = (e.key === "ArrowLeft") ? pos - step : pos + step;
      var before = pos;
      pos = clamp(np);
      if (np < minPos && before <= minPos + 0.002) {
        handle.classList.add("blocked");  // already at the stop, pushing harder
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
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init);
  else init();
})();
