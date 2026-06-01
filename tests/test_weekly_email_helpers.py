"""Tests for pure helper functions in the weekly-email view.

These cover the reply-guidance footer and the anti-repetition block — both
are pure string builders with no Qt/DB dependency.
"""


def test_reply_guidance_mentions_company_average_and_privacy() -> None:
    from app.ui.views.weekly_email_view import _reply_guidance_html

    html = _reply_guidance_html()
    # Invites a reply.
    assert "Reply" in html
    # Steers reps toward benchmark questions.
    assert "company average" in html
    # States the privacy boundary: own territory + averages, not other reps.
    assert "another rep" in html
    # Mentions the future-email preference channel.
    assert "future emails" in html


def test_recent_emails_block_uses_90_day_window() -> None:
    from app.ui.views.weekly_email_view import _recent_emails_block

    block = _recent_emails_block(["Last month you grew ABC Flooring."])
    assert "last ~90 days" in block
    # Empty history yields no block.
    assert _recent_emails_block(None) == ""
    assert _recent_emails_block([]) == ""


def test_recent_emails_block_truncates_long_bodies() -> None:
    from app.ui.views.weekly_email_view import _recent_emails_block

    long_body = "x" * 2000
    block = _recent_emails_block([long_body])
    assert " …" in block
    # Only the excerpt (900 chars) plus the ellipsis is included, not all 2000.
    assert block.count("x") <= 905
