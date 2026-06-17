/* bot.js — async chat with no full-page reload.
 *
 * The form is a real <form method="post" action="/creator/bot"> so a
 * no-JS client (disabled JS, JS error, fetch failure) still works the
 * old PRG way: browser navigates, server redirects back. With JS, we
 * intercept submit, POST the same FormData via fetch with
 * X-Requested-With: fetch, swap the message list <ol> innerHTML with
 * the server-rendered partial, and never reload the page shell.
 *
 * Action card confirm/cancel forms are intercepted via delegation —
 * any <form data-bot-action> goes through the same async path.
 *
 * CSRF: untouched. We send the same FormData the native form would,
 * so the csrf_token hidden input rides along and the middleware
 * verifies it from the body exactly like before.
 */
(() => {
  const composer = document.querySelector("[data-bot-composer]");
  const submit = document.querySelector("[data-bot-submit]");
  const textarea = document.getElementById("bot-message");
  const messageList = document.getElementById("botMessages");
  const errorSlot = document.querySelector("[data-bot-error-slot]");

  if (!composer || !submit || !textarea || !messageList) return;

  let inFlight = false;
  let blurTimer = null;

  // Generous slack: treat "a little above the bottom" as still following
  // the conversation, so arriving messages keep us pinned without fighting
  // a user who has scrolled up to read older history.
  const NEAR_BOTTOM_SLACK = 80;

  const isNearBottom = (el) =>
    el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_SLACK;

  // .bot-messages is the scroll container (flex:1; overflow-y:auto), so set
  // scrollTop directly — instant and far more reliable than scrollIntoView,
  // which is flaky on first paint.
  const scrollToBottom = (smooth) => {
    const top = messageList.scrollHeight;
    if (smooth && messageList.scrollTo) {
      messageList.scrollTo({ top, behavior: "smooth" });
    } else {
      messageList.scrollTop = top;
    }
  };

  // Pin to the newest message across late layout shifts. A deferred script
  // runs before web fonts apply and before images load — both grow the list
  // afterward and would otherwise strand the view above the latest message.
  // Double rAF waits for layout + paint; we also re-pin on load + fonts.
  const pinToBottom = () => {
    requestAnimationFrame(() => {
      scrollToBottom(false);
      requestAnimationFrame(() => scrollToBottom(false));
    });
  };

  // Initial load: land on the newest message, robustly.
  pinToBottom();
  window.addEventListener("load", pinToBottom);
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(pinToBottom);
  }

  const setBusy = (busy) => {
    inFlight = busy;
    if (busy) {
      submit.setAttribute("disabled", "disabled");
      submit.setAttribute("aria-busy", "true");
      textarea.setAttribute("readonly", "readonly");
    } else {
      submit.removeAttribute("disabled");
      submit.removeAttribute("aria-busy");
      textarea.removeAttribute("readonly");
    }
  };

  const addTyping = () => {
    const li = document.createElement("li");
    li.className = "bot-message assistant";
    li.setAttribute("data-bot-typing", "");
    li.innerHTML =
      '<span class="meta">babyg</span>' +
      '<div class="bubble" aria-label="thinking"><span class="bot-typing-dots"><span></span><span></span><span></span></span></div>';
    messageList.appendChild(li);
    scrollToBottom(true);
    return li;
  };

  const addUserMessage = (text) => {
    const li = document.createElement("li");
    li.className = "bot-message user";
    li.setAttribute("data-bot-optimistic", "");
    li.innerHTML =
      '<span class="meta">you</span>' +
      '<div class="bubble">' +
      escapeHtml(text) +
      "</div>";
    messageList.appendChild(li);
    scrollToBottom(true);
  };

  const removeTyping = () => {
    const t = messageList.querySelector("[data-bot-typing]");
    if (t) t.remove();
  };

  const showInlineError = (text) => {
    if (!errorSlot) return;
    errorSlot.innerHTML =
      '<div class="bot-banner bot-banner-error" data-bot-banner ' +
      'style="margin:0 24px 12px;padding:12px 16px;background:rgba(255,77,109,0.1);' +
      'border:1px solid var(--lime);border-radius:12px;color:var(--lime);font-size:13px;">' +
      escapeHtml(text) +
      "</div>";
  };

  const clearInlineError = () => {
    if (!errorSlot) return;
    errorSlot.innerHTML = "";
  };

  const escapeHtml = (s) =>
    String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  const swapList = (html, preserveBottomLock) => {
    const wasNearBottom = preserveBottomLock && isNearBottom(messageList);
    const prevScroll = messageList.scrollTop;
    messageList.innerHTML = html;
    if (wasNearBottom) {
      // The list was just replaced — re-pin after the new DOM paints.
      pinToBottom();
    } else {
      // User is reading older history; hold their position.
      messageList.scrollTop = prevScroll;
    }
  };

  // The server returns the partial as either:
  //   - a chunk of <li> + optional banner (200 OK), or
  //   - same shape with an embedded error banner (4xx)
  // The partial sometimes leads with the banner (.bot-banner-error)
  // and sometimes with <li> messages. We split: any leading banner
  // goes to errorSlot, the rest replaces the message-list innerHTML.
  const applyPartial = (html) => {
    clearInlineError();
    const wrap = document.createElement("div");
    wrap.innerHTML = html;
    const banner = wrap.querySelector(".bot-banner");
    if (banner && errorSlot) {
      errorSlot.innerHTML = banner.outerHTML;
      banner.remove();
    }
    swapList(wrap.innerHTML, true);
  };

  async function postForm(form) {
    const fd = new FormData(form);
    return fetch(form.action, {
      method: form.method || "POST",
      body: fd,
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
    });
  }

  async function postComposer(fd) {
    return fetch(composer.action, {
      method: composer.method || "POST",
      body: fd,
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
    });
  }

  const resetTextarea = () => {
    textarea.value = "";
    textarea.defaultValue = "";
    textarea.textContent = "";
    textarea.removeAttribute("value");
    textarea.style.height = "";
    composer.reset();
    textarea.value = "";
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  };

  const setKeyboardOpen = (open) => {
    document.body.classList.toggle("chat-keyboard-open", open);
    // Force an immediate inset recompute so the hero/composer snap to
    // the new viewport instead of waiting for the next visualViewport
    // event (which can lag the focus by ~200ms on iOS).
    updateKeyboardInset();
  };

  // Publish the keyboard height as --keyboard-inset on <html>. The CSS
  // subtracts it from `#view` height so the composer never sits behind
  // the iOS keyboard accessory bar and the hero never gets scrolled
  // off-screen. dvh alone doesn't track keyboard transitions cleanly
  // on iOS; visualViewport.height + offsetTop is the reliable signal.
  function updateKeyboardInset() {
    const vv = window.visualViewport;
    if (!vv) {
      document.documentElement.style.setProperty("--keyboard-inset", "0px");
      return;
    }
    const inset = Math.max(
      0,
      Math.round(window.innerHeight - vv.height - vv.offsetTop)
    );
    document.documentElement.style.setProperty(
      "--keyboard-inset",
      inset + "px"
    );
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", updateKeyboardInset);
    window.visualViewport.addEventListener("scroll", updateKeyboardInset);
    updateKeyboardInset();
  }

  async function send() {
    if (inFlight) return;
    const value = textarea.value.trim();
    if (!value) return;

    clearInlineError();
    const fd = new FormData(composer);
    resetTextarea();
    setBusy(true);
    addUserMessage(value);
    addTyping();

    try {
      const resp = await postComposer(fd);
      const html = await resp.text();
      removeTyping();
      if (resp.ok) {
        applyPartial(html);
      } else {
        // The 400 response is still the same partial with an embedded
        // banner — apply it so the existing history doesn't vanish.
        applyPartial(html);
      }
    } catch (_e) {
      removeTyping();
      showInlineError(
        "couldn't reach babyg. your message wasn't sent — try again."
      );
    } finally {
      setBusy(false);
      textarea.focus();
    }
  }

  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    send();
  });

  textarea.addEventListener("focus", () => {
    if (blurTimer) window.clearTimeout(blurTimer);
    setKeyboardOpen(true);
  });

  textarea.addEventListener("blur", () => {
    blurTimer = window.setTimeout(() => setKeyboardOpen(false), 120);
  });

  textarea.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    if (textarea.value.trim() && !inFlight) {
      composer.requestSubmit();
    }
  });

  // Delegated handler for action card confirm/cancel forms inside the
  // message list. They re-render after each list swap, so we listen on
  // the parent.
  messageList.addEventListener("submit", async (e) => {
    const form = e.target.closest("form[data-bot-action]");
    if (!form) return;
    if (inFlight) {
      e.preventDefault();
      return;
    }
    e.preventDefault();
    setBusy(true);
    addTyping();
    try {
      const resp = await postForm(form);
      const html = await resp.text();
      removeTyping();
      if (resp.ok || resp.status === 400) {
        applyPartial(html);
      } else {
        showInlineError("couldn't save that action — try again.");
      }
    } catch (_e) {
      removeTyping();
      showInlineError("network error — your action wasn't saved.");
    } finally {
      setBusy(false);
    }
  });
})();
