/* confirm_submit.js — ask before a destructive form submits.

   A submit button (or its form) with data-confirm="question" shows a
   confirmation dialog first; cancel stops the submit and nothing is
   sent. This replaces inline onclick="return confirm(...)" handlers,
   which the page's Content-Security-Policy (script-src 'self') blocks.

   The listener sits on document, so it runs before submit_feedback.js
   (on window) and that script sees a cancelled submit as prevented. A
   form already on its way (data-submitting, set by submit_feedback.js)
   isn't asked again; that script blocks the repeat.
*/
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) || event.defaultPrevented) return;
    var button = event.submitter || null;
    var question =
      (button && button.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
    if (!question || form.hasAttribute("data-submitting")) return;
    if (!window.confirm(question)) event.preventDefault();
  });
})();
