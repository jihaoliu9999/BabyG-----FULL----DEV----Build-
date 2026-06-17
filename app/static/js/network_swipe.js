/* Swipe-style network discovery — gesture + keyboard glue.

   Progressive enhancement: the three forms (pass / view profile /
   connect) work without JS, so a keyboard-only or no-JS visitor can
   still drive the page. This file adds:

     * left-arrow  → submit the "pass" form
     * right-arrow → submit the "connect" form
     * enter/space → submit "view profile" only when its button is
                     focused (Space/Enter on other buttons already
                     activates them natively)
     * pointer drag → translate the card. Past a threshold the
                      appropriate form is submitted; below threshold
                      the card springs back. Reduced-motion users
                      get no transition.

   The server-side flow is unchanged — JS only triggers form submits.
   That means action recording, dedup, redirects, all CSRF protection
   happen the same way they would without JS.
*/
(function () {
  "use strict";

  const card = document.querySelector("[data-network-swipe-card]");
  if (!card) return;

  const reducedMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const passForm = card.querySelector('[data-network-action="passed"]');
  const connectForm = card.querySelector('[data-network-action="connected"]');
  const profileForm = card.querySelector('[data-network-action="opened_profile"]');

  function submitForm(form) {
    if (!form) return;
    // Prevent double-submit if a pointer release races a keystroke.
    if (form.dataset.submitted === "1") return;
    form.dataset.submitted = "1";
    form.submit();
  }

  /* ----------------------------- keyboard ----------------------------- */
  document.addEventListener("keydown", function (e) {
    // Don't hijack keystrokes inside form fields.
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    if (e.key === "ArrowLeft") {
      e.preventDefault();
      submitForm(passForm);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      submitForm(connectForm);
    } else if (e.key === "Enter" || e.key === " ") {
      // Only intercept when the view-profile button is focused.
      // Native button activation already handles Enter/Space on the
      // other two — letting it fire prevents double-submits.
      const focused = document.activeElement;
      if (
        focused &&
        focused.getAttribute("data-action-btn") === "opened_profile"
      ) {
        // Native button activation will handle this; no preventDefault.
      }
    }
  });

  /* ----------------------------- pointer ------------------------------ */
  // Pointer events cover both mobile touch and desktop mouse drag.
  // The threshold is ~28% of the card width; small enough to feel
  // light, big enough to avoid accidental triggers on a scroll.
  const THRESHOLD_FRACTION = 0.28;

  let dragging = false;
  let startX = 0;
  let currentDx = 0;
  let pointerId = null;

  function setTransform(dx) {
    // Tiny rotation gives the swipe a tactile feel without going
    // overboard. Capped so the card never flips upside down.
    const rot = Math.max(-10, Math.min(10, dx / 18));
    card.style.transform = "translate3d(" + dx + "px, 0, 0) rotate(" + rot + "deg)";
    const fade = Math.min(1, Math.abs(dx) / (card.offsetWidth * 0.6));
    card.style.opacity = String(1 - fade * 0.4);
  }

  function reset() {
    if (!reducedMotion) {
      card.style.transition = "transform 220ms ease, opacity 220ms ease";
    }
    card.style.transform = "";
    card.style.opacity = "";
    // Drop the transition after it lands so a new drag doesn't lag.
    window.setTimeout(function () {
      card.style.transition = "";
    }, 240);
  }

  function commit(direction) {
    // direction: -1 = pass, +1 = connect
    if (!reducedMotion) {
      card.style.transition = "transform 220ms ease, opacity 220ms ease";
      const off = (card.offsetWidth + 80) * (direction > 0 ? 1 : -1);
      card.style.transform =
        "translate3d(" + off + "px, 0, 0) rotate(" + direction * 14 + "deg)";
      card.style.opacity = "0";
    }
    submitForm(direction > 0 ? connectForm : passForm);
  }

  card.addEventListener("pointerdown", function (e) {
    // Ignore drags that start inside a button — they're clicks, not swipes.
    if (e.target && (e.target.closest("button") || e.target.closest("a"))) {
      return;
    }
    dragging = true;
    pointerId = e.pointerId;
    startX = e.clientX;
    currentDx = 0;
    card.setPointerCapture(e.pointerId);
    card.style.transition = "none";
  });

  card.addEventListener("pointermove", function (e) {
    if (!dragging || e.pointerId !== pointerId) return;
    currentDx = e.clientX - startX;
    setTransform(currentDx);
  });

  function endDrag(e) {
    if (!dragging || (e.pointerId !== undefined && e.pointerId !== pointerId)) return;
    dragging = false;
    try {
      card.releasePointerCapture(pointerId);
    } catch (_) {
      /* the browser may have already released it on pointer cancel */
    }
    pointerId = null;
    const threshold = card.offsetWidth * THRESHOLD_FRACTION;
    if (currentDx > threshold) {
      commit(1);
    } else if (currentDx < -threshold) {
      commit(-1);
    } else {
      reset();
    }
    currentDx = 0;
  }

  card.addEventListener("pointerup", endDrag);
  card.addEventListener("pointercancel", endDrag);
})();
