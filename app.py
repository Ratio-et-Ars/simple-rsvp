"""Simple, self-hosted, multi-event RSVP app.

Events are addressed by slug (``/<slug>``). The landing page lists events that
are both *listed* and *active*; unlisted events stay reachable by their slug
URL but never appear on the landing page. All ``/admin`` routes are protected
by HTTP Basic Auth.
"""

import csv
import hmac
import io
import os
import re
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import quote, urlencode, urlsplit

from flask import (
    Flask, Response, abort, make_response, redirect, render_template, request,
    send_from_directory, url_for,
)
from werkzeug.exceptions import HTTPException
from PIL import Image, ImageOps

import db
import notify

# Cap decoded image size to defend against decompression-bomb uploads.
Image.MAX_IMAGE_PIXELS = 30_000_000

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload cap

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise SystemExit("Set the ADMIN_PASSWORD environment variable before starting.")

# Stamped in at Docker build time from the release tag; "dev" for local runs.
APP_VERSION = os.environ.get("APP_VERSION", "dev")

ALLOWED_EXTENSIONS = ("png", "jpg", "jpeg", "webp")
# Map Pillow's decoded format to the extension we save under. Trusting the
# decoded format (not the uploaded filename) means a renamed file can't smuggle
# in an unexpected type.
FORMAT_TO_EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}

# Neutral default for the public RSVP "notes" box. Events can override it per-event
# (e.g. a potluck might ask "Bringing a side dish?"); blank falls back to this.
DEFAULT_NOTES_HINT = "Anything we should know? Let us know!"

app.teardown_appcontext(db.close_db)


@app.after_request
def set_security_headers(resp):
    # The app serves only same-origin assets (no CDN). Inline <script>/<style>
    # and on* handlers in the templates require 'unsafe-inline'; the noise
    # background in style.css is an inline data: SVG, hence img-src data:.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    )
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


# --- Helpers ----------------------------------------------------------------

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "event"


def unique_slug(base):
    """Return ``base`` or ``base-2``, ``base-3``... so slugs never collide."""
    slug = base
    n = 2
    while db.slug_exists(slug):
        slug = f"{base}-{n}"
        n += 1
    return slug


def safe_int(value):
    # Clamp to a sane range so a single RSVP can't claim billions of guests.
    try:
        return min(100000, max(0, int(value)))
    except (TypeError, ValueError):
        return 0


def csv_safe(value):
    """Neutralize CSV/spreadsheet formula injection.

    A cell that begins with ``= + - @`` (or a tab/CR) can be interpreted as a
    formula by Excel/Sheets. Prefix such values with a single quote so the
    spreadsheet treats them as plain text.
    """
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def check_csrf():
    """Reject cross-site state-changing requests.

    Browsers send an ``Origin`` header on cross-site (and most same-site) POSTs.
    If it's present and its host differs from the request host, the POST came
    from another site, so block it. When ``Origin`` is absent we allow it: some
    legitimate same-origin form posts omit it, and same-site GETs never set it.
    """
    origin = request.headers.get("Origin")
    if not origin:
        return
    if urlsplit(origin).netloc != request.host:
        abort(403)


def format_datetime(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str)
    except (TypeError, ValueError):
        return "Date TBD", ""
    # %-I/%-d are glibc-only; build the strings without them for portability.
    time_str = dt.strftime("%I:%M %p").lstrip("0")
    date_str = dt.strftime("%A, %B ") + str(dt.day) + dt.strftime(", %Y")
    return date_str, time_str


def event_view(event):
    """Build the display context shared by public event pages."""
    try:
        dt = datetime.fromisoformat(event["datetime"])
        days_remaining = (dt.date() - date.today()).days
    except (TypeError, ValueError):
        dt = None
        days_remaining = None

    if days_remaining is None:
        countdown, is_past = "", False
    elif days_remaining < 0:
        countdown, is_past = "", True
    else:
        countdown, is_past = humanize_countdown(days_remaining), False

    date_str, time_str = format_datetime(event["datetime"])
    return {
        "event": event,
        "date_str": date_str,
        "time_str": time_str,
        "countdown": countdown,
        "is_past": is_past,
        "show_form": bool(event["active"]) and not is_past,
        "maps_url": maps_url(event),
        "gcal_url": gcal_url(event),
        "cover_url": cover_url(event),
        "notes_hint": event["notes_hint"] or DEFAULT_NOTES_HINT,
    }


