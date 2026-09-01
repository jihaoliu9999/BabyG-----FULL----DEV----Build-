(function () {
  "use strict";

  var prefersReducedMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var REVEAL_SELECTORS = [
    ".intel-card",
    ".listing-card",
    ".creator-card",
    ".event-card",
    ".thread-row",
    ".role-card",
    ".card",
    ".onb-section",
    ".chip-group",
    ".kv-list"
  ];

  function tagReveal(el, idx) {
    if (el.classList.contains("reveal")) return;
    el.classList.add("reveal");
    var delayClass = "reveal-delay-" + (Math.min(5, (idx % 5) + 1));
    el.classList.add(delayClass);
  }

  function init() {
    if (prefersReducedMotion || !("IntersectionObserver" in window)) {
      REVEAL_SELECTORS.forEach(function (sel) {
        document.querySelectorAll(sel).forEach(function (el) {
          el.classList.add("reveal", "is-visible");
        });
      });
      return;
    }

    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    }, {
      root: null,
      rootMargin: "0px 0px -8% 0px",
      threshold: 0.04
    });

    REVEAL_SELECTORS.forEach(function (sel) {
      var nodes = document.querySelectorAll(sel);
      nodes.forEach(function (el, idx) {
        tagReveal(el, idx);
        observer.observe(el);
      });
    });
  }

  function pinToLatest() {
    // The DM thread's actual scroll container is `.dm-thread-messages-wrap`
    // (the `<ol class="dm-messages">` inside it isn't the overflow ancestor).
    // Bot chat's scroller is `.bot-messages`. Operator abuse review keeps
    // its default top-anchored scroll — operators need the oldest context
    // first — so `.dm-messages` alone is deliberately not in the selector.
    var lists = document.querySelectorAll(
      ".bot-messages, .dm-thread-messages-wrap"
    );
    lists.forEach(function (list) {
      // Keep scrolling local to the message list. scrollIntoView() can move
      // the entire document on iOS and leave the app shell vertically offset.
      list.scrollTop = list.scrollHeight;
    });
  }

  function bindAutogrow() {
    var areas = document.querySelectorAll(".bot-composer textarea, .dm-composer textarea, [data-bot-composer] textarea");
    areas.forEach(function (ta) {
      function grow() {
        ta.style.height = "auto";
        ta.style.height = Math.min(220, ta.scrollHeight) + "px";
      }
      ta.addEventListener("input", grow);
      grow();
    });
  }

  function bindButtonPress() {
    if (prefersReducedMotion) return;
    document.addEventListener("pointerdown", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var btn = t.closest(".btn, .chip, .intel-card, .listing-card, .creator-card, .thread-row, .role-card");
      if (!btn) return;
      btn.style.transition = "transform 80ms cubic-bezier(0.22, 1, 0.36, 1)";
      btn.style.transform = (btn.style.transform || "") + " scale(0.985)";
      var release = function () {
        btn.style.transform = "";
        btn.removeEventListener("pointerup", release);
        btn.removeEventListener("pointerleave", release);
        btn.removeEventListener("pointercancel", release);
      };
      btn.addEventListener("pointerup", release);
      btn.addEventListener("pointerleave", release);
      btn.addEventListener("pointercancel", release);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      init();
      pinToLatest();
      bindAutogrow();
      bindButtonPress();
    });
  } else {
    init();
    pinToLatest();
    bindAutogrow();
    bindButtonPress();
  }
})();
