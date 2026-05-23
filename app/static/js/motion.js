(function () {
  "use strict";

  var prefersReducedMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var REVEAL_SELECTORS = [
    ".marketing-feature",
    ".intel-card",
    ".op-card",
    ".role-card",
    ".creator-card",
    ".record-card",
    ".dm-thread-row",
    ".notif-item",
    ".feed-status-card",
    ".onb-section",
    ".mini-stat",
    ".views-count-card",
    ".event-card",
    ".receipt-card",
    ".performance-card",
    ".connection-card",
    ".connection-section",
    ".bot-empty",
    ".feed-empty",
    ".dm-empty",
    ".notif-strip",
    ".op-table tbody tr"
  ];

  function tagReveal(el, idx) {
    if (el.classList.contains("reveal")) return;
    el.classList.add("reveal");
    var delayClass = "reveal-delay-" + (Math.min(5, (idx % 5) + 1));
    el.classList.add(delayClass);
  }

  function init() {
    if (prefersReducedMotion || !("IntersectionObserver" in window)) {
      // Reveal immediately for accessibility / older browsers
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
      threshold: 0.06
    });

    REVEAL_SELECTORS.forEach(function (sel) {
      var nodes = document.querySelectorAll(sel);
      nodes.forEach(function (el, idx) {
        tagReveal(el, idx);
        observer.observe(el);
      });
    });
  }

  // Smooth scroll to bottom of message/chat threads on initial paint
  function pinToLatest() {
    var lists = document.querySelectorAll(".bot-messages, .dm-messages");
    lists.forEach(function (list) {
      var last = list.lastElementChild;
      if (last && last.scrollIntoView) {
        last.scrollIntoView({ block: "end" });
      }
    });
  }

  // Auto-grow textareas in composers
  function bindAutogrow() {
    var areas = document.querySelectorAll(".bot-composer textarea, .dm-composer textarea");
    areas.forEach(function (ta) {
      function grow() {
        ta.style.height = "auto";
        ta.style.height = Math.min(220, ta.scrollHeight) + "px";
      }
      ta.addEventListener("input", grow);
      grow();
    });
  }

  // Ripple-ish feedback on primary buttons
  function bindButtonPress() {
    if (prefersReducedMotion) return;
    document.addEventListener("pointerdown", function (e) {
      var t = e.target;
      if (!t || !t.closest) return;
      var btn = t.closest(".btn, .chip, .feedback-btn, .role-card, .op-card, .creator-card, .dm-thread-link");
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
