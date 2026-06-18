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
    var input = document.querySelector("[data-dm-composer-input]");
    var draft = useBtn.getAttribute("data-draft") || "";
    if (draft && input) {
      input.value = draft.trim();
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
        return r.json().then(function (data) {
          return { response: r, data: data };
        }).catch(function () {
          return { response: r, data: { ok: r.ok } };
        });
      })
      .then(function (result) {
        if (!result.response.ok) throw new Error(result.data.error || "refresh failed");
        window.location.reload();
      })
      .catch(function () {
        if (btn) {
          btn.disabled = false;
          btn.textContent = "try again";
        }
      });
  });

  document.addEventListener("submit", function (e) {
    var form = e.target.closest("[data-brief-follow-up]");
    if (!form) return;
    e.preventDefault();
    var submitter = e.submitter;
    if (!submitter) return;
    var resultEl = form.parentElement.querySelector("[data-follow-up-result]");
    var data = new FormData(form);
    data.set("focus", submitter.value);
    Array.prototype.forEach.call(form.querySelectorAll("button"), function (button) {
      button.disabled = true;
    });
    if (resultEl) {
      resultEl.hidden = false;
      resultEl.textContent = "babyg is reading the thread...";
    }
    fetch(form.action, {
      method: "POST",
      body: data,
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.json().then(function (payload) {
          if (!r.ok || !payload.ok) throw new Error(payload.error || "follow-up failed");
          return payload.result;
        });
      })
      .then(function (result) {
        if (!resultEl) return;
        resultEl.textContent = "";
        var title = document.createElement("strong");
        title.textContent = result.title || "babyg follow-up";
        var analysis = document.createElement("p");
        analysis.textContent = result.analysis || "babyg needs more context.";
        resultEl.appendChild(title);
        resultEl.appendChild(analysis);
        if (result.draft) {
          var draft = document.createElement("p");
          draft.className = "dm-brief-reply-text";
          draft.textContent = result.draft;
          var button = document.createElement("button");
          button.type = "button";
          button.className = "btn btn-ghost btn-sm";
          button.setAttribute("data-use-draft", "");
          button.setAttribute("data-draft", result.draft);
          button.textContent = "use draft";
          resultEl.appendChild(draft);
          resultEl.appendChild(button);
        }
      })
      .catch(function () {
        if (resultEl) resultEl.textContent = "babyg could not complete that review. try again shortly.";
      })
      .then(function () {
        Array.prototype.forEach.call(form.querySelectorAll("button"), function (button) {
          button.disabled = false;
        });
      });
  });
})();
