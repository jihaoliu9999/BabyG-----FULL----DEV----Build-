/* network_connections.js — disconnect an accepted connection.

   Progressive enhancement: each disconnect form posts natively without
   JS (the server flips the row to `removed` and redirects back). With
   JS we add a confirmation step, post via fetch, and update the UI in
   place — removing the row on success or showing an error state without
   a full reload. CSRF rides along in the form's hidden input.
*/
(function () {
  "use strict";

  // Nothing to wire up if there are no accepted connections on the page.
  if (!document.querySelector("[data-disconnect-form]")) return;

  function setStatus(row, msg, isError) {
    if (!row) return;
    var el = row.querySelector("[data-connection-status]");
    if (!el) return;
    el.textContent = msg;
    el.style.color = isError ? "var(--lime)" : "var(--ink-dim)";
  }

  // Bind the delegated submit handler once — this script may be re-run on
  // soft navigation. The handler resolves the form from the live event, so
  // a single binding keeps working after a client-side content swap.
  if (window.__connDisconnectBound) return;
  window.__connDisconnectBound = true;
  document.addEventListener("submit", function (e) {
    var form = e.target.closest("[data-disconnect-form]");
    if (!form) return;
    e.preventDefault();

    var row = form.closest("[data-connection-row]");
    var name = (row && row.getAttribute("data-peer-name")) || "this creator";

    // Confirmation step so a connection is never removed by accident.
    if (
      !window.confirm(
        "disconnect from " + name + "? they'll be removed from your connections."
      )
    ) {
      return;
    }

    var btn = form.querySelector("[data-disconnect-btn]");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "removing…";
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
      .then(function (data) {
        if (data && data.ok) {
          // Success: drop the row immediately, no refresh needed.
          if (row) {
            row.style.transition = "opacity .2s ease";
            row.style.opacity = "0";
            window.setTimeout(function () {
              row.remove();
            }, 200);
          }
        } else {
          setStatus(row, "couldn't disconnect — try again.", true);
          if (btn) {
            btn.disabled = false;
            btn.textContent = "disconnect";
          }
        }
      })
      .catch(function () {
        setStatus(row, "network error — not disconnected.", true);
        if (btn) {
          btn.disabled = false;
          btn.textContent = "disconnect";
        }
      });
  });
})();
