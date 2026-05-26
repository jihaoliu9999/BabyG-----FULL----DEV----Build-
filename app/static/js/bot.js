(() => {
  const composer = document.querySelector("[data-bot-composer]");
  const submit = document.querySelector("[data-bot-submit]");
  const textarea = document.getElementById("bot-message");

  const scrollToLatest = () => {
    if (composer) {
      composer.scrollIntoView({ block: "end", behavior: "smooth" });
    }
  };

  scrollToLatest();

  if (!composer || !submit || !textarea) {
    return;
  }

  composer.addEventListener("submit", () => {
    const value = textarea.value.trim();
    if (!value) {
      return;
    }
    // Submit-button feedback only — no fake "thinking" bubble.
    // The browser navigates away after POST; on response the page
    // reloads with fresh HTML and these states naturally reset.
    submit.setAttribute("disabled", "disabled");
    submit.setAttribute("aria-busy", "true");
    textarea.setAttribute("readonly", "readonly");
    scrollToLatest();
  });

  textarea.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }
    event.preventDefault();
    if (textarea.value.trim()) {
      composer.requestSubmit();
    }
  });
})();
