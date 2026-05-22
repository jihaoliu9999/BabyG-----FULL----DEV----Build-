(() => {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const accessToken = params.get("access_token");
  const refreshToken = params.get("refresh_token");
  const type = params.get("type") || "magiclink";
  const errorDescription = params.get("error_description");

  if (errorDescription) {
    window.location.replace(
      `/auth/callback?error_description=${encodeURIComponent(errorDescription)}`,
    );
    return;
  }

  if (!accessToken || !refreshToken) {
    window.location.replace("/auth/callback?callback_error=missing");
    return;
  }

  const form = document.createElement("form");
  form.method = "post";
  form.action = "/auth/callback";

  for (const [name, value] of [
    ["access_token", accessToken],
    ["refresh_token", refreshToken],
    ["type", type],
  ]) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.appendChild(input);
  }

  document.body.appendChild(form);
  form.submit();
})();
