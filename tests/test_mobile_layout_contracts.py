"""Regression contracts for the logged-in mobile shell."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
APP_CSS = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
BOT_JS = (ROOT / "app/static/js/bot.js").read_text(encoding="utf-8")
MOTION_JS = (ROOT / "app/static/js/motion.js").read_text(encoding="utf-8")


def test_skip_link_is_hidden_until_focused() -> None:
    skip_rule = APP_CSS.split(".skip-link {", 1)[1].split("}", 1)[0]
    assert "left: -10000px" in skip_rule
    assert "z-index: -1" in skip_rule
    assert ".skip-link:focus-visible" in APP_CSS


def test_chat_uses_actual_visual_viewport_height() -> None:
    assert "--visual-viewport-height" in BOT_JS
    assert "var(--visual-viewport-height, 100dvh)" in APP_CSS
    assert "54px - env(safe-area-inset-top" not in APP_CSS


def test_message_pinning_does_not_scroll_the_document() -> None:
    pin_function = MOTION_JS.split("function pinToLatest()", 1)[1].split(
        "function bindAutogrow()", 1
    )[0]
    assert "list.scrollTop = list.scrollHeight" in pin_function
    assert "scrollIntoView" not in pin_function.replace(
        "scrollIntoView() can move", ""
    )


def test_mobile_controls_keep_ios_safe_font_size() -> None:
    assert "textarea, select) { font-size: 16px; }" in APP_CSS
    assert ".bot-composer textarea" in APP_CSS
    composer_rule = APP_CSS.split(".bot-composer textarea {", 1)[1].split("}", 1)[0]
    assert "font-size: 16px" in composer_rule


def test_hidden_brand_topbar_does_not_reserve_mobile_space() -> None:
    assert ".is-brand-shell .brand-main" in APP_CSS
    assert "padding-top: max(18px, env(safe-area-inset-top, 0px))" in APP_CSS
