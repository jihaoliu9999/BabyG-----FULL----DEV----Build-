(() => {
  const composer = document.querySelector("[data-bot-composer]");
  const typing = document.getElementById("bot-typing");
  const submit = document.querySelector("[data-bot-submit]");
  const textarea = document.getElementById("bot-message");
  const promptButtons = document.querySelectorAll("[data-bot-prompt]");

  const scrollToLatest = () => {
    const target = typing && !typing.hidden ? typing : composer;
    if (target) {
      target.scrollIntoView({ block: "end", behavior: "smooth" });
    }
  };

  scrollToLatest();

  if (!composer || !typing || !submit || !textarea) {
    return;
  }

  composer.addEventListener("submit", () => {
    const value = textarea.value.trim();
    if (!value) {
      return;
    }
    typing.hidden = false;
    submit.setAttribute("disabled", "disabled");
    submit.textContent = "sending";
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

  promptButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const prompt = button.getAttribute("data-bot-prompt") || "";
      textarea.value = prompt;
      textarea.focus();
    });
  });
})();
