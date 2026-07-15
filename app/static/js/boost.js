/* boost.js — soft client-side navigation for the babyg creator app.

   Why: every tab click did a full document reload, which tore down and
   repainted the fixed bottom tab bar (it "twitches") and felt slow. With
   JS we intercept internal GET navigations, fetch the next page, and swap
   only the page content + nav active-states + title. The fixed shell (tab
   bar, sidebar) stays mounted, so it never moves and there's no reload.

   Page scripts: most creator pages ship a small page script. We re-run the
   destination page's script after the swap so its DOM gets wired up. Only
   scripts that are safe to re-run are allowed (element-scoped, or with
   their document/window listeners guarded to bind once against the live
   DOM — see SOFT_SAFE). Anything else (chat, dm thread, old network, or an
   inline script) falls back to a normal full navigation, so nothing
   interactive ever breaks.

   No server, CSRF, or form changes — forms still POST and reload normally.
*/
(function () {
  "use strict";

  var root = document.body;
  if (!root || root.className.indexOf("is-creator-app") === -1) return;
  if (!window.history || !window.fetch || !window.DOMParser) return;

  // Loaded on every shell page.
  var SHARED = ["/static/js/motion.js", "/static/js/boost.js"];
  // Page scripts that are safe to (re-)run after a content swap.
  var SOFT_SAFE = [
    "/static/js/discover.js",
    "/static/js/network_connections.js",
    "/static/js/profile.js",
  ];

  // Inspect a document's scripts. `unsafe` means it has a script we can't
  // safely re-run (so we must full-navigate). `page` is the re-runnable
  // page scripts to inject after a swap.
  function classify(doc) {
    var page = [];
    var unsafe = false;
    var scripts = doc.querySelectorAll("script");
    for (var i = 0; i < scripts.length; i++) {
      var s = scripts[i];
      if (!s.src) {
        if ((s.textContent || "").trim()) unsafe = true;
        continue;
      }
      var p = new URL(s.src, location.origin).pathname;
      if (SHARED.indexOf(p) !== -1) continue;
      if (SOFT_SAFE.indexOf(p) !== -1) {
        page.push(p);
        continue;
      }
      unsafe = true;
    }
    return { unsafe: unsafe, page: page };
  }

  // If THIS page runs a script we can't safely re-run (chat, dm thread,
  // old network, or any inline script), don't soft-navigate from it — its
  // listeners could linger. Every navigation from here stays a full load.
  if (classify(document).unsafe) return;

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

  function swapInner(id, doc) {
    var cur = document.getElementById(id);
    var next = doc.getElementById(id);
    if (cur && next) cur.innerHTML = next.innerHTML;
  }

  // Order/whitespace-insensitive body class compare so a trivial markup
  // difference never forces an unnecessary full reload.
  function normClass(s) {
    return (s || "").split(/\s+/).filter(Boolean).sort().join(" ");
  }

  // Replace the previously-injected page scripts with this page's set.
  // Re-running element-scoped scripts rebinds the fresh DOM; document/window
  // listeners in SOFT_SAFE scripts are self-guarded so they don't stack.
  function setPageScripts(srcs) {
    var old = document.querySelectorAll("script[data-page-script]");
    for (var i = 0; i < old.length; i++) old[i].remove();
    srcs.forEach(function (src) {
      var sc = document.createElement("script");
      sc.src = src;
      sc.setAttribute("data-page-script", "");
      document.body.appendChild(sc);
    });
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
          throw new Error("redirected");
        }
        return r.text();
      })
      .then(function (html) {
        if (current !== token) return; // a newer click superseded this one
        var doc = new DOMParser().parseFromString(html, "text/html");
        var cls = classify(doc);
        var nextBody = doc.body ? doc.body.className : "";
        if (
          !doc.getElementById("view") ||
          normClass(nextBody) !== normClass(root.className) ||
          cls.unsafe
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
        setPageScripts(cls.page);
        root.classList.remove("boost-loading");
        document.dispatchEvent(new CustomEvent("babyg:navigated"));
      })
      .catch(function () {
        location.assign(url.href);
      });
  }

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