def gcal_url(event):
    """'Add to Google Calendar' link. No end time in the schema, so default +2h."""
    try:
        start = datetime.fromisoformat(event["datetime"])
    except (TypeError, ValueError):
        return None
    fmt = "%Y%m%dT%H%M%S"
    params = {
        "action": "TEMPLATE",
        "text": event["title"],
        "dates": start.strftime(fmt) + "/" + (start + timedelta(hours=2)).strftime(fmt),
        "details": event["description"] or "",
        "location": (event["address"] or "").strip() or (event["location"] or "").strip(),
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def humanize_countdown(days):
    """Round a day count to a friendly unit ('Tomorrow', '3 weeks to go')."""
    if days == 0:
        return "Today!"
    if days == 1:
        return "Tomorrow"
    if days < 14:
        return f"{days} days to go"
    if days < 60:
        return f"{round(days / 7)} weeks to go"
    return f"{round(days / 30)} months to go"


def maps_url(event):
    """Google Maps search URL — uses the address if given, else the location name."""
    query = (event["address"] or "").strip() or (event["location"] or "").strip()
    if not query:
        return None
    return "https://www.google.com/maps/search/?api=1&query=" + quote(query)


def guest_mailto(event, emails):
    """A ``mailto:`` link that drafts an email to every guest in BCC.

    The zero-config way to reach guests: opens the admin's own mail client with
    addresses hidden from each other (BCC) and the subject pre-filled. Returns
    None when nobody has left an email yet.
    """
    if not emails:
        return None
    params = urlencode({"subject": f"Update: {event['title']}"}, quote_via=quote)
    # BCC addresses go in raw (joined by commas) — mail clients expect them
    # unencoded, and addresses never contain characters that need escaping.
    return "mailto:?bcc=" + ",".join(emails) + "&" + params


def cover_url(event):
    """Cover image URL with an mtime cache-buster so a re-upload shows at once."""
    if not event["cover"]:
        return None
    try:
        version = int(os.path.getmtime(os.path.join(db.UPLOAD_DIR, event["cover"])))
    except OSError:
        version = 0
    return url_for("cover", slug=event["slug"]) + f"?v={version}"


def basic_auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        ok = bool(auth) and hmac.compare_digest(auth.username or "", "admin") \
            and hmac.compare_digest(auth.password or "", ADMIN_PASSWORD)
        if not ok:
            return Response(
                "Login required", 401,
                {"WWW-Authenticate": "Basic realm='RSVP Admin'"},
            )
        return f(*args, **kwargs)
    return decorated


# --- Public routes ----------------------------------------------------------

@app.route("/")
def home():
    views = [event_view(e) for e in db.list_events(only_listed=True, only_active=True)]
    # Upcoming first (soonest at top), then past events (most recent first).
    upcoming = [v for v in views if not v["is_past"]]
    past = [v for v in views if v["is_past"]][::-1]
    return render_template("index.html", events=upcoming + past)


@app.route("/cover/<slug>")
def cover(slug):
    event = db.get_event(slug)
    if not event or not event["cover"]:
        abort(404)
    return send_from_directory(db.UPLOAD_DIR, event["cover"])


@app.route("/<slug>")
def event_page(slug):
    event = db.get_event(slug)
    if not event:
        abort(404)
    my_rsvp = db.get_rsvp_by_token(request.cookies.get("rsvp_" + slug))
    if my_rsvp and my_rsvp["event_id"] != event["id"]:
        my_rsvp = None
    return render_template("event.html", my_rsvp=my_rsvp, **event_view(event))


@app.route("/<slug>/rsvp", methods=["POST"])
def submit_rsvp(slug):
    check_csrf()
    event = db.get_event(slug)
    if not event:
        abort(404)
    view = event_view(event)
    if not view["show_form"]:
        abort(403)
    name = request.form.get("name", "").strip()
    if not name:
        abort(400)
    adults = safe_int(request.form.get("adults", 1))
    kids = safe_int(request.form.get("kids", 0))
    notes = request.form.get("notes", "").strip()[:1000]
    email = request.form.get("email", "").strip()[:254]

    # If this browser already holds a token for an RSVP to this event, update
    # that row instead of creating a duplicate. The token is an unguessable
    # capability — only the submitter's browser can edit their own RSVP.
    existing = db.get_rsvp_by_token(request.cookies.get("rsvp_" + slug))
    updated = bool(existing and existing["event_id"] == event["id"])
    if updated:
        token = existing["token"]
        db.update_rsvp(existing["id"], name[:200], adults, kids, notes, email)
    else:
        token = secrets.token_urlsafe(16)
        db.add_rsvp(event["id"], name[:200], adults, kids, notes, token, email)

    # Best-effort ping to any configured + enabled channel (Discord/email).
    # Compute the fresh total and the per-channel toggles here, in the request
    # (where the DB is available), and hand notify() plain values — the network
    # work happens off-thread and never blocks this response.
    notify.notify_rsvp(
        title=event["title"], name=name[:200], adults=adults, kids=kids,
        notes=notes, total=db.guest_counts(event["id"])["total"], updated=updated,
        enabled=_notify_toggles(),
    )

    resp = make_response(render_template(
        "confirmation.html",
        event=event, date_str=view["date_str"], time_str=view["time_str"],
        gcal_url=view["gcal_url"], updated=updated,
    ))
    resp.set_cookie("rsvp_" + slug, token, max_age=60 * 60 * 24 * 400,
                    samesite="Lax", httponly=True)
    return resp


@app.route("/<slug>/rsvp/cancel", methods=["POST"])
def cancel_rsvp(slug):
    check_csrf()
    event = db.get_event(slug)
    if not event:
        abort(404)
    existing = db.get_rsvp_by_token(request.cookies.get("rsvp_" + slug))
    if existing and existing["event_id"] == event["id"]:
        db.delete_rsvp(existing["id"])
    resp = make_response(redirect(url_for("event_page", slug=slug)))
    resp.delete_cookie("rsvp_" + slug)
    return resp


# --- Admin routes -----------------------------------------------------------

def _notify_toggles():
    """Per-channel on/off flags from the settings table (default: on)."""
    return {c: db.get_setting(f"notify_{c}_enabled", "1") != "0"
            for c in notify.CHANNELS}


def _human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _settings_context():
    """Everything the Settings page renders: channel status, stats, runtime."""
    from importlib.metadata import PackageNotFoundError, version as pkg_version
    import platform

    def pkg(name):
        try:
            return pkg_version(name)
        except PackageNotFoundError:
            return "?"

    toggles = _notify_toggles()
    channels = [{
        "key": c,
        "label": notify.CHANNEL_LABELS[c],
        "configured": notify.channel_configured(c),
        "detail": notify.channel_detail(c),
        "enabled": toggles[c],
    } for c in notify.CHANNELS]
    try:
        db_size = _human_size(os.path.getsize(db.DB_PATH))
    except OSError:
        db_size = "—"
    return {
        "version": APP_VERSION,
        "channels": channels,
        "stats": db.stats(),
        "runtime": {
            "python": platform.python_version(),
            "flask": pkg("flask"),
            "data_dir": os.path.abspath(db.DATA_DIR),
            "db_size": db_size,
        },
        "tested": request.args.get("tested"),
        "test_error": request.args.get("error"),
    }


@app.route("/admin")
@basic_auth_required
def admin():
    events = db.list_events()
    return render_template("admin/dashboard.html", events=events)


@app.route("/admin/settings", methods=["GET", "POST"])
@basic_auth_required
def admin_settings():
    if request.method == "POST":
        check_csrf()
        for c in notify.CHANNELS:
            db.set_setting(
                f"notify_{c}_enabled",
                "1" if request.form.get(f"enable_{c}") == "on" else "0",
            )
        return redirect(url_for("admin_settings"))
    return render_template("admin/settings.html", **_settings_context())


@app.route("/admin/settings/test", methods=["POST"])
@basic_auth_required
def admin_test_notification():
    check_csrf()
    channel = request.form.get("channel", "")
    if channel not in notify.CHANNELS:
        abort(400)
    # Test the configuration regardless of the on/off toggle, and surface the
    # result back on the Settings page via query params (no session/flash needed).
    error = notify.send_test(channel)
    return redirect(url_for("admin_settings", tested=channel, error=error or ""))


@app.route("/admin/new", methods=["GET", "POST"])
@basic_auth_required
def admin_new():
    if request.method == "POST":
        check_csrf()
        title = request.form["title"].strip()
        base = slugify(request.form.get("slug", "").strip() or title)
        slug = unique_slug(base)
        db.create_event(
            slug=slug,
            title=title,
            dt=request.form["datetime"],
            location=request.form.get("location", "").strip(),
            address=request.form.get("address", "").strip(),
            description=request.form.get("description", "").strip(),
            notes_hint=request.form.get("notes_hint", "").strip(),
            active=request.form.get("active") == "on",
            listed=request.form.get("listed") == "on",
        )
        return redirect(url_for("admin_event", slug=slug))
    return render_template("admin/new.html", default_notes_hint=DEFAULT_NOTES_HINT)


@app.route("/admin/<slug>")
@basic_auth_required
def admin_event(slug):
    event = db.get_event(slug)
    if not event:
        abort(404)
    emails = db.guest_emails(event["id"])
    return render_template(
        "admin/event.html",
        event=event,
        rsvps=db.list_rsvps(event["id"]),
        counts=db.guest_counts(event["id"]),
        cover_url=cover_url(event),
        default_notes_hint=DEFAULT_NOTES_HINT,
        email_count=len(emails),
        mailto_url=guest_mailto(event, emails),
        smtp_configured=notify.smtp_configured(),
        broadcast_sent=request.args.get("sent"),
        broadcast_error=request.args.get("error"),
    )


@app.route("/admin/<slug>/edit", methods=["POST"])
@basic_auth_required
def admin_edit_event(slug):
    check_csrf()
    if not db.get_event(slug):
        abort(404)
    db.update_event(
        slug=slug,
        title=request.form["title"].strip(),
        dt=request.form["datetime"],
        location=request.form.get("location", "").strip(),
        address=request.form.get("address", "").strip(),
        description=request.form.get("description", "").strip(),
        notes_hint=request.form.get("notes_hint", "").strip(),
        active=request.form.get("active") == "on",
        listed=request.form.get("listed") == "on",
    )
    return redirect(url_for("admin_event", slug=slug))


@app.route("/admin/<slug>/delete", methods=["POST"])
@basic_auth_required
def admin_delete_event(slug):
    check_csrf()
    event = db.get_event(slug)
    if not event:
        abort(404)
    if event["cover"]:
        _remove_cover_files(slug)
    db.delete_event(slug)
    return redirect(url_for("admin"))


@app.route("/admin/<slug>/rsvp/<int:rsvp_id>", methods=["POST"])
@basic_auth_required
def admin_edit_rsvp(slug, rsvp_id):
    check_csrf()
    if not db.get_event(slug):
        abort(404)
    if "delete" in request.form:
        db.delete_rsvp(rsvp_id)
    else:
        db.update_rsvp(
            rsvp_id,
            request.form["name"].strip(),
            safe_int(request.form.get("adults", 0)),
            safe_int(request.form.get("kids", 0)),
            request.form.get("notes", "").strip(),
            request.form.get("email", "").strip()[:254],
        )
    return redirect(url_for("admin_event", slug=slug))


@app.route("/admin/<slug>/notify", methods=["POST"])
@basic_auth_required
def admin_notify_guests(slug):
    """Server-side email broadcast to everyone who left an address.

    The configured-SMTP counterpart to the mailto link: the admin writes a
    subject + message here and the server sends it (guests Bcc'd). Result is
    surfaced back on the manage page via query params, mirroring the Settings
    test-notification pattern.
    """
    check_csrf()
    event = db.get_event(slug)
    if not event:
        abort(404)
    subject = request.form.get("subject", "").strip()[:200] or f"Update: {event['title']}"
    message = request.form.get("message", "").strip()[:5000]
    if not message:
        return redirect(url_for("admin_event", slug=slug,
                                error="Write a message before sending."))
    error = notify.send_guest_broadcast(subject, message, db.guest_emails(event["id"]))
    if error:
        return redirect(url_for("admin_event", slug=slug, error=error))
    return redirect(url_for("admin_event", slug=slug, sent="1"))


@app.route("/admin/<slug>/export.csv")
@basic_auth_required
def export_csv(slug):
    event = db.get_event(slug)
    if not event:
        abort(404)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Email", "Adults", "Kids", "Notes"])
    for r in db.list_rsvps(event["id"]):
        writer.writerow([
            csv_safe(r["name"]), csv_safe(r["email"]),
            r["adults"], r["kids"], csv_safe(r["notes"]),
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={slug}-rsvps.csv"},
    )


def _remove_cover_files(slug):
    for ext in ALLOWED_EXTENSIONS:
        try:
            os.remove(os.path.join(db.UPLOAD_DIR, f"{slug}.{ext}"))
        except FileNotFoundError:
            pass


@app.route("/admin/<slug>/upload", methods=["POST"])
@basic_auth_required
def upload_cover(slug):
    check_csrf()
    if not db.get_event(slug):
        abort(404)
    file = request.files.get("cover")
    if not file or not file.filename:
        abort(400, "Invalid file")
    # Trust the decoded image, not the uploaded filename. Pillow validates the
    # actual bytes; a too-large or malformed image raises and we 400 cleanly.
    try:
        img = Image.open(file.stream)
        ext = FORMAT_TO_EXT.get(img.format)
        if ext is None:
            abort(400, "Invalid image")
        img = ImageOps.exif_transpose(img)  # honor phone-photo rotation
        img.thumbnail((1600, 900))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        os.makedirs(db.UPLOAD_DIR, exist_ok=True)
        _remove_cover_files(slug)  # only one cover per event
        filename = f"{slug}.{ext}"
        img.save(os.path.join(db.UPLOAD_DIR, filename))
    except HTTPException:
        raise  # let our own abort(400) through
    except Exception:
        # Malformed image, unsupported codec, or a decompression bomb.
        abort(400, "Invalid image")
    db.set_cover(slug, filename)
    return redirect(url_for("admin_event", slug=slug))


# --- Errors -----------------------------------------------------------------

@app.errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


# --- One-time migration from the old single-event JSON layout ---------------

def migrate_legacy():
    """Import the pre-SQLite single event/RSVP files, if present, once."""
    legacy_event = os.path.join(db.DATA_DIR, "event.json")
    if not os.path.exists(legacy_event):
        return
    import json
    with app.app_context():
        if db.list_events():
            return  # already have data; don't re-import
        with open(legacy_event) as f:
            ev = json.load(f)
        slug = unique_slug(slugify(ev.get("title", "event")))
        db.create_event(
            slug=slug,
            title=ev.get("title", "Event"),
            dt=ev.get("datetime", ""),
            location=ev.get("location", ""),
            description=ev.get("description", ""),
            active=ev.get("active", True),
            listed=True,
        )
        event = db.get_event(slug)
        legacy_rsvps = os.path.join(db.DATA_DIR, "rsvps.json")
        if os.path.exists(legacy_rsvps):
            with open(legacy_rsvps) as f:
                for r in json.load(f):
                    db.add_rsvp(
                        event["id"], r.get("name", ""),
                        safe_int(r.get("adults", 0)), safe_int(r.get("kids", 0)),
                        r.get("notes", ""),
                    )
        # Carry over a legacy cover image, if one exists.
        for ext in ALLOWED_EXTENSIONS:
            src = os.path.join("static", f"cover.{ext}")
            if os.path.exists(src):
                os.makedirs(db.UPLOAD_DIR, exist_ok=True)
                os.replace(src, os.path.join(db.UPLOAD_DIR, f"{slug}.{ext}"))
                db.set_cover(slug, f"{slug}.{ext}")
                break
        # Mark the legacy files as migrated so this never runs again.
        os.replace(legacy_event, legacy_event + ".migrated")
        if os.path.exists(legacy_rsvps):
            os.replace(legacy_rsvps, legacy_rsvps + ".migrated")


db.init_db()
migrate_legacy()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3022))
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    app.run(debug=debug, host="0.0.0.0", port=port)
