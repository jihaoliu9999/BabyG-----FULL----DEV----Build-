(function () {
  "use strict";

  var root = document.querySelector("[data-discover-root]");
  if (!root) return;

  var toggle = root.querySelector("[data-filter-toggle]");
  var panel = root.querySelector("[data-filter-panel]");
  var closeButton = root.querySelector("[data-filter-close]");
  if (toggle && panel) {
    var mobileFilters = window.matchMedia("(max-width: 1023px)");

    function syncFilterA11y(open) {
      if (mobileFilters.matches) {
        panel.setAttribute("aria-hidden", String(!open));
      } else {
        panel.removeAttribute("aria-hidden");
      }
    }

    function setFiltersOpen(open) {
      panel.classList.toggle("is-open", open);
      root.classList.toggle("is-filter-open", open);
      toggle.setAttribute("aria-expanded", String(open));
      syncFilterA11y(open);
    }

    syncFilterA11y(panel.classList.contains("is-open"));
    function syncFilterA11yOnViewportChange() {
      syncFilterA11y(panel.classList.contains("is-open"));
    }

    if (mobileFilters.addEventListener) {
      mobileFilters.addEventListener("change", syncFilterA11yOnViewportChange);
    } else if (mobileFilters.addListener) {
      mobileFilters.addListener(syncFilterA11yOnViewportChange);
    }

    toggle.addEventListener("click", function () {
      setFiltersOpen(!panel.classList.contains("is-open"));
    });

    if (closeButton) {
      closeButton.addEventListener("click", function () {
        setFiltersOpen(false);
        toggle.focus();
      });
    }

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && panel.classList.contains("is-open")) {
        setFiltersOpen(false);
        toggle.focus();
      }
    });

    document.addEventListener("click", function (event) {
      if (!panel.classList.contains("is-open")) return;
      if (panel.contains(event.target) || toggle.contains(event.target)) return;
      setFiltersOpen(false);
    });
  }

  var cards = Array.prototype.slice.call(root.querySelectorAll("[data-discover-card]"));
  if (!cards.length) return;
  var index = 0;
  var busy = false;
  var count = root.querySelector("[data-card-count]");
  var forms = Array.prototype.slice.call(root.querySelectorAll("[data-swipe-form]"));

  function currentCard() {
    return cards[index] || null;
  }

  function syncDock() {
    var card = currentCard();
    if (!card) return;
    var kind = card.getAttribute("data-card-kind");
    var id = card.querySelector('input[name="target_card_id"]').value;
    forms.forEach(function (form) {
      form.querySelector("[data-target-kind]").value = kind;
      form.querySelector("[data-target-id]").value = id;
    });
    var action = root.querySelector("[data-primary-action]");
    var label = root.querySelector("[data-primary-label]");
    if (action) action.value = kind === "opportunity" ? "interested" : "connected";
    if (label) label.textContent = kind === "opportunity" ? "i'm interested" : "connect";
    if (count) count.textContent = String(cards.length - index);
  }

  function showNext(direction) {
    var card = currentCard();
    if (!card) return;
    card.classList.add(direction === "right" ? "is-leaving-right" : "is-leaving-left");
    window.setTimeout(function () {
      card.hidden = true;
      card.classList.remove("is-leaving-right", "is-leaving-left");
      index += 1;
      var next = currentCard();
      if (next) {
        next.hidden = false;
        next.classList.add("is-entering");
        window.requestAnimationFrame(function () { next.classList.remove("is-entering"); });
        syncDock();
      } else {
        window.location.reload();
      }
      busy = false;
    }, 210);
  }

  forms.forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (busy || !window.fetch) return;
      event.preventDefault();
      busy = true;
      var action = form.querySelector('input[name="action"]').value;
      var body = new URLSearchParams(new FormData(form));
      window.fetch(form.getAttribute("action"), {
        method: "POST",
        body: body,
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
          "X-Requested-With": "fetch"
        }
      }).then(function (response) {
        if (!response.ok) throw new Error("swipe failed");
        showNext(action === "connected" || action === "interested" ? "right" : "left");
      }).catch(function () {
        busy = false;
        form.submit();
      });
    });
  });

  var dragStart = null;
  cards.forEach(function (card) {
    card.addEventListener("pointerdown", function (event) {
      // Ignore drags while a previous swipe is animating out or when this
      // card isn't the visible one — otherwise a rapid tap on a fresh
      // card while its predecessor is still leaving fires two swipes and
      // the transform state gets stuck mid-animation.
      if (busy || card !== currentCard()) return;
      if (event.target.closest("button, a, input")) return;
      dragStart = { x: event.clientX, pointerId: event.pointerId };
      card.setPointerCapture(event.pointerId);
      card.classList.add("is-dragging");
    });
    card.addEventListener("pointermove", function (event) {
      if (!dragStart || dragStart.pointerId !== event.pointerId) return;
      var delta = event.clientX - dragStart.x;
      card.style.transform = "translateX(" + delta + "px) rotate(" + (delta / 35) + "deg)";
    });
    card.addEventListener("pointerup", function (event) {
      if (!dragStart || dragStart.pointerId !== event.pointerId) return;
      var delta = event.clientX - dragStart.x;
      dragStart = null;
      card.classList.remove("is-dragging");
      if (Math.abs(delta) < 80) {
        // Below threshold: spring the card back to center via the base
        // 0.2s transition (already restored by dropping is-dragging).
        card.style.transform = "";
        return;
      }
      var selector = delta > 0 ? '[data-swipe-form="primary"]' : '[data-swipe-form="passed"]';
      var form = root.querySelector(selector);
      if (!form) {
        card.style.transform = "";
        return;
      }
      // Above threshold: continue the animation from where the finger
      // left off. Reset-then-animate causes a one-frame snap back to
      // center before the CSS is-leaving-* class kicks in ("glitchy"
      // rebound). Setting the leaving transform inline lets the base
      // transition ride from the drag position straight off-screen.
      var dir = delta > 0 ? 1 : -1;
      card.style.transform =
        "translateX(" + (dir * 115) + "%) rotate(" + (dir * 7) + "deg)";
      card.style.opacity = "0";
      form.requestSubmit();
    });
    card.addEventListener("pointercancel", function () {
      dragStart = null;
      card.classList.remove("is-dragging");
      card.style.transform = "";
    });
  });

  // Bind the document-level keyboard shortcut exactly once (this script is
  // re-run on soft navigation). Resolve the live discover root at event
  // time so it keeps working after a client-side content swap.
  if (!window.__discoverKeydownBound) {
    window.__discoverKeydownBound = true;
    document.addEventListener("keydown", function (event) {
      if (event.target.matches && event.target.matches("input, textarea, select")) return;
      var liveRoot = document.querySelector("[data-discover-root]");
      if (!liveRoot) return;
      var form = null;
      if (event.key === "ArrowLeft") form = liveRoot.querySelector('[data-swipe-form="passed"]');
      if (event.key.toLowerCase() === "s") form = liveRoot.querySelector('[data-swipe-form="saved"]');
      if (event.key === "ArrowRight") form = liveRoot.querySelector('[data-swipe-form="primary"]');
      if (form) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
  }

  syncDock();
}());
