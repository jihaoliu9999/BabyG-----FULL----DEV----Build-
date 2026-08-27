"""Regression contracts for the logged-in mobile shell."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
APP_CSS = (ROOT / "app/static/css/app.css").read_text(encoding="utf-8")
BASE_TEMPLATE = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
BOT_JS = (ROOT / "app/static/js/bot.js").read_text(encoding="utf-8")
DISCOVER_JS = (ROOT / "app/static/js/discover.js").read_text(encoding="utf-8")
DM_BRIEFS_JS = (ROOT / "app/static/js/dm_briefs.js").read_text(encoding="utf-8")
DM_THREAD_JS = (ROOT / "app/static/js/dm_thread.js").read_text(encoding="utf-8")
DM_THREAD_TEMPLATE = (ROOT / "app/templates/creator/dm_thread.html").read_text(
    encoding="utf-8"
)
DASHBOARD_TEMPLATE = (ROOT / "app/templates/creator/dashboard.html").read_text(
    encoding="utf-8"
)
DISCOVER_TEMPLATE = (ROOT / "app/templates/creator/discover.html").read_text(
    encoding="utf-8"
)
MOTION_JS = (ROOT / "app/static/js/motion.js").read_text(encoding="utf-8")
BOOST_JS = (ROOT / "app/static/js/boost.js").read_text(encoding="utf-8")


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
    conn_search_rule = APP_CSS.split(".conn-search input,", 1)[1].split("}", 1)[0]
    opportunity_input_rule = APP_CSS.split(".op-new-field input,\n.op-new-field textarea {", 1)[
        1
    ].split("}", 1)[0]
    settings_input_rule = APP_CSS.split(
        ".is-creator-app .settings-clean-shell .settings-field input,", 1
    )[1].split("}", 1)[0]

    assert "font-size: 16px" in composer_rule
    assert "font-size: 16px" in conn_search_rule
    assert "font-size: 16px" in opportunity_input_rule
    assert "font-size: 16px" in settings_input_rule


def test_bot_composer_uses_single_visible_textbox() -> None:
    box_rule = APP_CSS.split(
        ".is-creator-app.is-chat .bot-composer .box,", 1
    )[1].split("}", 1)[0]
    textarea_rule = APP_CSS.split(
        ".is-creator-app.is-chat .bot-composer textarea,", 1
    )[1].split("}", 1)[0]
    send_rule = APP_CSS.split(
        ".is-creator-app.is-chat .bot-composer .send {", 1
    )[1].split("}", 1)[0]
    send_icon_rule = APP_CSS.split(
        ".is-creator-app.is-chat .bot-composer .send svg {", 1
    )[1].split("}", 1)[0]

    assert "background: transparent" in box_rule
    assert "border: 0" in box_rule
    assert "border-radius: 0" in box_rule
    assert "box-shadow: none" in box_rule
    assert "background: linear-gradient" in textarea_rule
    assert "border: 1px solid rgba(255,255,255,.12)" in textarea_rule
    assert "border-radius: 16px" in textarea_rule
    assert "font-size: 16px" in textarea_rule
    assert "min-width: 0" in textarea_rule
    assert "min-height: 48px" in textarea_rule
    assert "width: 48px" in send_rule
    assert "height: 48px" in send_rule
    assert "flex: 0 0 48px" in send_rule
    assert "place-items: center" in send_rule
    assert "transform: translateX(1px)" in send_icon_rule


def test_dm_composers_use_single_visible_textbox() -> None:
    legacy_box_rule = APP_CSS.split(
        ".is-creator-app .dm-composer .box {", 1
    )[1].split("}", 1)[0]
    legacy_input_rule = APP_CSS.split(
        ".is-creator-app .dm-screen .dm-composer input,", 1
    )[1].split("}", 1)[0]
    thread_box_rule = APP_CSS.split(".dm-thread-composer-box {", 1)[1].split(
        "}", 1
    )[0]
    thread_input_rule = APP_CSS.split(".dm-thread-composer-box input {", 1)[
        1
    ].split("}", 1)[0]
    thread_send_rule = APP_CSS.split(".dm-thread-composer-send {", 1)[1].split(
        "}", 1
    )[0]

    for box_rule in (legacy_box_rule, thread_box_rule):
        assert "background: transparent" in box_rule
        assert "border: 0" in box_rule
        assert "border-radius: 0" in box_rule
        assert "box-shadow: none" in box_rule

    for input_rule in (legacy_input_rule, thread_input_rule):
        assert "background: var(--surface-2)" in input_rule
        assert "border: 1px solid var(--hairline-strong)" in input_rule
        assert "border-radius: 16px" in input_rule
        assert "font-size: 16px" in input_rule
        assert "min-width: 0" in input_rule
        assert "min-height: 44px" in input_rule

    assert "width: 44px" in thread_send_rule
    assert "flex: 0 0 44px" in thread_send_rule


def test_bot_prompt_chips_start_prompt_on_tap() -> None:
    chip_handler = BOT_JS.split("// Suggested-prompt chips.", 1)[1].split(
        "// Inline chips under any bot message", 1
    )[0]

    # Composer v2 pattern: the chip strip re-renders every turn, so
    # the handler is now bound via delegation on the stable composer
    # element. Fresh chips inherit the handler without rebinding.
    # data-chip-submit="1" chips (verb chips off the pending-action
    # path) auto-submit; every other chip fills the composer and lets
    # the creator edit before send.
    assert "if (inFlight) return" in chip_handler
    assert "textarea.value = text" in chip_handler
    assert 'textarea.dispatchEvent(new Event("input", { bubbles: true }))' in chip_handler
    assert 'chip.getAttribute("data-chip-submit")' in chip_handler
    assert "composer.requestSubmit()" in chip_handler
    # No .remove() call: swapChipStrip in applyPartial replaces the
    # element, so the handler must never blow away its own binding.
    assert "chipsRow.remove()" not in chip_handler


def test_creator_dm_search_toolbar_is_mobile_safe() -> None:
    toolbar_rule = APP_CSS.split(".dm-inbox-topbar {", 1)[1].split("}", 1)[0]
    search_rule = APP_CSS.split(".dm-inbox-search {", 1)[1].split("}", 1)[0]
    input_rule = APP_CSS.split(
        ".is-creator-app .dm-inbox-search input,", 1
    )[1].split("}", 1)[0]
    action_rule = APP_CSS.split(".dm-inbox-compose {", 1)[1].split("}", 1)[0]

    assert "display: flex" in toolbar_rule
    assert "align-items: center" in toolbar_rule
    assert "gap: 10px" in toolbar_rule
    assert "flex: 1 1 0%" in search_rule
    assert "min-width: 0" in search_rule
    assert "background: transparent" in search_rule
    assert "border: 0" in search_rule
    assert "border-radius: 0" in search_rule
    assert "font-size: 16px" in input_rule
    assert "min-width: 0" in input_rule
    assert "padding: 0 14px 0 38px" in input_rule
    assert "background: var(--surface-1)" in input_rule
    assert "border: 1px solid var(--hairline)" in input_rule
    assert "border-radius: 999px" in input_rule
    assert "flex: 0 0 44px" in action_rule
    assert "min-width: 44px" in action_rule


def test_discover_filters_open_as_bounded_mobile_sheet() -> None:
    mobile_rule = APP_CSS.split(
        "@media (max-width: 1023px) {", 1
    )[1].split("@media (min-width: 1024px)", 1)[0]
    panel_rule = mobile_rule.split(".discover-filters {", 1)[1].split("}", 1)[0]
    close_rule = mobile_rule.split(".discover-filter-close {", 1)[1].split("}", 1)[0]
    actions_rule = mobile_rule.split(".discover-filter-actions {", 1)[1].split(
        "}", 1
    )[0]

    assert 'data-filter-close aria-label="close filters"' in DISCOVER_TEMPLATE
    assert 'name="category"' in DISCOVER_TEMPLATE
    assert 'name="location"' in DISCOVER_TEMPLATE
    assert 'name="budget_min"' in DISCOVER_TEMPLATE
    assert 'name="budget_max"' in DISCOVER_TEMPLATE
    assert 'root.querySelector("[data-filter-close]")' in DISCOVER_JS
    assert 'window.matchMedia("(max-width: 1023px)")' in DISCOVER_JS
    assert 'root.classList.toggle("is-filter-open", open)' in DISCOVER_JS
    assert 'panel.setAttribute("aria-hidden", String(!open))' in DISCOVER_JS
    assert 'panel.removeAttribute("aria-hidden")' in DISCOVER_JS
    assert "position: fixed" in panel_rule
    assert "bottom: calc(var(--tabbar-h)" in panel_rule
    assert "max-height: min(60dvh, 420px)" in panel_rule
    assert "overflow-y: auto" in panel_rule
    assert "z-index: 130" in panel_rule
    assert "width: 44px" in close_rule
    assert "height: 44px" in close_rule
    assert "position: sticky" in actions_rule


def test_creator_dm_pages_do_not_render_shared_profile_chrome() -> None:
    """DM inbox/thread screens own their mobile chrome. The shared
    creator badge/avatar should not stack above the search bar or the
    thread header."""
    assert (
        "{% set is_dm = is_creator and cp.startswith('/creator/dm') %}"
        in BASE_TEMPLATE
    )
    assert "{{ 'is-dm' if is_dm else '' }}" in BASE_TEMPLATE
    assert (
        BASE_TEMPLATE.count(
            "not cp.startswith('/creator/bot') and not is_dm"
        )
        == 2
    )
    assert ".is-creator-app.is-dm .mobile-header" in APP_CSS
    assert ".is-creator-app.is-dm .app-main > #view" in APP_CSS


def test_creator_tabbar_items_are_centered_on_mobile() -> None:
    rule = APP_CSS.split(".is-creator-app .creator-tabbar {", 1)[1].split("}", 1)[0]
    item_rule = APP_CSS.split(".is-creator-app .creator-tabbar a {", 1)[1].split(
        "}", 1
    )[0]

    assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in rule
    assert "repeat(6" not in rule
    assert "justify-items: stretch" in rule
    assert "width: 100%" in item_rule
    assert "max-width: none" in item_rule
    assert "letter-spacing: 0" in item_rule


def test_dm_thread_composer_sits_close_to_bottom_tabbar() -> None:
    thread_rule = APP_CSS.split(".is-creator-app .dm-thread {", 1)[1].split(
        "}", 1
    )[0]
    composer_rule = APP_CSS.split(".dm-thread-composer {", 1)[1].split("}", 1)[0]
    status_rule = APP_CSS.split(".dm-thread-composer-status {", 1)[1].split(
        "}", 1
    )[0]
    visible_status_rule = APP_CSS.split(
        ".dm-thread-composer-status.is-visible {", 1
    )[1].split("}", 1)[0]
    box_rule = APP_CSS.split(".dm-thread-composer-box {", 1)[1].split("}", 1)[0]
    input_rule = APP_CSS.split(".dm-thread-composer-box input {", 1)[1].split(
        "}", 1
    )[0]

    assert "height: 100%" in thread_rule
    assert "min-height: 100%" in thread_rule
    assert "padding-bottom: 0" in thread_rule
    assert "var(--tabbar-h)" not in thread_rule
    assert "+ 86px" not in thread_rule
    assert "padding: 8px 16px 8px" in composer_rule
    assert "height: 0" in status_rule
    assert "min-height: 0" in status_rule
    assert "overflow: hidden" in status_rule
    assert "height: 18px" in visible_status_rule
    assert "min-height: 44px" in box_rule
    assert "font-size: 16px" in input_rule
    assert "min-width: 0" in input_rule


def test_creator_settings_work_links_do_not_overlap_labels() -> None:
    rule = APP_CSS.split(
        ".is-creator-app .profile-fidelity-settings-card > a {", 1
    )[1].split("}", 1)[0]

    assert "grid-template-columns: minmax(64px, 88px) minmax(0, 1fr) auto" in rule
    assert "max-content" not in rule
    assert "34px minmax(0, 1fr)" not in rule


def test_creator_settings_work_links_use_uniform_bold_text() -> None:
    rule = APP_CSS.split(
        ".is-creator-app .settings-work-card .profile-fidelity-settings-card > a > .eyebrow,",
        1,
    )[1].split("}", 1)[0]

    assert "font-family: var(--sans)" in rule
    assert "font-size: 13px" in rule
    assert "font-weight: 650" in rule
    assert "letter-spacing: 0" in rule
    assert "text-transform: none" in rule


def test_creator_home_shortcuts_fit_mobile_labels() -> None:
    shortcut_rule = APP_CSS.split(
        ".is-creator-app .creator-home-shortcut {", 1
    )[1].split("}", 1)[0]
    shortcut_mobile_rule = APP_CSS.split(
        ".is-creator-app .creator-home-shortcuts { grid-template-columns: repeat(2, minmax(0, 1fr)) !important;",
        1,
    )[1].split("@media (max-width: 420px)", 1)[0]
    label_rule = APP_CSS.split(
        ".is-creator-app .creator-home-shortcut span {", 1
    )[1].split("}", 1)[0]

    assert "max-height" not in shortcut_rule
    assert "min-width: 0" in shortcut_rule
    assert (
        "grid-template-columns: repeat(2, minmax(0, 1fr)) !important"
        in APP_CSS
    )
    assert "min-height: 62px !important" in shortcut_mobile_rule
    assert "overflow-wrap: anywhere" in label_rule
    assert "<span>check dms</span>" in DASHBOARD_TEMPLATE
    assert "<span>ask babyg</span>" in DASHBOARD_TEMPLATE
    assert "<span>browse discover</span>" in DASHBOARD_TEMPLATE
    assert "<span>my connections</span>" in DASHBOARD_TEMPLATE


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


def test_dm_thread_keyboard_composer_docks_to_visual_viewport() -> None:
    """When the DM composer is focused, the tabbar is hidden and should
    not keep reserving height below the input. The thread uses the real
    visual viewport so the composer sits against the keyboard instead of
    floating above an empty band."""
    assert "--dm-visual-viewport-height" in DM_THREAD_JS
    assert "visualViewport.height + visualViewport.offsetTop" in DM_THREAD_JS
    assert "dm-keyboard-open" in DM_THREAD_JS

    keyboard_view_rule = APP_CSS.split(
        ".is-dm-thread.dm-keyboard-open #view {", 1
    )[1].split("}", 1)[0]
    keyboard_composer_rule = APP_CSS.split(
        ".is-dm-thread.dm-keyboard-open .dm-thread-composer {", 1
    )[1].split("}", 1)[0]
    tabbar_hide_rule = APP_CSS.split(
        "body.dm-keyboard-open .app-tabbar,", 1
    )[1].split("}", 1)[0]

    assert "var(--dm-visual-viewport-height, 100dvh)" in keyboard_view_rule
    assert "var(--tabbar-h)" not in keyboard_view_rule
    assert "padding-bottom: 2px" in keyboard_composer_rule
    assert "safe-area-inset-bottom" not in keyboard_composer_rule
    assert "body.dm-keyboard-open .creator-tabbar" in tabbar_hide_rule


def test_dm_composer_polish_keeps_controls_scoped_and_stable() -> None:
    """The composer status row stays hidden until JS toggles the
    is-visible class, without reserving idle space above the reply box.
    The send button disables on empty input. All of these must survive
    the DM thread redesign."""
    status_rule = APP_CSS.split(
        ".dm-thread-composer-status {", 1
    )[1].split("}", 1)[0]
    visible_status_rule = APP_CSS.split(
        ".dm-thread-composer-status.is-visible {", 1
    )[1].split("}", 1)[0]
    assert "min-height: 0" in status_rule
    assert "overflow: hidden" in status_rule
    assert "min-height: 18px" in visible_status_rule
    assert "visibility: hidden" in status_rule
    assert "data-dm-brief-status" in DM_THREAD_TEMPLATE
    assert '"babyg is reading"' in DM_BRIEFS_JS
    assert "babyg is reading…" not in DM_BRIEFS_JS

    assert "composerSend.disabled = !composerInput.value.trim()" in DM_BRIEFS_JS
    assert "data-dm-send" in DM_THREAD_TEMPLATE

    # The report form still renders BEFORE the composer in source order
    # so keyboard-open on mobile pushes the composer above the fold
    # without the report block getting in the way.
    report_position = DM_THREAD_TEMPLATE.index('class="dm-thread-report"')
    composer_position = DM_THREAD_TEMPLATE.index('class="dm-thread-composer"')
    assert report_position < composer_position


def test_babyg_guide_is_tap_friendly_and_replaces_old_dm_prompts() -> None:
    """The "ask babyg" refresh action lives inside the slim header's
    ⋯ menu (tap-friendly via the menu button, not a full-width button
    stealing space in the message list). Old inline prompt strings must
    stay gone so the redesign doesn't regress toward the busy layout."""
    menu_summary_rule = APP_CSS.split(
        ".dm-thread-menu > summary {", 1
    )[1].split("}", 1)[0]
    assert "44px" in menu_summary_rule  # tap target
    assert "data-brief-refresh" in DM_THREAD_TEMPLATE
    assert "ask babyg" in DM_THREAD_TEMPLATE
    assert "dm-brief-prompt" not in DM_THREAD_TEMPLATE
    assert "ask babyg about this message" not in DM_THREAD_TEMPLATE
    assert "ask babyg to re-check" not in DM_THREAD_TEMPLATE


