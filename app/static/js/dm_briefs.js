/* dm_briefs.js — private babyg brief interactions.

   Two behaviors, both progressive-enhancement over plain forms:

   1. "use draft": copies the suggested reply into the composer input.
      It NEVER sends — sending stays an explicit user action via the
      composer's own submit.
   2. "ask babyg": re-generates the brief via fetch, then reloads so the
      fresh brief renders. Without JS the same form posts and the server
      redirects back (a reload), so the feature still works.
*/
(function () {
  "use strict";

  document.addEventListener("click", function (e) {
    var useBtn = e.target.closest("[data-use-draft]");
    if (!useBtn) return;
    var panel = useBtn.closest("[data-dm-brief]");
    var replyEl = panel && panel.querySelector("[data-brief-reply]");
    var input = document.querySelector("[data-dm-composer-input]");
    if (replyEl && input) {
      input.value = (replyEl.textContent || "").trim();
      input.focus();
    }
  });

  document.addEventListener("submit", function (e) {
    var form = e.target.closest("[data-brief-refresh]");
    if (!form) return;
    e.preventDefault();
    var btn = form.querySelector("[data-ask-babyg]");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "babyg is reading…";
    }
    fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.json().catch(function () {
          return { ok: r.ok };
        });
      })
      .then(function () {
        window.location.reload();
      })
      .catch(function () {
        if (btn) {
          btn.disabled = false;
          btn.textContent = "try again";
        }
      });
  });
})();
