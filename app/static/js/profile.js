(() => {
  const form = document.querySelector("[data-profile-photo-form]");
  const input = document.querySelector("[data-profile-photo-input]");
  const preview = document.querySelector("[data-profile-photo-preview]");
  const initials = document.querySelector("[data-profile-photo-initials]");
  const submit = document.querySelector("[data-profile-photo-submit]");
  const label = document.querySelector("[data-profile-photo-label]");

  if (!form || !input || !preview || !submit) {
    return;
  }

  // Matches the server-side cap in app/services/storage.py.
  const MAX_BYTES = 5 * 1024 * 1024;

  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (!file) {
      return;
    }
    if (file.size > MAX_BYTES) {
      alert("photo is too big — max 5 MB");
      input.value = "";
      return;
    }
    const url = URL.createObjectURL(file);
    preview.src = url;
    preview.hidden = false;
    if (initials) {
      initials.hidden = true;
    }
    submit.hidden = false;
    if (label) {
      label.textContent = "pick a different one";
    }
  });
})();
