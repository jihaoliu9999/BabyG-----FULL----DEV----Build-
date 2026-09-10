/* Home v5 primary-card carousel.
 *
 * Progressive enhancement: the underlying markup is a CSS scroll-snap
 * track, so if this JS never runs the box still works (swipe/drag
 * scroll gives you the same slide behavior, just without live dot
 * updates). This script upgrades it in three ways:
 *
 *   1. Live-update the active dot + "N / M" count as the track scrolls.
 *   2. Click a dot to jump to that slide.
 *   3. Left/right arrow keys navigate when the carousel has focus.
 *
 * Deliberately no touch-event handlers — the native scroll-snap
 * behavior is smoother than any JS-driven pointermove logic, and
 * matches iOS/Android system carousels. Trackpad horizontal swipe
 * already works via the same native scroll path on macOS/ChromeOS.
 *
 * No external deps.
 */
(function () {
  'use strict';

  function attach(root) {
    var track = root.querySelector('[data-hv5-track]');
    if (!track) return;
    var slideCount = parseInt(root.getAttribute('data-hv5-carousel') || '1', 10);
    if (slideCount < 2) return;

    var dots = Array.prototype.slice.call(
      root.querySelectorAll('.hv5-primary-dot')
    );
    var counter = root.querySelector('[data-hv5-current]');

    function updateActive() {
      // Which slide is closest to the visible left edge?
      var trackRect = track.getBoundingClientRect();
      var slides = track.children;
      var best = 0;
      var bestDelta = Infinity;
      for (var i = 0; i < slides.length; i++) {
        var slide = slides[i];
        var delta = Math.abs(slide.getBoundingClientRect().left - trackRect.left);
        if (delta < bestDelta) {
          bestDelta = delta;
          best = i;
        }
      }
      for (var d = 0; d < dots.length; d++) {
        dots[d].classList.toggle('is-active', d === best);
      }
      if (counter) counter.textContent = String(best + 1);
    }

    // Scroll listener: rAF-throttled so touch scrolling stays smooth.
    var ticking = false;
    track.addEventListener('scroll', function () {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () {
        updateActive();
        ticking = false;
      });
    }, { passive: true });

    // Dot click -> jump.
    dots.forEach(function (dot) {
      dot.style.pointerEvents = 'auto';
      dot.style.cursor = 'pointer';
      dot.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        var idx = parseInt(dot.getAttribute('data-dot-index') || '0', 10);
        var target = track.children[idx];
        if (!target) return;
        track.scrollTo({
          left: target.offsetLeft - track.offsetLeft,
          behavior: 'smooth',
        });
      });
    });

    // Keyboard arrows.
    root.tabIndex = 0;
    root.addEventListener('keydown', function (e) {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      var current = 0;
      for (var d = 0; d < dots.length; d++) {
        if (dots[d].classList.contains('is-active')) { current = d; break; }
      }
      var next = current + (e.key === 'ArrowRight' ? 1 : -1);
      next = Math.max(0, Math.min(dots.length - 1, next));
      var target = track.children[next];
      if (target) {
        e.preventDefault();
        track.scrollTo({
          left: target.offsetLeft - track.offsetLeft,
          behavior: 'smooth',
        });
      }
    });

    // Prime the dot state in case the page loaded scrolled.
    updateActive();
  }

  function init() {
    var roots = document.querySelectorAll('[data-hv5-carousel]');
    for (var i = 0; i < roots.length; i++) attach(roots[i]);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
