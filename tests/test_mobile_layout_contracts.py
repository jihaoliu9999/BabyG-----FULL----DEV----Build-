"""Regression contracts for the logged-in mobile shell."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
APP_CSS = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
BOT_JS = (ROOT / "app/static/js/bot.js").read_text(encoding="utf-8")
DM_BRIEFS_JS = (ROOT / "app/static/js/dm_briefs.js").read_text(encoding="utf-8")
DM_THREAD_TEMPLATE = (ROOT / "app/templates/creator/dm_thread.html").read_text(
    encoding="utf-8"
)
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


def test_visual_viewport_height_includes_offset_top() -> None:
    """When iOS auto-scrolls on input focus, ``visualViewport.offsetTop``
    becomes the scroll amount. Body is ``position: fixed; inset: 0`` so
    it stays at the layout-viewport origin — ``#view``'s height has to
    include offsetTop so the composer reaches the real bottom of the
    visible area instead of stranding mid-screen above a black gap."""
    update_fn = BOT_JS.split("function updateKeyboardInset()", 1)[1].split(
        "if (window.visualViewport)", 1
    )[0]
    assert "vv.height + vv.offsetTop" in update_fn


def test_tabbar_extends_dark_backdrop_below_bottom_edge() -> None:
    """iOS Safari's bottom URL bar (non-standalone) sits below the
    tabbar; without an extending background, the collapsed URL bar +
    safe-area combination looks like a stray gap. A box-shadow below
    the tabbar paints a solid dark continuation without affecting
    layout. Locate the main `.app-tabbar` declaration (the standalone
    selector, not `.is-marketing .app-tabbar`) and assert the skirt
    is present inside it."""
    rule = APP_CSS.split("\n.app-tabbar {", 1)[1].split("}", 1)[0]
    assert "box-shadow: 0 200px 0 0" in rule


def test_in_app_shell_background_is_darker_than_tabbar_blur() -> None:
    """The body background visible below the tabbar must be at least as
    dark as the tabbar's blurred tone (~rgb(8,8,8)). Otherwise the body
    shows through as a brighter band and reads as a stray gap during
    tab navigation. In-app shells use --jet (#050505) so any visible
    area below the tabbar recedes instead of contrasting."""
    assert ".app-shell:not(.is-marketing)," in APP_CSS
    assert ".operator-shell { background: var(--jet); }" in APP_CSS


def test_chat_keyboard_composer_drops_safe_area_padding() -> None:
    """When the keyboard is open, the iOS home-indicator safe-area is
    already covered by the keyboard, so the composer must collapse its
    bottom padding to a hairline. Anything bigger reads as the gap the
    screenshot showed."""
    keyboard_open_rule = APP_CSS.split(
        ".is-chat.chat-keyboard-open .bot-composer {", 1
    )[1].split("}", 1)[0]
    assert "padding-bottom: 2px" in keyboard_open_rule
    # No safe-area-inset-bottom involvement when keyboard is up.
    assert "safe-area-inset-bottom" not in keyboard_open_rule


def test_dm_composer_polish_keeps_controls_scoped_and_stable() -> None:
    status_rule = APP_CSS.split(
        ".is-creator-app .dm-composer-status {", 1
    )[1].split("}", 1)[0]
    assert "min-height: 18px" in status_rule
    assert "visibility: hidden" in status_rule
    assert "data-dm-brief-status" in DM_THREAD_TEMPLATE
    assert '"babyg is reading"' in DM_BRIEFS_JS
    assert "babyg is reading…" not in DM_BRIEFS_JS

    assert "composerSend.disabled = !composerInput.value.trim()" in DM_BRIEFS_JS
    assert "data-dm-send" in DM_THREAD_TEMPLATE
    assert ".is-creator-app.is-dm-thread #view" in APP_CSS
    assert "var(--tabbar-h) - 12px" in APP_CSS

    report_position = DM_THREAD_TEMPLATE.index('class="dm-thread-report"')
    body_end = DM_THREAD_TEMPLATE.index("  </div>\n\n  {% set high_risk")
    composer_position = DM_THREAD_TEMPLATE.index('class="dm-composer"')
    assert report_position < body_end < composer_position
