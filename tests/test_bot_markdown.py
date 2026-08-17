"""The bot_markdown filter lets Claude emit paragraphs, bullet lists,
and bold — nothing else. Anything that could be executable HTML must
be escaped before we wrap tags around it, even in an LLM output the
prompt has hardened."""

from __future__ import annotations

from app.core.templating import _bot_markdown


def test_returns_empty_markup_on_none_or_empty() -> None:
    assert str(_bot_markdown(None)) == ""
    assert str(_bot_markdown("")) == ""
    assert str(_bot_markdown("   \n\n  ")) == ""


def test_single_line_wraps_in_paragraph() -> None:
    assert str(_bot_markdown("hello")) == "<p>hello</p>"


def test_double_newline_starts_new_paragraph() -> None:
    out = str(_bot_markdown("first line.\n\nsecond line."))
    assert out == "<p>first line.</p><p>second line.</p>"


def test_single_newline_becomes_br_within_paragraph() -> None:
    out = str(_bot_markdown("line one\nline two"))
    assert out == "<p>line one<br>line two</p>"


def test_bullet_list_of_dashes() -> None:
    src = "- one\n- two\n- three"
    assert str(_bot_markdown(src)) == "<ul><li>one</li><li>two</li><li>three</li></ul>"


def test_bullet_list_of_stars_also_works() -> None:
    assert "<ul>" in str(_bot_markdown("* one\n* two"))


def test_bold_asterisks_render_as_strong() -> None:
    out = str(_bot_markdown("that **matters** now"))
    assert out == "<p>that <strong>matters</strong> now</p>"


def test_bold_inside_bullets() -> None:
    src = "- **name:** value\n- **other:** thing"
    out = str(_bot_markdown(src))
    assert "<strong>name:</strong>" in out
    assert "<strong>other:</strong>" in out


def test_html_in_source_is_escaped() -> None:
    """The single most important test in this file. LLMs love angle
    brackets. If they slip a raw tag through, it must render as text."""
    out = str(_bot_markdown("hey <script>alert(1)</script> ok"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_html_inside_bullet_is_also_escaped() -> None:
    out = str(_bot_markdown("- <img onerror=x>\n- ok"))
    assert "<img" not in out.replace("<img onerror=x>", "")
    assert "&lt;img" in out


def test_bold_across_mixed_content() -> None:
    src = "Rich. he wants to meet.\n\n- **jihao** — recent\n- ruslan — cold"
    out = str(_bot_markdown(src))
    assert "<p>Rich. he wants to meet.</p>" in out
    assert "<ul><li><strong>jihao</strong> — recent</li>" in out


def test_mixed_paragraph_and_list_blocks() -> None:
    src = "intro paragraph\n\n- one\n- two\n\nclosing sentence."
    out = str(_bot_markdown(src))
    assert out == (
        "<p>intro paragraph</p><ul><li>one</li><li>two</li></ul>"
        "<p>closing sentence.</p>"
    )


def test_partial_bullets_do_not_become_list() -> None:
    """If a block mixes bullets and non-bullet lines it's not a list —
    render as a paragraph so we don't strip real content."""
    src = "- one\nnot a bullet"
    out = str(_bot_markdown(src))
    assert "<ul>" not in out
    assert "<p>" in out
