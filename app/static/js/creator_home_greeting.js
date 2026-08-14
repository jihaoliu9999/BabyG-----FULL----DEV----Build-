// Swap the creator-home greeting to match the viewer's local hour.
// The server renders four candidates (one per time slot) on the h1's
// data attributes and defaults the text to the afternoon variant.
// This script picks which one to display so a Miami creator doesn't
// see "good morning" at 8pm because the server is on UTC.
//
// Slot boundaries (viewer-local hours):
//   morning   05:00 – 11:59
//   afternoon 12:00 – 16:59
//   evening   17:00 – 20:59
//   night     21:00 – 04:59
(function () {
  var el = document.querySelector('[data-greeting-slots]');
  if (!el) return;
  var h = new Date().getHours();
  var slot;
  if (h >= 5 && h < 12) slot = 'morning';
  else if (h >= 12 && h < 17) slot = 'afternoon';
  else if (h >= 17 && h < 21) slot = 'evening';
  else slot = 'night';
  var text = el.dataset['greet' + slot.charAt(0).toUpperCase() + slot.slice(1)];
  if (text) el.textContent = text;
})();
