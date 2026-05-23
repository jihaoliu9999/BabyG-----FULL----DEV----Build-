(() => {
  const input = document.querySelector("[data-profile-photo-input]");
  const preview = document.querySelector("[data-profile-photo-preview]");
  const empty = document.querySelector("[data-profile-photo-empty]");

  if (!input || !preview) {
    return;
  }

  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (!file) {
      return;
    }
    const url = URL.createObjectURL(file);
    preview.src = url;
    preview.hidden = false;
    if (empty) {
      empty.hidden = true;
    }
  });
})();
