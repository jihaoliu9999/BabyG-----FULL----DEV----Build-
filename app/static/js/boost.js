/* boost.js — soft client-side navigation for the babyg creator app.

   Problem it solves: every tab click did a full document reload, which
   tore down and repainted the fixed bottom tab bar (it "twitches") and
   felt slow. With JS we intercept internal GET navigations, fetch the
   next page, and swap only the page content + nav active-states + title.
   The shell (fixed tab bar, sidebar) stays mounted, so it never moves,
   and there's no full reload.

   Safety first — this NEVER breaks interactivity or special layouts:
     * Only same-origin GET links under /creator are intercepted.
     * If the destination ships its own <script> (discover, chat, dm
       thread, network, etc.) or uses a different <body> shell class
       (chat / dm-thread), we fall back to a normal full navigation so
       those pages' scripts and layouts work exactly as before.
     * Modified clicks, new-tab, downloads, hashes, redirects, non-HTML,
       and any fetch error all fall back to the browser's default.
   No server, CSRF, or form behavior changes — forms still POST normally.
*/
(function () {
  "use strict";

  var root = document.body;
  if (!root || root.className.indexOf("is-creator-app") === -1) return;
  if (!window.history || !window.fetch || !window.DOMParser) return;

  // Scripts present on every shell page. Anything else in a fetched page
  // means that page has its own behavior → full navigation.
  var SHARED_SCRIPTS = ["/static/js/motion.js", "/static/js/boost.js"];

  function isPlainLeftClick(e) {
    return (
      !e.defaultPrevented &&
      e.button === 0 &&
      !e.metaKey &&
      !e.ctrlKey &&
      !e.shiftKey &&
      !e.altKey
    );
  }

  function eligible(a) {
    if (!a || !a.getAttribute("href")) return null;
    if (a.target && a.target !== "" && a.target !== "_self") return null;
    if (a.hasAttribute("download") || a.hasAttribute("data-no-boost")) return null;
    var url;
    try {
      url = new URL(a.href);
    } catch (_) {
      return null;
    }
    if (url.origin !== location.origin) return null;
    if (url.pathname.indexOf("/creator") !== 0) return null;
    if (url.pathname === location.pathname && url.search === location.search) {
      return null;
    }
    return url;
  }

  function hasOwnScripts(doc) {
    var scripts = doc.querySelectorAll("script");
    for (var i = 0; i < scripts.length; i++) {
      var s = scripts[i];
      if (!s.src) {
        if ((s.textContent || "").trim()) return true; // inline script
        continue;
      }
      var path = new URL(s.src, location.origin).pathname;
      if (SHARED_SCRIPTS.indexOf(path) === -1) return true;
    }
    return false;
  }

  function swapInner(id, doc) {
    var cur = document.getElementById(id);
    var next = doc.getElementById(id);
    if (cur && next) cur.innerHTML = next.innerHTML;
  }

  var current = null;

  function navigate(url, push) {
    var token = {};
    current = token;
    root.classList.add("boost-loading");
    fetch(url.href, {
      headers: { "X-Requested-With": "boost" },
      credentials: "same-origin",
    })
      .then(function (r) {
        var type = r.headers.get("content-type") || "";
        if (!r.ok || type.indexOf("text/html") === -1) throw new Error("not html");
        if (r.redirected && new URL(r.url).pathname !== url.pathname) {
          throw new Error("redirected"); // onboarding/auth bounce → full load
        }
        return r.text();
      })
      .then(function (html) {
        if (current !== token) return; // a newer click superseded this one
        var doc = new DOMParser().parseFromString(html, "text/html");
        var nextBody = doc.body ? doc.body.className : "";
        // Different shell (chat/dm-thread) or page ships scripts → full load.
        if (
          !doc.getElementById("view") ||
          nextBody !== root.className ||
          hasOwnScripts(doc)
        ) {
          location.assign(url.href);
          return;
        }
        swapInner("view", doc);
        swapInner("tabbar", doc);
        swapInner("sidebar-nav", doc);
        var title = doc.querySelector("title");
        if (title) document.title = title.textContent;
        if (push) history.pushState({ boost: true }, "", url.href);
        var view = document.getElementById("view");
        if (view) view.scrollTop = 0;
        window.scrollTo(0, 0);
        root.classList.remove("boost-loading");
        document.dispatchEvent(new CustomEvent("babyg:navigated"));
      })
      .catch(function () {
        location.assign(url.href);
      });
  }

  // If THIS page ships its own scripts (chat, discover, dm thread, ...),
  // their document-level listeners would linger after a soft content swap.
  // Don't activate boost here — navigations from this page stay full loads,
  // which tears those listeners down cleanly. The common static pages
  // (home, profile, dms list, settings, stats) still get instant soft-nav.
  if (hasOwnScripts(document)) return;

  document.addEventListener("click", function (e) {
    if (!isPlainLeftClick(e)) return;
    var a = e.target.closest ? e.target.closest("a") : null;
    var url = eligible(a);
    if (!url) return;
    e.preventDefault();
    navigate(url, true);
  });

  window.addEventListener("popstate", function () {
    var url = new URL(location.href);
    if (url.pathname.indexOf("/creator") !== 0) {
      location.reload();
      return;
    }
    navigate(url, false);
  });
})();
