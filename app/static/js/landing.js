(function () {
  "use strict";

  var root = document.querySelector(".lp");
  if (!root) return;

  document.documentElement.classList.add("js-ready");

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var header = document.getElementById("site-header");

  if (header) {
    var onScroll = function () {
      header.classList.toggle("is-scrolled", window.scrollY > 8);
    };
    document.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  var reveals = Array.prototype.slice.call(root.querySelectorAll(".reveal"));
  if ("IntersectionObserver" in window) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
    reveals.forEach(function (el) { observer.observe(el); });
  } else {
    reveals.forEach(function (el) { el.classList.add("in"); });
  }

  var heroMark = document.getElementById("heroMark");
  if (heroMark && !reduceMotion) {
    var markImg = heroMark.querySelector(".mark-img");
    var targetX = 0;
    var targetY = 0;
    var currentX = 0;
    var currentY = 0;

    document.addEventListener("mousemove", function (event) {
      var cx = window.innerWidth / 2;
      var cy = window.innerHeight / 2;
      targetX = ((event.clientX - cx) / cx) * 12;
      targetY = ((event.clientY - cy) / cy) * 12;
    }, { passive: true });

    (function drift() {
      currentX += (targetX - currentX) * 0.055;
      currentY += (targetY - currentY) * 0.055;
      if (markImg) {
        markImg.style.translate = currentX.toFixed(2) + "px " + currentY.toFixed(2) + "px";
      }
      window.requestAnimationFrame(drift);
    })();
  }

  Array.prototype.forEach.call(root.querySelectorAll("[data-filter-button]"), function (button) {
    button.addEventListener("click", function () {
      button.classList.toggle("is-active");
    });
  });
})();
