/* submit_feedback.js — instant feedback when a form button is tapped.

   Most babyg buttons are plain HTML forms: the tap posts, the server
   redirects, and the next page loads. Until that page paints (often most
   of a second) nothing on screen changed, so taps felt ignored and got
   repeated. This marks the tapped button busy the moment its form
   submits, and ignores repeat submits of that form while it's on its way.

   It never changes what a form sends: no button is disabled (a disabled
   button drops its name/value from the submission — e.g. the DM brief
   follow-up buttons' `focus`), nothing is serialized or fetched here, and
   forms a page script already handles itself (Manager composer, DM briefs,
   connections, Instagram evaluation — they call preventDefault) are left
   alone. The listener sits on window so it runs after every form- and
   document-level submit listener and sees their final decision.
*/
(function () {
  "use strict";

  var SUBMITTING = "data-submitting";
  var BUSY = "data-submit-busy";
  // If the next page never arrives (navigation stopped, network dropped),
  // hand the button back so the tap can be retried.
  var RELEASE_MS = 10000;

  function release(form) {
    form.removeAttribute(SUBMITTING);
    var busy = form.querySelectorAll("[" + BUSY + "]");
    for (var i = 0; i < busy.length; i++) {
      busy[i].removeAttribute(BUSY);
      busy[i].removeAttribute("aria-busy");
    }
  }

  window.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || event.defaultPrevented) return;
    var button = event.submitter || null;
    var target =
      (button && button.getAttribute("formtarget")) || form.getAttribute("target");
    if (target && target !== "_self") return;

    if (form.hasAttribute(SUBMITTING)) {
      // Already on its way: a second tap would post the same action twice.
      event.preventDefault();
      return;
    }
    form.setAttribute(SUBMITTING, "");
    if (button && button !== form && form.contains(button)) {
      button.setAttribute(BUSY, "");
      button.setAttribute("aria-busy", "true");
    }
    setTimeout(function () {
      release(form);
    }, RELEASE_MS);
  });

  // Coming back to this page from history must not show stale busy buttons.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) return;
    var forms = document.querySelectorAll("form[" + SUBMITTING + "]");
    for (var i = 0; i < forms.length; i++) release(forms[i]);
  });
})();
