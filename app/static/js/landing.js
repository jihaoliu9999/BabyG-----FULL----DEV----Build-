(function () {
  "use strict";

  var root = document.querySelector(".lp");
  if (!root) return;

  document.documentElement.classList.add("js-ready");

  var header = document.getElementById("site-header");
  if (header) {
    var onScroll = function () { header.classList.toggle("is-scrolled", window.scrollY > 8); };
    document.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  /* scroll reveal — never leaves content hidden */
  var reveals = Array.prototype.slice.call(root.querySelectorAll(".reveal"));
  var showAll = function () { reveals.forEach(function (el) { el.classList.add("in"); }); };

  if ("IntersectionObserver" in window && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { entry.target.classList.add("in"); io.unobserve(entry.target); }
      });
    }, { threshold: .1, rootMargin: "0px 0px -5% 0px" });
    reveals.forEach(function (el) {
      if (el.getBoundingClientRect().top < window.innerHeight) { el.classList.add("in"); return; }
      io.observe(el);
    });
    window.setTimeout(showAll, 4000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) showAll(); });
  } else {
    showAll();
  }

  /* filter chips */
  Array.prototype.forEach.call(root.querySelectorAll("[data-filter-button]"), function (button) {
    button.addEventListener("click", function () { button.classList.toggle("is-active"); });
  });

  /* old way vs babyg — cycling reasons */
  var SETS = [
    { old: ["takes a percent of the deal", "you have to be big enough to matter", "works business hours, on their time", "you are one name on a long roster"],
      babyg: ["takes nothing off your rate", "works with you at any size", "on all day, all night", "one account to look after, yours"] },
    { old: ["networking costs favors and time", "who you meet depends on who they know", "politics decide who gets in the room", "you hear about it after it is gone"],
      babyg: ["networking is free, no favors owed", "everyone is a search away", "no gatekeeping, no drama", "you see it the day it is posted"] },
    { old: ["you chase the reply for a week", "your schedule is a guess", "one person, one city, one circle", "you do the work and the admin"],
      babyg: ["it replies the same day, every time", "it checks your week before it books", "the whole network, wherever you are", "you make the work, it runs the rest"] },
    { old: ["you find out your rate was low later", "the same questions, answered again", "opportunities sit in an unread inbox", "momentum dies the week you get busy"],
      babyg: ["it knows what the work is worth", "it answers the repeats for you", "nothing sits unread for days", "it keeps things moving while you work"] },
    { old: ["a bad fit costs you the whole month", "you only meet people in your circle", "someone else decides if you are ready", "you are always the one following up"],
      babyg: ["wrong fits never reach you", "you meet outside your circle", "you decide, nobody signs off on you", "it follows up so you never have to"] },
    { old: ["growth depends on who you already know", "every deal starts from scratch", "you guess what the other side wants", "slow weeks stay slow"],
      babyg: ["growth depends on the work you post", "your profile does the first pitch", "you see the fit before you reply", "it fills the quiet weeks"] }
  ];

  var oldList = root.querySelector('[data-compare="old"]');
  var newList = root.querySelector('[data-compare="babyg"]');

  if (oldList && newList) {
    var index = 0;
    var paint = function (list, items, alt) {
      list.innerHTML = items.map(function (t) { return "<li>" + t + "</li>"; }).join("");
      list.classList.toggle("alt", alt);
    };
    var render = function () {
      var set = SETS[index % SETS.length];
      var alt = index % 2 === 1;
      paint(oldList, set.old, alt);
      paint(newList, set.babyg, alt);
      index += 1;
    };
    render();
    window.setInterval(render, 7500);
  }
})();
