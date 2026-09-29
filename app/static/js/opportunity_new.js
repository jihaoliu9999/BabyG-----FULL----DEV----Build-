/* The local card preview never submits or writes form state. */
(function () {
  "use strict";
  const root = document.querySelector("[data-op-new]");
  if (!root) return;
  const form = root.querySelector("form");
  const field = (name) => form.elements.namedItem(name);
  const preview = (name) => root.querySelector(`[data-op-preview-${name}]`);
  const update = () => {
    preview("title").textContent = field("title").value.trim() || "opportunity title";
    preview("description").textContent = field("description").value.trim() || "Your opportunity description will appear here.";
    root.querySelector("[data-op-count]").textContent = `${field("description").value.length}/2000`;
    for (const [name, input] of [["type", "listing_type"], ["compensation", "compensation_type"]]) {
      const selected = form.querySelector(`input[name="${input}"]:checked`);
      if (!selected) continue;
      const label = selected.closest("label");
      preview(name).querySelector("span").textContent = label.querySelector("strong").textContent;
      preview(name).querySelector("svg").replaceWith(label.querySelector("svg").cloneNode(true));
    }
    const location = field("location").value.trim();
    preview("location").hidden = !location;
    preview("location").querySelector("span").textContent = location;
    const date = field("deadline").value;
    preview("date").hidden = !date;
    // Format the selected calendar date directly, without timezone conversion.
    const parsed = date ? new Date(`${date}T12:00:00`) : null;
    preview("date").querySelector("span").textContent = parsed && !Number.isNaN(parsed.getTime())
      ? `Closes ${parsed.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}` : "";
    const tags = field("target_niches").value.split(",").map((tag) => tag.trim().toLowerCase().slice(0, 40)).filter(Boolean).slice(0, 6);
    preview("tags").replaceChildren(...tags.map((tag) => {
      const span = document.createElement("span");
      span.textContent = tag;
      return span;
    }));
  };
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  update();
  // This existing route belongs to Discover; reuse its existing active styling.
  for (const nav of document.querySelectorAll('#sidebar-nav, #tabbar')) {
    const discover = nav.querySelector('a[href="/creator/discover"]');
    if (discover) {
      discover.classList.add("active");
      discover.setAttribute("aria-current", "page");
    }
  }
})();
