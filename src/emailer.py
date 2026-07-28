"""Send the daily briefing via Resend.

Free tier sends from `onboarding@resend.dev`. Verify a domain to send from
your own address (not required).
"""

from __future__ import annotations

import logging
import os
from datetime import date

import resend

log = logging.getLogger(__name__)


def send_briefing(body: str, subject: str | None = None) -> dict:
    """Send the briefing as a plain-text email.

    Args:
        body: the briefing text from coach.generate_briefing().
        subject: optional override; default is "Workout — {today}".

    Returns:
        Resend's response dict (contains the email `id`).
    """
    api_key = os.environ.get("RESEND_API_KEY")
    to_addr = os.environ.get("EMAIL_TO")
    from_addr = os.environ.get(
        "EMAIL_FROM", "Workout Coach <onboarding@resend.dev>"
    )

    if not api_key:
        raise RuntimeError("RESEND_API_KEY not set")
    if not to_addr:
        raise RuntimeError("EMAIL_TO not set")

    resend.api_key = api_key

    if subject is None:
        subject = f"Workout — {date.today().strftime('%a %b %d')}"

    # Convert plain text to a minimal HTML body so it renders cleanly on phones.
    # Wrap in <pre> so the ASCII section dividers (`-----`) don't get mangled.
    html_body = (
        "<div style=\"font-family: -apple-system, BlinkMacSystemFont, "
        "'Segoe UI', sans-serif; max-width: 600px; padding: 16px;\">"
        f"<pre style=\"white-space: pre-wrap; font-family: inherit; "
        f"font-size: 15px; line-height: 1.5;\">{_html_escape(body)}</pre>"
        "</div>"
    )

    params = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "text": body,
        "html": html_body,
    }

    log.info("Sending briefing to %s via Resend", to_addr)
    response = resend.Emails.send(params)
    log.info("Resend response: %s", response)
    return response


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
