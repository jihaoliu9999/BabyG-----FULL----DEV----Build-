(function () {
  "use strict";

  var forms = document.querySelectorAll("[data-instagram-evaluate]");
  if (!forms.length) return;

  var sections = [
    "Summary",
    "Worth responding?",
    "Why",
    "Opportunity",
    "Risk",
    "Urgency",
    "Missing information",
    "Suggested next steps",
  ];

  function targetsFor(form) {
    var card = form.closest(".ig-review-card") || document;
    return {
      button: form.querySelector("button[type='submit']"),
      statusEl: card.querySelector("[data-instagram-eval-status]"),
      resultEl: card.querySelector("[data-instagram-evaluation]"),
    };
  }

  function showStatus(statusEl, title, body) {
    if (!statusEl) return;
    statusEl.hidden = false;
    statusEl.innerHTML = "";
    var strong = document.createElement("strong");
    strong.textContent = title;
    var p = document.createElement("p");
    p.textContent = body;
    statusEl.appendChild(strong);
    statusEl.appendChild(p);
  }

  function showEvaluation(resultEl, evaluation) {
    if (!resultEl || !evaluation) return;
    resultEl.hidden = false;
    resultEl.innerHTML = "";
    sections.forEach(function (section) {
      var article = document.createElement("article");
      var label = document.createElement("span");
      label.className = "mono";
      label.textContent = section;
      var p = document.createElement("p");
      p.textContent = evaluation[section] || "";
      article.appendChild(label);
      article.appendChild(p);
      resultEl.appendChild(article);
    });
  }

  forms.forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var targets = targetsFor(form);
      if (targets.button) {
        targets.button.disabled = true;
        targets.button.textContent = "evaluating";
      }
      if (targets.statusEl) targets.statusEl.hidden = true;
      fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin",
      })
        .then(function (response) {
          return response.json().then(function (payload) {
            return { response: response, payload: payload };
          });
        })
        .then(function (result) {
          if (!result.response.ok || !result.payload.ok) {
            throw new Error(result.payload.state || "internal_error");
          }
          showEvaluation(targets.resultEl, result.payload.evaluation);
        })
        .catch(function (error) {
          var state = error.message || "internal_error";
          if (state === "provider_failure") {
            showStatus(targets.statusEl, "ai provider failure", "babyg could not reach the evaluator. try again shortly.");
          } else if (state === "ownership_failure") {
            showStatus(targets.statusEl, "authorization failure", "this Instagram thread is not available for this account.");
          } else {
            showStatus(targets.statusEl, "internal error", "babyg could not evaluate this message.");
          }
        })
        .then(function () {
          if (targets.button) {
            targets.button.disabled = false;
            targets.button.textContent = "Evaluate this message";
          }
        });
    });
  });
})();
