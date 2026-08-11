/* profile photo upload — client-side compressor.
 *
 * iPhone photos (HDR / Live / portrait) routinely land at 4–8 MB. Shipping
 * those raw over cellular is slow and pointless: the server resizes
 * everything to 512×512 ~50 KB JPEG anyway. This module decodes the user's
 * pick in the browser, draws it onto a canvas at a max long-edge of 1280 px,
 * re-encodes as JPEG at quality 0.85, and substitutes the shrunken blob
 * back into the <input type="file"> so the form submits a tiny file.
 *
 * Failure mode: if the browser can't decode the file (older Android trying
 * to read HEIC, etc.) we silently fall back to uploading the original. The
 * server is still the authority on accept/reject and will surface the same
 * `?photo=bad_type` / `?photo=too_big` flashes it always has.
 *
 * No external libs: `createImageBitmap` + <canvas> + `DataTransfer` are
 * baseline in every modern browser.
 */
(() => {
  const form = document.querySelector("[data-profile-photo-form]");
  const input = document.querySelector("[data-profile-photo-input]");
  const previews = document.querySelectorAll("[data-profile-photo-preview]");
  const initials = document.querySelectorAll("[data-profile-photo-initials]");
  const openers = document.querySelectorAll("[data-profile-photo-open]");
  const submit = document.querySelector("[data-profile-photo-submit]");
  const label = document.querySelector("[data-profile-photo-label]");
  const status = document.querySelector("[data-profile-photo-status]");

  if (!form || !input || !previews.length || !submit) {
    return;
  }

  // Matches MAX_UPLOAD_BYTES in app/services/storage.py AND the
  // Supabase bucket's own file_size_limit (migrations/0010). Keeping
  // all three in lockstep means a borderline upload either passes
  // every gate or fails fast in the browser with a clean alert.
  const MAX_BYTES = 6 * 1024 * 1024;
  // Long-edge cap for the compressed copy. Bigger than the server's
  // final 512 so the server's center-crop has source pixels to work
  // with; small enough that a 4032×3024 iPhone photo lands ~150 KB.
  const COMPRESS_MAX_EDGE = 1280;
  const COMPRESS_QUALITY = 0.85;

  const setStatus = (text) => {
    if (!status) return;
    if (!text) {
      status.hidden = true;
      status.textContent = "";
    } else {
      status.hidden = false;
      status.textContent = text;
    }
  };

  const showPreview = (blobOrFile) => {
    const url = URL.createObjectURL(blobOrFile);
    previews.forEach((preview) => {
      preview.src = url;
      preview.hidden = false;
      preview.closest(".avatar")?.classList.add("avatar-photo");
    });
    initials.forEach((initial) => {
      initial.hidden = true;
    });
    submit.hidden = true;
    if (label) label.textContent = "saving photo";
  };

  const showInitialsFallback = (preview) => {
    preview.hidden = true;
    preview.removeAttribute("src");
    const avatar = preview.closest(".avatar");
    avatar?.classList.remove("avatar-photo");
    const initial = avatar?.querySelector("[data-profile-photo-initials]");
    if (initial) {
      initial.hidden = false;
    }
  };

  previews.forEach((preview) => {
    preview.addEventListener("error", () => showInitialsFallback(preview));
  });

  const submitPhotoChange = () => {
    setStatus("saving...");
    submit.hidden = true;
    if (typeof form.requestSubmit === "function") {
      form.requestSubmit(submit);
    } else {
      form.submit();
    }
  };

  openers.forEach((opener) => {
    opener.addEventListener("click", () => input.click());
  });

  // Swap a compressed Blob into the file input so the form submits it
  // as a normal multipart upload. DataTransfer is the standard way to
  // mutate FileList; works in every browser that has canvas.toBlob.
  const setInputFile = (blob, originalName) => {
    try {
      const ext = blob.type === "image/jpeg" ? "jpg" : "bin";
      const name = (originalName || "photo").replace(/\.[^.]+$/, "") + "." + ext;
      const file = new File([blob], name, { type: blob.type });
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      return true;
    } catch (_e) {
      // Very old browser without DataTransfer.items.add — give up and
      // let the original file (already in input.files) ship as-is.
      return false;
    }
  };

  async function compress(file) {
    // createImageBitmap is the fastest path and is what Safari uses to
    // decode HEIC on iOS. If decoding throws (Firefox/Chrome on a HEIC
    // file, for example) the caller falls back to the original file.
    let bitmap;
    try {
      bitmap = await createImageBitmap(file);
    } catch (_e) {
      return null;
    }

    const w = bitmap.width;
    const h = bitmap.height;
    const longEdge = Math.max(w, h);
    const scale = longEdge > COMPRESS_MAX_EDGE
      ? COMPRESS_MAX_EDGE / longEdge
      : 1;
    const cw = Math.max(1, Math.round(w * scale));
    const ch = Math.max(1, Math.round(h * scale));

    const canvas = document.createElement("canvas");
    canvas.width = cw;
    canvas.height = ch;
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      bitmap.close && bitmap.close();
      return null;
    }
    ctx.drawImage(bitmap, 0, 0, cw, ch);
    bitmap.close && bitmap.close();

    return new Promise((resolve) => {
      canvas.toBlob(
        (blob) => resolve(blob),
        "image/jpeg",
        COMPRESS_QUALITY,
      );
    });
  }

  input.addEventListener("change", async () => {
    const original = input.files && input.files[0];
    if (!original) return;

    // Quick alert on the absolute cap. The server enforces this too,
    // but bouncing here saves a round-trip on huge files.
    if (original.size > MAX_BYTES) {
      alert("photo is too big — max 6 MB");
      input.value = "";
      return;
    }

    setStatus("compressing…");
    let working = original;
    try {
      const compressed = await compress(original);
      if (compressed && compressed.size > 0 && compressed.size < original.size) {
        const swapped = setInputFile(compressed, original.name);
        if (swapped) {
          working = compressed;
        }
      }
      // If compression failed or didn't shrink the file, leave the
      // original in input.files and let the server handle it.
    } catch (_e) {
      // Same fallback — never block the user on compression errors.
    }
    setStatus(null);

    showPreview(working);
    window.setTimeout(submitPhotoChange, 0);
  });
})();

