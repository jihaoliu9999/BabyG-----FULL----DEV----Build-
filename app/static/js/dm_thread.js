// dm_thread.js — babyg reply chips fill the composer, never auto-send.
// Also handles ?draft= URL param used when a chip on the DM list opens
// this thread with a suggested reply pre-filled.
(function () {
  var root = document.querySelector("[data-dm-thread]");
  if (!root) return;
  var input = root.querySelector("[data-dm-composer-input]");
  if (!input) return;

  function updateKeyboardViewport() {
    var visualViewport = window.visualViewport;
    if (!visualViewport) {
      document.documentElement.style.setProperty(
        "--dm-visual-viewport-height",
        Math.round(window.innerHeight || document.documentElement.clientHeight) +
          "px"
      );
      document.body.classList.toggle(
        "dm-keyboard-open",
        document.activeElement === input
      );
      return;
    }

    var visibleHeight = Math.round(
      visualViewport.height + visualViewport.offsetTop
    );
    document.documentElement.style.setProperty(
      "--dm-visual-viewport-height",
      visibleHeight + "px"
    );

    document.body.classList.toggle(
      "dm-keyboard-open",
      document.activeElement === input
    );
  }

  function scheduleKeyboardViewportUpdate() {
    window.requestAnimationFrame(updateKeyboardViewport);
  }

  input.addEventListener("focus", scheduleKeyboardViewportUpdate);
  input.addEventListener("blur", function () {
    window.setTimeout(updateKeyboardViewport, 120);
  });
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", updateKeyboardViewport);
    window.visualViewport.addEventListener("scroll", updateKeyboardViewport);
  }
  window.addEventListener("resize", updateKeyboardViewport);
  window.addEventListener("orientationchange", updateKeyboardViewport);
  updateKeyboardViewport();

  function setDraft(text) {
    if (!text) return;
    input.value = text;
    input.focus();
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // Chips inside the thread fill the composer on click.
  root.querySelectorAll("[data-dm-chip]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      setDraft(chip.getAttribute("data-dm-draft") || "");
    });
  });

  // "report conversation" in the ⋯ menu → reveal the hidden report
  // form and scroll to it. The form itself is server-rendered <details>
  // that also opens on click so the reason field is immediately visible.
  var reportToggle = root.querySelector("[data-dm-report-toggle]");
  var reportWrap = root.querySelector("[data-dm-report]");
  if (reportToggle && reportWrap) {
    reportToggle.addEventListener("click", function () {
      reportWrap.hidden = false;
      var innerDetails = reportWrap.querySelector("details");
      if (innerDetails) innerDetails.open = true;
      // Collapse the ⋯ menu.
      var menu = reportToggle.closest("details");
      if (menu) menu.open = false;
      reportWrap.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  // If the DM-list row's chip forwarded a ?draft=… param, prefill it.
  try {
    var params = new URLSearchParams(window.location.search);
    var draft = params.get("draft");
    if (draft) {
      setDraft(draft);
      // Clean the URL so a refresh doesn't re-inject the draft.
      var clean = window.location.pathname;
      window.history.replaceState({}, "", clean);
    }
  } catch (_e) {
    /* URL API missing — no prefill, no harm */
  }
})();
