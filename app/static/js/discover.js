(function () {
  "use strict";

  var root = document.querySelector("[data-discover-root]");
  if (!root) return;

  var toggle = root.querySelector("[data-filter-toggle]");
  var emptyToggle = root.querySelector("[data-filter-toggle-empty]");
  var panel = root.querySelector("[data-filter-panel]");
  var closeButton = root.querySelector("[data-filter-close]");
  if (!toggle || !panel) return;

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

  if (emptyToggle) {
    emptyToggle.addEventListener("click", function () {
      setFiltersOpen(true);
    });
  }

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
    if (emptyToggle && emptyToggle.contains(event.target)) return;
    setFiltersOpen(false);
  });
}());
