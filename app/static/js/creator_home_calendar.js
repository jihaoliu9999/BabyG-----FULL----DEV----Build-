/* Home v5 mobile calendar day picker.
 *
 * Progressive enhancement: without JS, each day cell is a real anchor
 * pointing at /creator/calendar?view=day&date=<iso>, so navigation
 * still works. This script intercepts taps on the day strip at
 * mobile widths to swap the visible per-day event list in place
 * without leaving Home.
 *
 * Bug history: an earlier delegation-based version failed silently on
 * mobile Safari because e.target for a tap on the inner <strong>7</strong>
 * was the <strong>, and the ancestor-walk to find the anchor was
 * fragile. This rewrite binds a click handler to each cell directly
 * and uses closest() as a belt-and-suspenders lookup so the handler
 * fires whether the tap lands on the anchor, its <span>, or its
 * <strong>. Also cancels the anchor's default navigation with both
 * preventDefault + stopPropagation so mobile Safari's tap-to-nav
 * short-circuit cannot beat us to it.
 */
(function () {
  'use strict';

  var MOBILE_MAX = 767;

  function isMobile() {
    try { return window.matchMedia('(max-width: ' + MOBILE_MAX + 'px)').matches; }
    catch (_e) { return false; }
  }

  function attach(strip, lists) {
    var cells = strip.querySelectorAll('[data-home-day]');
    var listNodes = lists.querySelectorAll('[data-home-day-list]');
    if (!cells.length || !listNodes.length) return;

    function select(iso) {
      var i;
      for (i = 0; i < cells.length; i++) {
        var matches = cells[i].getAttribute('data-home-day') === iso;
        if (matches) cells[i].classList.add('is-selected');
        else cells[i].classList.remove('is-selected');
      }
      for (i = 0; i < listNodes.length; i++) {
        listNodes[i].hidden =
          listNodes[i].getAttribute('data-home-day-list') !== iso;
      }
    }

    function bindCell(cell) {
      cell.addEventListener('click', function (e) {
        if (!isMobile()) return;  // desktop keeps native link nav
        // Belt-and-suspenders: closest() handles a tap on any inner
        // <span>/<strong> that bubbled the click up through the
        // anchor's shadow content.
        var t = e.target && e.target.closest
          ? e.target.closest('[data-home-day]')
          : cell;
        var iso = (t || cell).getAttribute('data-home-day');
        if (!iso) return;
        e.preventDefault();
        e.stopPropagation();
        select(iso);
      });
    }

    for (var k = 0; k < cells.length; k++) bindCell(cells[k]);
  }

  function init() {
    var strip = document.querySelector('[data-home-day-strip]');
    var lists = document.querySelector('[data-home-day-events]');
    if (!strip || !lists) return;
    attach(strip, lists);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
