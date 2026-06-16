/* onboarding.js — small stepper for the creator signup wizard.
 *
 * All <fieldset data-onb-step="N"> sections live in the same form. With JS,
 * we show one at a time and the user clicks back / continue / finish.
 * The final "finish" button is the only real submit — it posts the
 * complete form to the existing /onboarding/creator endpoint, exactly
 * like the legacy single-page form.
 *
 * Without JS, every fieldset renders visible (no `display:none` is set
 * in CSS), and the form still submits cleanly. Hard fallback to today's
 * behavior.
 *
 * Per-step required-field validation runs before "continue" advances —
 * the browser's native :invalid hints surface inline.
 */
(() => {
  const form = document.querySelector("[data-onb-form]");
  if (!form) return;

  const steps = Array.from(form.querySelectorAll("[data-onb-step]"));
  if (steps.length === 0) return;

  const pips = Array.from(document.querySelectorAll("[data-onb-step-pip]"));
  const prevBtn = document.querySelector("[data-onb-prev]");
  const nextBtn = document.querySelector("[data-onb-next]");
  const finishBtn = document.querySelector("[data-onb-finish]");
  const otherToggle = form.querySelector("[data-onb-other-toggle]");
  const otherField = form.querySelector("[data-onb-other-field]");
  const otherInput = form.querySelector('input[name="niche_other"]');

  const total = steps.length;
  let current = 1;

  const stepEl = (n) => steps.find((s) => s.dataset.onbStep === String(n));
  const pipEl = (n) => pips.find((p) => p.dataset.onbStepPip === String(n));

  const syncOtherField = () => {
    if (!otherToggle || !otherField) return;
    otherField.hidden = !otherToggle.checked;
    if (otherInput) {
      otherInput.required = otherToggle.checked;
    }
    if (!otherToggle.checked && otherInput) {
      otherInput.value = "";
    }
  };

  const selectedLabels = (name) => {
    const checked = Array.from(
      form.querySelectorAll(`[name="${name}"]:checked`)
    ).map((input) => {
      const label = input.closest("label");
      return label ? label.textContent.trim() : input.value;
    }).filter(Boolean);
    if (name === "niches" && otherToggle && otherToggle.checked && otherInput) {
      const custom = otherInput.value.trim();
      return checked
        .filter((value) => value.toLowerCase() !== "other")
        .concat(custom ? [custom] : []);
    }
    return checked;
  };

  const updateReview = () => {
    const reviewFields = form.querySelectorAll("[data-onb-review]");
    if (!reviewFields.length) return;
    reviewFields.forEach((el) => {
      const key = el.dataset.onbReview;
      let value = "";
      if (key === "niches" || key === "content_formats") {
        value = selectedLabels(key).join(", ");
      } else {
        const field = form.querySelector(`[name="${key}"]`);
        value = field ? field.value.trim() : "";
      }
      el.textContent = value || "not set";
    });
  };

  const renderStep = () => {
    steps.forEach((s) => {
      s.hidden = s.dataset.onbStep !== String(current);
    });
    pips.forEach((p) => {
      const n = Number(p.dataset.onbStepPip);
      if (n === current) {
        p.setAttribute("aria-current", "step");
      } else {
        p.removeAttribute("aria-current");
      }
      if (n < current) {
        p.setAttribute("data-done", "");
      } else {
        p.removeAttribute("data-done");
      }
    });
    if (prevBtn) prevBtn.hidden = current === 1;
    if (nextBtn) nextBtn.hidden = current === total;
    if (finishBtn) finishBtn.hidden = current !== total;
    updateReview();
    // Focus the first field of the new step so keyboard users land in
    // the right place.
    const focusTarget = stepEl(current).querySelector(
      "input:not([type=hidden]):not([disabled]), textarea, select"
    );
    if (focusTarget && typeof focusTarget.focus === "function") {
      // Defer so the browser has time to un-hide the fieldset.
      setTimeout(() => {
        try { focusTarget.focus({ preventScroll: false }); } catch (_) {}
      }, 0);
    }
    // Scroll the wizard wrap into view so the new step is at the top.
    const wrap = document.querySelector("[data-onb-wrap]");
    if (wrap && wrap.scrollIntoView) {
      wrap.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  };

  const validateStep = (n) => {
    // Native required-field validation, scoped to the current fieldset.
    // Anything outside this fieldset is ignored so the next step's
    // requireds don't block this one.
    const fieldset = stepEl(n);
    if (!fieldset) return true;
    const inputs = fieldset.querySelectorAll(
      "input[required], select[required], textarea[required]"
    );
    for (const el of inputs) {
      if (!el.checkValidity()) {
        el.reportValidity();
        return false;
      }
    }
    // Chip groups marked with "*" in the label are visually required but
    // are checkbox lists — at least one box must be ticked.
    const chipGroups = fieldset.querySelectorAll(".field");
    for (const fld of chipGroups) {
      const labelMark = fld.querySelector(".req");
      if (!labelMark) continue;
      const inputs = fld.querySelectorAll(
        ".chips input[type=checkbox], .chips input[type=radio], [data-onb-other-toggle]"
      );
      if (inputs.length === 0) continue;
      const anyChecked = Array.from(inputs).some((i) => i.checked);
      if (!anyChecked) {
        // Surface a friendly message via the first hint span.
        const hint = fld.querySelector(".field-hint");
        if (hint) {
          hint.style.color = "var(--lime)";
          setTimeout(() => { hint.style.color = ""; }, 2400);
        }
        return false;
      }
      if (
        fld.querySelector("[data-onb-other-toggle]:checked") &&
        otherInput &&
        !otherInput.value.trim()
      ) {
        otherInput.focus();
        otherInput.reportValidity();
        return false;
      }
    }
    return true;
  };

  if (nextBtn) {
    nextBtn.addEventListener("click", () => {
      if (!validateStep(current)) return;
      if (current < total) {
        current += 1;
        renderStep();
      }
    });
  }
  if (prevBtn) {
    prevBtn.addEventListener("click", () => {
      if (current > 1) {
        current -= 1;
        renderStep();
      }
    });
  }
  // The finish button is `type=submit` — clicking it posts the form
  // natively. We just gate it on the last step's validation; the
  // earlier steps were validated when the user advanced through them.
  if (finishBtn) {
    finishBtn.addEventListener("click", (e) => {
      // Don't preventDefault on success — let the form submit.
      if (!validateStep(current)) {
        e.preventDefault();
      }
    });
  }

  // Block Enter-in-text-input from submitting before the user reaches
  // step N. Textareas keep newline behavior.
  form.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    if (e.target.tagName === "TEXTAREA") return;
    if (current < total) {
      e.preventDefault();
      if (nextBtn) nextBtn.click();
    }
  });

  if (otherToggle) {
    otherToggle.addEventListener("change", syncOtherField);
  }
  if (otherInput) {
    otherInput.addEventListener("input", updateReview);
  }
  form.addEventListener("change", updateReview);
  syncOtherField();
  renderStep();
})();
