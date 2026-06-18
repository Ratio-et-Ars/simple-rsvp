"""Best-effort RSVP notifications.

When someone RSVPs, fire a short message to whichever channels are configured
*and* enabled. Everything here is *best-effort* and runs on a daemon thread, so
a slow or broken endpoint never delays the guest's confirmation page or breaks
the RSVP. Stdlib only — no new dependencies.

**Secrets live in environment variables, never in the database.** The admin
Settings page can only switch a configured channel on or off (that toggle is the
one non-secret bit, stored in the ``settings`` table); the endpoints and
credentials below are read from the environment at send time.

Channels (set the env var to configure):

- **Discord** — ``DISCORD_WEBHOOK_URL``: a channel webhook URL. We POST a
  ``{"content": ...}`` message to it.
- **Email** — ``SMTP_HOST`` plus ``NOTIFY_EMAIL`` (the recipient). Optional:
  ``SMTP_PORT`` (default 587), ``SMTP_USER``, ``SMTP_PASSWORD``, ``SMTP_FROM``,
  and ``SMTP_STARTTLS`` (set to ``0`` to disable STARTTLS).

Adding another HTTP-POST channel (e.g. ntfy.sh, Slack) is a few lines: add it to
``CHANNELS`` and give it a ``_send_*`` plus a branch in ``channel_configured``.
"""

import json
import os
import smtplib
import threading
import urllib.request
from email.message import EmailMessage

CHANNELS = ("discord", "email")
CHANNEL_LABELS = {"discord": "Discord", "email": "Email"}


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _plural(n, singular, plural=None):
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _mask(value):
    """Show just enough of a secret to recognize it, never the whole thing."""
    if not value:
        return ""
    return "••••" + value[-6:] if len(value) > 8 else "••••"


def channel_configured(name):
    """True when the env vars needed to actually send on ``name`` are present."""
    if name == "discord":
        return bool(_env("DISCORD_WEBHOOK_URL"))
    if name == "email":
        return bool(_env("SMTP_HOST") and _env("NOTIFY_EMAIL"))
    return False


def channel_detail(name):
    """A non-secret, masked one-liner describing how a channel is wired up."""
    if name == "discord":
        return _mask(_env("DISCORD_WEBHOOK_URL"))
    if name == "email":
        host, to = _env("SMTP_HOST"), _env("NOTIFY_EMAIL")
        return f"{to} via {host}" if host and to else ""
    return ""


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


def notify_rsvp(*, title, name, adults, kids, notes, total, updated, enabled=None):
    """Send RSVP notifications on a background thread.

    Called from within the request (so it can be handed plain values and the
    per-channel ``enabled`` toggles); the network/SMTP work happens off-thread
    and swallows all errors. ``enabled`` maps channel name -> bool; a channel
    fires only if it is both configured (env) and enabled (default True).
    """
    active = [c for c in CHANNELS
              if channel_configured(c) and (enabled is None or enabled.get(c, True))]
    if not active:
        return
    body = _compose(title, name, adults, kids, notes, total, updated)
    subject = f"New RSVP: {name} → {title}"
    threading.Thread(
        target=_dispatch, args=(subject, body, active), daemon=True,
    ).start()


def _dispatch(subject, body, active):
    for channel in active:
        try:
            _send(channel, subject, body)
        except Exception:
            pass  # best-effort: never surface a notification failure to the guest


def send_test(channel):
    """Synchronously send a test message. Returns None on success, else an error
    string — used by the Settings page so the admin gets real feedback."""
    if not channel_configured(channel):
        return "Not configured."
    try:
        _send(channel, "RSVP test notification",
              "✅ Test notification from your RSVP app — if you can read this, it works!")
        return None
    except Exception as e:
        return str(e)


def _send(channel, subject, body):
    if channel == "discord":
        _send_discord(body)
    elif channel == "email":
        _send_email(subject, body)


def _send_discord(body):
    # Discord caps content at 2000 chars; our inputs are already short, but be safe.
    payload = json.dumps({"content": ("\U0001F389 " + body)[:2000]}).encode()
    req = urllib.request.Request(
        _env("DISCORD_WEBHOOK_URL"), data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "simple-rsvp"},
    )
    with urllib.request.urlopen(req, timeout=10):
        pass


def _send_email(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _env("SMTP_FROM") or _env("SMTP_USER") or "rsvp@localhost"
    msg["To"] = _env("NOTIFY_EMAIL")
    msg.set_content(body)
    port = int(_env("SMTP_PORT") or 587)
    with smtplib.SMTP(_env("SMTP_HOST"), port, timeout=15) as s:
        if _env("SMTP_STARTTLS", "1").lower() not in ("0", "false", "no"):
            s.starttls()
        user, password = _env("SMTP_USER"), _env("SMTP_PASSWORD")
        if user and password:
            s.login(user, password)
        s.send_message(msg)
