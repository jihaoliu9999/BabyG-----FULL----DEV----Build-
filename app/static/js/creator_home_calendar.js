/* Home v5 mobile calendar day picker.
 *
 * Progressive enhancement: without JS, each day cell is a real anchor
 * pointing at /creator/calendar?view=day&date=<iso>, so navigation
 * still works. This script intercepts taps on the day strip at
 * mobile widths to swap the visible per-day event list in place
 * without leaving Home.
 *
 * No external deps. No touch handlers — we let the browser's default
 * tap semantics fire naturally.
 */
(function () {
  'use strict';

  var MOBILE_MAX = 767;

  function isMobile() {
    try { return window.matchMedia('(max-width: ' + MOBILE_MAX + 'px)').matches; }
    catch (_e) { return false; }
  }

  function init() {
    var strip = document.querySelector('[data-home-day-strip]');
    var lists = document.querySelector('[data-home-day-events]');
    if (!strip || !lists) return;

    strip.addEventListener('click', function (e) {
      // Desktop keeps the native link behavior.
      if (!isMobile()) return;
      var target = e.target;
      while (target && target !== strip && target.getAttribute('data-home-day') == null) {
        target = target.parentNode;
      }
      if (!target || target === strip) return;
      var iso = target.getAttribute('data-home-day');
      if (!iso) return;
      e.preventDefault();

      // Update visual selection on the strip.
      var cells = strip.querySelectorAll('[data-home-day]');
      for (var i = 0; i < cells.length; i++) {
        var cell = cells[i];
        var matches = cell.getAttribute('data-home-day') === iso;
        if (matches) cell.classList.add('is-selected');
        else cell.classList.remove('is-selected');
      }

      // Swap the visible per-day list.
      var allLists = lists.querySelectorAll('[data-home-day-list]');
      for (var j = 0; j < allLists.length; j++) {
        var list = allLists[j];
        list.hidden = list.getAttribute('data-home-day-list') !== iso;
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
