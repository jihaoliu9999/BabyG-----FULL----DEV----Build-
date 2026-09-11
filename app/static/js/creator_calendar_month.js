/* Dedicated /creator/calendar month-only bottom sheet.
 *
 * This file is loaded ONLY from creator/calendar_list.html. The Home
 * calendar preview uses a separate JS module (creator_home_calendar.js)
 * with its own selectors; touching this file must never affect Home.
 *
 * Contract:
 *   - Tap a day cell ([data-cal-day]) -> open the day-detail sheet
 *     ([data-cal-sheet="day"]), populated from the JSON payload
 *     embedded at #calendar-month-events-data.
 *   - Tap `+ add` inside the day sheet -> swap to the add-event sheet
 *     ([data-cal-sheet="add"]) with the date field prefilled to the
 *     currently-selected day.
 *   - Backdrop click, close button, and ESC close whichever sheet is open.
 *   - No page navigation, no full reload — the month grid stays visible
 *     behind the sheet.
 */
(function () {
  'use strict';

  function readEvents() {
    var node = document.getElementById('calendar-month-events-data');
    if (!node) return {};
    try { return JSON.parse(node.textContent || '{}'); }
    catch (_e) { return {}; }
  }

  function humanDate(iso) {
    // 2026-09-11 -> Friday, September 11
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
    if (!m) return String(iso || '');
    var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    var weekdays = [
      'Sunday', 'Monday', 'Tuesday', 'Wednesday',
      'Thursday', 'Friday', 'Saturday'
    ];
    var months = [
      'January', 'February', 'March', 'April', 'May', 'June',
      'July', 'August', 'September', 'October', 'November', 'December'
    ];
    return weekdays[d.getDay()] + ', ' + months[d.getMonth()] + ' ' + d.getDate();
  }

  function markSelected(grid, iso) {
    if (!grid) return;
    var cells = grid.querySelectorAll('[data-cal-day]');
    for (var i = 0; i < cells.length; i++) {
      var match = cells[i].getAttribute('data-cal-day') === iso;
      cells[i].classList.toggle('is-selected', match);
    }
  }

  function openSheet(name) {
    var sheets = document.querySelectorAll('[data-cal-sheet]');
    for (var i = 0; i < sheets.length; i++) {
      var isTarget = sheets[i].getAttribute('data-cal-sheet') === name;
      if (isTarget) {
        sheets[i].hidden = false;
        sheets[i].setAttribute('aria-hidden', 'false');
        sheets[i].classList.add('is-open');
      } else {
        sheets[i].hidden = true;
        sheets[i].setAttribute('aria-hidden', 'true');
        sheets[i].classList.remove('is-open');
      }
    }
    try { document.body.classList.add('calendar-sheet-open'); } catch (_e) {}
  }

  function closeAllSheets() {
    var sheets = document.querySelectorAll('[data-cal-sheet]');
    for (var i = 0; i < sheets.length; i++) {
      sheets[i].hidden = true;
      sheets[i].setAttribute('aria-hidden', 'true');
      sheets[i].classList.remove('is-open');
    }
    try { document.body.classList.remove('calendar-sheet-open'); } catch (_e) {}
  }

  function renderDaySheet(iso, events) {
    var titleEl = document.querySelector('[data-cal-sheet-title]');
    var listEl = document.querySelector('[data-cal-sheet-list]');
    if (titleEl) titleEl.textContent = humanDate(iso);
    if (!listEl) return;
    listEl.innerHTML = '';
    var items = events[iso] || [];
    if (!items.length) {
      var empty = document.createElement('li');
      empty.className = 'calendar-sheet-empty';
      empty.textContent = 'nothing scheduled';
      listEl.appendChild(empty);
      return;
    }
    for (var i = 0; i < items.length; i++) {
      var it = items[i] || {};
      var li = document.createElement('li');
      li.className = 'calendar-sheet-event';
      var timeSpan = document.createElement('span');
      timeSpan.className = 'calendar-sheet-event-time';
      timeSpan.textContent = it.is_all_day
        ? 'all day'
        : (it.time_label || '');
      var titleSpan = document.createElement('span');
      titleSpan.className = 'calendar-sheet-event-title';
      titleSpan.textContent = it.title || 'untitled event';
      li.appendChild(timeSpan);
      li.appendChild(titleSpan);
      listEl.appendChild(li);
    }
  }

  function prepareAddSheet(iso) {
    var dateInput = document.querySelector('[data-cal-sheet-date]');
    if (dateInput && iso) dateInput.value = iso;
    var allDay = document.querySelector('[data-cal-sheet-allday]');
    var timeField = document.querySelector('[data-cal-sheet-time-field]');
    var timeInput = document.querySelector('[data-cal-sheet-time]');
    function syncAllDay() {
      if (!allDay) return;
      var isAllDay = !!allDay.checked;
      if (timeField) timeField.style.display = isAllDay ? 'none' : '';
      if (timeInput) timeInput.disabled = isAllDay;
    }
    if (allDay && !allDay.dataset.wired) {
      allDay.addEventListener('change', syncAllDay);
      allDay.dataset.wired = '1';
    }
    syncAllDay();
  }

  function init() {
    var grid = document.querySelector('[data-cal-month-grid]');
    if (!grid) return;
    var events = readEvents();
    var currentIso = null;

    grid.addEventListener('click', function (e) {
      var target = e.target && e.target.closest
        ? e.target.closest('[data-cal-day]')
        : null;
      if (!target) return;
      e.preventDefault();
      var iso = target.getAttribute('data-cal-day');
      if (!iso) return;
      currentIso = iso;
      markSelected(grid, iso);
      renderDaySheet(iso, events);
      openSheet('day');
    });

    document.addEventListener('click', function (e) {
      if (e.target && e.target.matches('[data-cal-sheet-close]')) {
        e.preventDefault();
        closeAllSheets();
      }
      if (e.target && e.target.matches('[data-cal-sheet-add]')) {
        e.preventDefault();
        prepareAddSheet(currentIso);
        openSheet('add');
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeAllSheets();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
