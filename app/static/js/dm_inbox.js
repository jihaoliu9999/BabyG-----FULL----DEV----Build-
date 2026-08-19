// dm_inbox.js — client-side search for the DM list.
//
// The server renders every row with a data-dm-name attribute containing
// the peer's name, handle, and preview text (all lowercased). Typing in
// the search box hides rows whose data-dm-name doesn't contain the term.
// No network calls; no filter chips (removed with the v4 redesign — the
// unread count lives in the header instead).
(function () {
  var root = document.querySelector("[data-dm-inbox]");
  if (!root) return;

  var searchInput = root.querySelector("[data-dm-search]");
  var rows = root.querySelectorAll("[data-dm-row]");
  var emptyHint = root.querySelector("[data-dm-empty-hint]");
  if (!searchInput) return;

  function applyFilter() {
    var q = (searchInput.value || "").trim().toLowerCase();
    var visible = 0;
    rows.forEach(function (row) {
      var name = row.getAttribute("data-dm-name") || "";
      var show = !q || name.indexOf(q) !== -1;
      row.hidden = !show;
      if (show) visible++;
    });
    if (emptyHint) emptyHint.hidden = visible !== 0 || rows.length === 0;
  }

  searchInput.addEventListener("input", applyFilter);
  applyFilter();
})();
