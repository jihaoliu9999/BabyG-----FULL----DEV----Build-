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
  const preview = document.querySelector("[data-profile-photo-preview]");
  const initials = document.querySelector("[data-profile-photo-initials]");
  const submit = document.querySelector("[data-profile-photo-submit]");
  const label = document.querySelector("[data-profile-photo-label]");
  const status = document.querySelector("[data-profile-photo-status]");

  if (!form || !input || !preview || !submit) {
    return;
  }

  // Matches MAX_UPLOAD_BYTES in app/services/storage.py.
  const MAX_BYTES = 10 * 1024 * 1024;
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
    preview.src = url;
    preview.hidden = false;
    if (initials) initials.hidden = true;
    submit.hidden = false;
    if (label) label.textContent = "pick a different one";
  };

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
      alert("photo is too big — max 10 MB");
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
  });
})();
