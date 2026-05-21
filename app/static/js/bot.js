(() => {
  const composer = document.querySelector("[data-bot-composer]");
  const typing = document.getElementById("bot-typing");
  const submit = document.querySelector("[data-bot-submit]");
  const textarea = document.getElementById("bot-message");

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
    submit.textContent = "Sending";
    textarea.setAttribute("readonly", "readonly");
    scrollToLatest();
  });
})();
