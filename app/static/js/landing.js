(function () {
  "use strict";

  var root = document.querySelector(".lp");
  if (!root) return;

  document.documentElement.classList.add("js-ready");

  var header = document.getElementById("site-header");
  if (header) {
    var onScroll = function () {
      if (window.scrollY > 8) header.classList.add("is-scrolled");
      else header.classList.remove("is-scrolled");
    };
    document.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  var reveals = Array.from(root.querySelectorAll(".reveal"));
  function checkReveals() {
    var vh = window.innerHeight;
    reveals.forEach(function (el) {
      if (el.classList.contains("in")) return;
      var r = el.getBoundingClientRect();
      if (r.top < vh * 0.92 && r.bottom > 0) {
        el.classList.add("in");
      }
    });
  }
  requestAnimationFrame(function () {
    checkReveals();
    requestAnimationFrame(checkReveals);
  });
  window.addEventListener("scroll", checkReveals, { passive: true });
  window.addEventListener("resize", checkReveals);

  var loopSection = root.querySelector("[data-loop-section]");
  if (loopSection) {
    var loopSteps = Array.from(loopSection.querySelectorAll("[data-loop-step]"));
    var loopPanels = Array.from(loopSection.querySelectorAll("[data-loop-panel]"));
    var audienceButtons = Array.from(loopSection.querySelectorAll("[data-loop-audience]"));
    var toneButtons = Array.from(loopSection.querySelectorAll("[data-loop-tone]"));
    var draftCopy = loopSection.querySelector("[data-loop-draft]");
    var composer = loopSection.querySelector("[data-loop-composer]");
    var useDraft = loopSection.querySelector("[data-loop-use-draft]");
    var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var loopStep = 0;
    var loopAudience = "creator";
    var loopTone = "warm";
    var loopPaused = false;
    var loopTimer = null;
    var draftReplies = {
      warm: "hey! love this, i'm definitely open. could you share the budget, usage length, platforms, and whether paid ad rights are included?",
      business: "thanks, i'm open to this. can you clarify the budget, usage length, platforms, and whether paid ad rights are included?",
      firm: "happy to discuss. before we go further i'll need the budget, usage length, platforms, and paid ad rights confirmed."
    };

    function setLoopCopy() {
      Array.from(loopSection.querySelectorAll("[data-copy-creator]")).forEach(function (el) {
        var next = el.getAttribute("data-copy-" + loopAudience);
        if (next) el.textContent = next;
      });
      Array.from(loopSection.querySelectorAll("[data-initial-creator]")).forEach(function (el) {
        var next = el.getAttribute("data-initial-" + loopAudience);
        if (next) el.textContent = next;
      });
      root.setAttribute("data-loop-audience", loopAudience);
    }

    function scheduleLoop() {
      if (reduceMotion) return;
      clearTimeout(loopTimer);
      if (loopPaused) return;
      loopTimer = setTimeout(function () {
        activateLoopStep((loopStep + 1) % loopSteps.length);
      }, loopStep === 3 ? 6200 : 4200);
    }

    function activateLoopStep(nextStep) {
      loopStep = nextStep;
      loopSteps.forEach(function (button, index) {
        var active = index === loopStep;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
      });
      loopPanels.forEach(function (panel, index) {
        var active = index === loopStep;
        panel.hidden = !active;
        panel.classList.toggle("is-active", active);
      });
      scheduleLoop();
    }

    function setAudience(nextAudience) {
      loopAudience = nextAudience;
      audienceButtons.forEach(function (button) {
        var active = button.getAttribute("data-loop-audience") === loopAudience;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
      });
      setLoopCopy();
    }

    function setTone(nextTone) {
      loopTone = nextTone;
      toneButtons.forEach(function (button) {
        button.classList.toggle("is-active", button.getAttribute("data-loop-tone") === loopTone);
      });
      if (draftCopy) draftCopy.textContent = draftReplies[loopTone];
    }

    loopSteps.forEach(function (button) {
      var index = parseInt(button.getAttribute("data-loop-step"), 10);
      button.addEventListener("click", function () {
        activateLoopStep(index);
      });
      button.addEventListener("mouseenter", function () {
        activateLoopStep(index);
      });
      button.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          activateLoopStep(index);
        }
      });
    });

    audienceButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        setAudience(button.getAttribute("data-loop-audience"));
      });
    });

    toneButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        setTone(button.getAttribute("data-loop-tone"));
      });
    });

    if (useDraft && composer && draftCopy) {
      useDraft.addEventListener("click", function () {
        composer.value = draftCopy.textContent;
        composer.focus();
      });
    }

    loopSection.addEventListener("mouseenter", function () {
      loopPaused = true;
      clearTimeout(loopTimer);
    });
    loopSection.addEventListener("mouseleave", function () {
      loopPaused = false;
      scheduleLoop();
    });
    loopSection.addEventListener("focusin", function () {
      loopPaused = true;
      clearTimeout(loopTimer);
    });
    loopSection.addEventListener("focusout", function () {
      loopPaused = false;
      scheduleLoop();
    });

    setLoopCopy();
    setTone(loopTone);
    activateLoopStep(loopStep);
  }

  var heroMark = document.getElementById("heroMark");
  if (heroMark && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    var markImg = heroMark.querySelector(".mark-img");
    var targetX = 0, targetY = 0, curX = 0, curY = 0;
    document.addEventListener("mousemove", function (e) {
      var cx = window.innerWidth / 2;
      var cy = window.innerHeight / 2;
      targetX = ((e.clientX - cx) / cx) * 14;
      targetY = ((e.clientY - cy) / cy) * 14;
    });
    (function loop() {
      curX += (targetX - curX) * 0.06;
      curY += (targetY - curY) * 0.06;
      if (markImg) {
        markImg.style.translate = curX + "px " + curY + "px";
      }
      requestAnimationFrame(loop);
    })();
  }

  // The IntersectionObserver-driven [data-count] counter animation
  // for the homepage stats section was removed alongside the markup.
  // No other surface used data-count, so the helper went with it.

  var bgMarks = root.querySelectorAll(".bg-mark");
  if (bgMarks.length && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    var speeds = [0.12, 0.18, 0.10, 0.20, 0.14, 0.16, 0.13];
    var baseTops = Array.prototype.map.call(bgMarks, function (m) { return m.offsetTop; });
    var ty = window.scrollY;
    var cy = ty;
    window.addEventListener("scroll", function () { ty = window.scrollY; }, { passive: true });
    (function tick() {
      cy += (ty - cy) * 0.12;
      Array.prototype.forEach.call(bgMarks, function (mark, i) {
        var offset = (cy - baseTops[i]) * speeds[i % speeds.length];
        mark.style.transform = "translateY(" + offset.toFixed(2) + "px)";
      });
      requestAnimationFrame(tick);
    })();
  }
})();
