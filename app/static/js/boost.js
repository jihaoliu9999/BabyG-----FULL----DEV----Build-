/* boost.js — promotes the non-blocking Google Fonts stylesheet.

   Navigation is deliberately NOT intercepted here. This file used to
   catch internal /creator link clicks, fetch the destination, and try to
   swap the page content in place. The swap could never succeed (it
   compared body classes after adding its own `boost-loading` class), so
   every intercepted tap fetched and rendered the destination, discarded
   it, then fell back to location.assign: two server renders per tap,
   and GET side effects (discover impressions, profile views) recorded
   twice. Links and back/forward are plain browser navigation — one tap,
   one request.
*/
(function () {
  "use strict";

  // Promote non-blocking Google Fonts on every page (creator, brand,
  // operator, auth, landing). base.html ships the fonts link with
  // media="print" so first paint isn't blocked on the network round-
  // trip; here we flip it to media="all" once boost.js parses.
  try {
    var _webfont = document.querySelector("link[data-webfont-swap]");
    if (_webfont) _webfont.media = "all";
  } catch (_e) {
    /* harmless — proceed to normal boot */
  }
})();
