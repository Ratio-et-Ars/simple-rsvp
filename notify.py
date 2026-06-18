"""Best-effort RSVP notifications.

When someone RSVPs, fire a short message to whichever channels are configured
via environment variables. Everything here is *best-effort* and runs on a
daemon thread, so a slow or broken endpoint never delays the guest's
confirmation page or breaks the RSVP. Stdlib only — no new dependencies.

Channels (set the env var to enable; leave unset to disable):

- **Discord** — ``DISCORD_WEBHOOK_URL``: a channel webhook URL. We POST a
  ``{"content": ...}`` message to it.
- **Email** — ``SMTP_HOST`` plus ``NOTIFY_EMAIL`` (the recipient). Optional:
  ``SMTP_PORT`` (default 587), ``SMTP_USER``, ``SMTP_PASSWORD``, ``SMTP_FROM``,
  and ``SMTP_STARTTLS`` (set to ``0`` to disable STARTTLS).

Adding another HTTP-POST channel (e.g. ntfy.sh, Slack) is a few lines in
``_dispatch``.
"""

import json
import os
import smtplib
import threading
import urllib.request
from email.message import EmailMessage


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _plural(n, singular, plural=None):
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _compose(title, name, adults, kids, notes, total, updated):
    """Build the plain-text notification body (shared by all channels)."""
    action = "updated their RSVP for" if updated else "RSVP'd to"
    lines = [
        f"{name} {action} {title}.",
        f"Party: {_plural(adults, 'adult')}, {_plural(kids, 'kid')}.",
    ]
    if notes:
        lines.append(f"Note: {notes}")
    lines.append(f"{_plural(total, 'guest')} total now.")
    return "\n".join(lines)


def notify_rsvp(*, title, name, adults, kids, notes, total, updated):
    """Send RSVP notifications on a background thread.

    Called from within the request (so it can be handed plain values); the
    actual network/SMTP work happens off-thread and swallows all errors.
    """
    if not (_env("DISCORD_WEBHOOK_URL") or _env("SMTP_HOST")):
        return  # nothing configured — skip the thread entirely
    body = _compose(title, name, adults, kids, notes, total, updated)
    subject = f"New RSVP: {name} → {title}"
    threading.Thread(
        target=_dispatch, args=(subject, body), daemon=True,
    ).start()


def _dispatch(subject, body):
    if _env("DISCORD_WEBHOOK_URL"):
        _send_discord(body)
    if _env("SMTP_HOST") and _env("NOTIFY_EMAIL"):
        _send_email(subject, body)


def _send_discord(body):
    # Discord caps content at 2000 chars; our inputs are already short, but be safe.
    payload = json.dumps({"content": ("\U0001F389 " + body)[:2000]}).encode()
    req = urllib.request.Request(
        _env("DISCORD_WEBHOOK_URL"), data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "simple-rsvp"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # best-effort: never surface a notification failure to the guest


def _send_email(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _env("SMTP_FROM") or _env("SMTP_USER") or "rsvp@localhost"
    msg["To"] = _env("NOTIFY_EMAIL")
    msg.set_content(body)
    port = int(_env("SMTP_PORT") or 587)
    try:
        with smtplib.SMTP(_env("SMTP_HOST"), port, timeout=15) as s:
            if _env("SMTP_STARTTLS", "1").lower() not in ("0", "false", "no"):
                s.starttls()
            user, password = _env("SMTP_USER"), _env("SMTP_PASSWORD")
            if user and password:
                s.login(user, password)
            s.send_message(msg)
    except Exception:
        pass  # best-effort