# ---------------------------------------------------------------------------
# Nav-speed contracts — patch 2B
#
# These lock in the non-blocking-fonts + prefetch + watermark-priority
# pattern so a well-meaning template edit does not silently re-block
# first paint on every page.
# ---------------------------------------------------------------------------


def test_google_fonts_link_is_non_blocking() -> None:
    """Fonts CSS must ship with media=print + data-webfont-swap so it
    downloads without blocking first paint. boost.js flips it to
    media=all once JS parses; the noscript fallback keeps text styled
    for JS-disabled users."""
    assert "data-webfont-swap" in BASE_TEMPLATE
    assert 'media="print"' in BASE_TEMPLATE
    # Runtime promotion must exist AND must run BEFORE the creator-only
    # early-return so brand + operator + auth all benefit.
    assert "data-webfont-swap" in BOOST_JS
    early_return = BOOST_JS.index("is-creator-app")
    promotion = BOOST_JS.index("data-webfont-swap")
    assert promotion < early_return
    # Noscript fallback so JS-disabled users still get real fonts.
    assert "<noscript>" in BASE_TEMPLATE


def test_role_shells_prefetch_top_nav_destinations() -> None:
    """Each role's shell prefetches its top nav destinations so the
    first click after landing is close to instant."""
    for path in (
        "/creator/discover",
        "/creator/bot",
        "/creator/dm",
        "/creator/profile/settings",
        "/brand/discover",
        "/brand/profile",
        "/operator",
    ):
        assert f'rel="prefetch" href="{path}"' in BASE_TEMPLATE, (
            f"missing prefetch link for {path}"
        )


