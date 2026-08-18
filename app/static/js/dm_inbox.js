// dm_inbox.js — client-side search + all/unread filter for the DM list.
//
// The server renders every row. This script hides rows that don't match
// the current filter or search term, and updates the empty-hint. No
// network calls; no scroll jump when the query changes.
(function () {
  var root = document.querySelector("[data-dm-inbox]");
  if (!root) return;

  var searchInput = root.querySelector("[data-dm-search]");
  var filters = root.querySelectorAll("[data-dm-filter]");
  var rows = root.querySelectorAll("[data-dm-row]");
  var emptyHint = root.querySelector("[data-dm-empty-hint]");

  var mode = "all";
  var term = "";

  function applyFilter() {
    var q = term.trim().toLowerCase();
    var visible = 0;
    rows.forEach(function (row) {
      var name = row.getAttribute("data-dm-name") || "";
      var unread = row.getAttribute("data-dm-unread") === "1";
      var matchesFilter = mode === "all" || (mode === "unread" && unread);
      var matchesQuery = !q || name.indexOf(q) !== -1;
      var show = matchesFilter && matchesQuery;
      row.hidden = !show;
      if (show) visible++;
    });
    if (emptyHint) emptyHint.hidden = visible !== 0 || rows.length === 0;
  }

  if (searchInput) {
    searchInput.addEventListener("input", function (e) {
      term = e.target.value || "";
      applyFilter();
    });
  }

  filters.forEach(function (btn) {
    btn.addEventListener("click", function () {
      mode = btn.getAttribute("data-dm-filter") || "all";
      filters.forEach(function (b) {
        var active = b === btn;
        b.classList.toggle("active", active);
        b.setAttribute("aria-selected", active ? "true" : "false");
      });
      applyFilter();
    });
  });

  applyFilter();
})();