(() => {
  const buttons = document.querySelectorAll("[data-location-button]");
  if (!buttons.length) return;

  // BigDataCloud's free client-side reverse-geocode. No API key, no auth
  // header — designed for browser use. Returns city/locality + principalSubdivision
  // (region/state) + countryName. We only fill empty inputs so the user's
  // typing always wins over our guess.
  async function reverseGeocode(form, lat, lng, status) {
    const cityInput = form.querySelector('[name="location_city"]');
    const regionInput = form.querySelector('[name="location_region"]');
    const countryInput = form.querySelector('[name="location_country"]');
    const url =
      "https://api.bigdatacloud.net/data/reverse-geocode-client" +
      `?latitude=${encodeURIComponent(lat)}` +
      `&longitude=${encodeURIComponent(lng)}` +
      "&localityLanguage=en";
    try {
      const resp = await fetch(url, { credentials: "omit" });
      if (!resp.ok) throw new Error("rg http " + resp.status);
      const data = await resp.json();
      const city = data.city || data.locality || "";
      const region = data.principalSubdivision || "";
      const country = data.countryName || "";
      // Only fill blanks so we don't clobber what the user typed.
      if (cityInput && !cityInput.value && city) cityInput.value = city;
      if (regionInput && !regionInput.value && region) regionInput.value = region;
      if (countryInput && !countryInput.value && country) countryInput.value = country;
      if (status) {
        const filled = [city, region].filter(Boolean).join(", ") || country;
        status.textContent = filled
          ? `location captured: ${filled}. edit if it's off, then save.`
          : "location captured. add a city or region if you want it shown on your profile.";
      }
    } catch (_err) {
      // Geocode failed — coords still save, just no auto-filled label.
      if (status) {
        status.textContent =
          "location captured. add a city or region if you want it shown on your profile.";
      }
    }
  }

  buttons.forEach((button) => {
    const form = button.closest("form");
    if (!form) return;
    const status = form.querySelector("[data-location-status]");
    const lat = form.querySelector("[data-location-lat]");
    const lng = form.querySelector("[data-location-lng]");
    const source = form.querySelector("[data-location-source]");

    button.addEventListener("click", () => {
      if (!("geolocation" in navigator)) {
        if (status) status.textContent = "browser location is not available here.";
        return;
      }
      button.setAttribute("disabled", "disabled");
      if (status) status.textContent = "asking for location permission...";
      navigator.geolocation.getCurrentPosition(
        (position) => {
          if (lat) lat.value = String(position.coords.latitude);
          if (lng) lng.value = String(position.coords.longitude);
          if (source) source.value = "browser";
          if (status) status.textContent = "location captured. resolving city…";
          reverseGeocode(
            form,
            position.coords.latitude,
            position.coords.longitude,
            status
          ).finally(() => {
            button.removeAttribute("disabled");
          });
          return;
        },
        () => {
          if (status) status.textContent = "location not shared. enter your city manually.";
          button.removeAttribute("disabled");
        },
        { enableHighAccuracy: false, timeout: 8000, maximumAge: 600000 }
      );
    });
  });
})();

(() => {
  const management = document.querySelector("#profile-management");
  const links = document.querySelectorAll("[data-open-profile-management]");
  if (!management || !links.length) return;

  links.forEach((link) => {
    link.addEventListener("click", () => {
      management.open = true;
    });
  });
})();

(() => {
  const triggers = document.querySelectorAll("[data-profile-chip-open]");
  const dialogs = document.querySelectorAll("[data-profile-chip-dialog]");

  if (!triggers.length || !dialogs.length) {
    return;
  }

  const byId = new Map();
  dialogs.forEach((dialog) => {
    byId.set(dialog.id, dialog);

    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) {
        closeDialog(dialog);
      }
    });

    dialog.addEventListener("close", () => {
      resetDialog(dialog);
      if (![...dialogs].some((item) => item.open)) {
        document.documentElement.classList.remove("profile-chip-lock");
      }
    });

    dialog.querySelectorAll("[data-profile-chip-close]").forEach((button) => {
      button.addEventListener("click", () => closeDialog(dialog));
    });
  });

  function openDialog(dialog) {
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
    document.documentElement.classList.add("profile-chip-lock");
  }

  function closeDialog(dialog) {
    if (typeof dialog.close === "function" && dialog.open) {
      dialog.close();
    } else {
      dialog.removeAttribute("open");
      resetDialog(dialog);
    }
    if (![...dialogs].some((item) => item.open)) {
      document.documentElement.classList.remove("profile-chip-lock");
    }
  }

  function resetDialog(dialog) {
    const form = dialog.querySelector("form");
    if (form) {
      form.reset();
    }
  }

  triggers.forEach((trigger) => {
    trigger.addEventListener("click", () => {
      const section = trigger.getAttribute("data-profile-chip-open");
      const dialog = byId.get(`profile-chip-${section}`);
      if (dialog) {
        openDialog(dialog);
      }
    });
  });
})();