def test_watermark_imgs_use_low_priority_async_decode() -> None:
    """The 273 KB logo watermarks are decorative — they must not block
    first paint or contend with above-the-fold decodes."""
    watermark_lines = [
        line for line in BASE_TEMPLATE.splitlines()
        if 'class="app-bg-mark' in line
    ]
    # Six in the creator shell + four in the brand shell = ten total.
    assert len(watermark_lines) == 10
    for line in watermark_lines:
        assert 'decoding="async"' in line, line
        assert 'fetchpriority="low"' in line, line


def test_mobile_secondary_actions_are_tap_friendly() -> None:
    """Frequent mobile actions should not collapse into tiny controls."""
    dm_inbox_chip_rule = APP_CSS.split(".dm-inbox-chip {", 1)[1].split("}", 1)[0]
    dm_thread_chip_rule = APP_CSS.split(".dm-thread-chip {", 1)[1].split("}", 1)[0]
    bot_chip_rule = APP_CSS.split(".bot-prompt-chip {", 1)[1].split("}", 1)[0]
    thread_back_rule = APP_CSS.split(
        ".dm-thread-back,\n.dm-thread-menu > summary {", 1
    )[1].split("}", 1)[0]
    conn_button_rule = APP_CSS.split(".conn-btn {", 1)[1].split("}", 1)[0]
    conn_icon_rule = APP_CSS.split(".conn-btn.icon {", 1)[1].split("}", 1)[0]
    legacy_send_rule = APP_CSS.split(".dm-screen .dm-composer .send {", 1)[
        1
    ].split("}", 1)[0]
    home_link_rule = APP_CSS.split(".is-creator-app .creator-home-link {", 1)[
        1
    ].split("}", 1)[0]
    needs_chip_rule = APP_CSS.split(".is-creator-app .creator-needs-chip {", 1)[
        1
    ].split("}", 1)[0]
    section_link_rule = APP_CSS.split(
        ".is-creator-app .creator-section-head > a {", 1
    )[1].split("}", 1)[0]

    assert "min-height: 40px" in dm_inbox_chip_rule
    assert "min-height: 40px" in dm_thread_chip_rule
    assert "min-height: 44px" in bot_chip_rule
    assert "width: 44px" in thread_back_rule
    assert "height: 44px" in thread_back_rule
    assert "min-height: 44px" in conn_button_rule
    assert "width: 44px" in conn_icon_rule
    assert "height: 44px" in conn_icon_rule
    assert "width: 44px" in legacy_send_rule
    assert "flex: 0 0 44px" in legacy_send_rule
    assert "min-height: 44px" in home_link_rule
    assert "min-height: 40px" in needs_chip_rule
    assert "min-height: 44px" in section_link_rule


def test_creator_mobile_typography_is_not_visually_squeezed() -> None:
    """Core creator app labels use normal tracking on mobile."""
    checked_selectors = (
        ".is-creator-app .creator-tabbar a {",
        ".is-creator-app .discover-head h1 {",
        ".is-creator-app .creator-screen-title h1 {",
        ".is-creator-app .creator-home-hero h1 {",
        ".is-creator-app .profile-fidelity-title h1 {",
        ".is-creator-app .profile-fidelity-copy h2 {",
        ".dm-inbox-name {",
        ".bot-hero h1 {",
        ".conn-page-head h1 {",
        ".conn-name {",
    )

    for selector in checked_selectors:
        rule = APP_CSS.split(selector, 1)[1].split("}", 1)[0]
        assert "letter-spacing: 0" in rule
        assert "letter-spacing: -" not in rule
        assert "font-size: clamp" not in rule
