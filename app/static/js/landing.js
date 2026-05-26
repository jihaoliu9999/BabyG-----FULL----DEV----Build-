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

  var countEls = root.querySelectorAll("[data-count]");
  if (countEls.length && "IntersectionObserver" in window) {
    var ciO = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var el = e.target;
        var target = parseInt(el.dataset.count, 10);
        var suffix = el.dataset.suffix || "";
        if (target === 0) {
          ciO.unobserve(el);
          return;
        }
        var n = 0;
        var dur = 1100;
        var start = performance.now();
        function tick(now) {
          var t = Math.min(1, (now - start) / dur);
          var eased = 1 - Math.pow(1 - t, 3);
          n = Math.round(target * eased);
          el.textContent = n + suffix;
          if (t < 1) requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);
        ciO.unobserve(el);
      });
    }, { threshold: 0.4 });
    countEls.forEach(function (el) { ciO.observe(el); });
  }

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
