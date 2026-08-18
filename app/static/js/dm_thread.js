// dm_thread.js — babyg reply chips fill the composer, never auto-send.
// Also handles ?draft= URL param used when a chip on the DM list opens
// this thread with a suggested reply pre-filled.
(function () {
  var root = document.querySelector("[data-dm-thread]");
  if (!root) return;
  var input = root.querySelector("[data-dm-composer-input]");
  if (!input) return;

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
