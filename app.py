"""Simple, self-hosted, multi-event RSVP app.

Events are addressed by slug (``/<slug>``). The landing page lists events that
are both *listed* and *active*; unlisted events stay reachable by their slug
URL but never appear on the landing page. All ``/admin`` routes are protected
by HTTP Basic Auth.
"""

import csv
import io
import os
import re
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import quote, urlencode

from flask import (
    Flask, Response, abort, make_response, redirect, render_template, request,
    send_from_directory, url_for,
)
from PIL import Image, ImageOps

import db

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload cap

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "letmein")
ALLOWED_EXTENSIONS = ("png", "jpg", "jpeg", "webp")

app.teardown_appcontext(db.close_db)


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
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


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
        if not auth or auth.username != "admin" or auth.password != ADMIN_PASSWORD:
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

    # If this browser already holds a token for an RSVP to this event, update
    # that row instead of creating a duplicate. The token is an unguessable
    # capability — only the submitter's browser can edit their own RSVP.
    existing = db.get_rsvp_by_token(request.cookies.get("rsvp_" + slug))
    if existing and existing["event_id"] == event["id"]:
        token = existing["token"]
        db.update_rsvp(existing["id"], name[:200], adults, kids, notes)
    else:
        token = secrets.token_urlsafe(16)
        db.add_rsvp(event["id"], name[:200], adults, kids, notes, token)

    resp = make_response(render_template(
        "confirmation.html",
        event=event, date_str=view["date_str"], time_str=view["time_str"],
        gcal_url=view["gcal_url"], updated=bool(existing and existing["event_id"] == event["id"]),
    ))
    resp.set_cookie("rsvp_" + slug, token, max_age=60 * 60 * 24 * 400,
                    samesite="Lax", httponly=True)
    return resp


@app.route("/<slug>/rsvp/cancel", methods=["POST"])
def cancel_rsvp(slug):
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

@app.route("/admin")
@basic_auth_required
def admin():
    events = db.list_events()
    return render_template("admin/dashboard.html", events=events)


@app.route("/admin/new", methods=["GET", "POST"])
@basic_auth_required
def admin_new():
    if request.method == "POST":
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
            active=request.form.get("active") == "on",
            listed=request.form.get("listed") == "on",
        )
        return redirect(url_for("admin_event", slug=slug))
    return render_template("admin/new.html")


@app.route("/admin/<slug>")
@basic_auth_required
def admin_event(slug):
    event = db.get_event(slug)
    if not event:
        abort(404)
    return render_template(
        "admin/event.html",
        event=event,
        rsvps=db.list_rsvps(event["id"]),
        counts=db.guest_counts(event["id"]),
        cover_url=cover_url(event),
    )


@app.route("/admin/<slug>/edit", methods=["POST"])
@basic_auth_required
def admin_edit_event(slug):
    if not db.get_event(slug):
        abort(404)
    db.update_event(
        slug=slug,
        title=request.form["title"].strip(),
        dt=request.form["datetime"],
        location=request.form.get("location", "").strip(),
        address=request.form.get("address", "").strip(),
        description=request.form.get("description", "").strip(),
        active=request.form.get("active") == "on",
        listed=request.form.get("listed") == "on",
    )
    return redirect(url_for("admin_event", slug=slug))


@app.route("/admin/<slug>/delete", methods=["POST"])
@basic_auth_required
def admin_delete_event(slug):
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
        )
    return redirect(url_for("admin_event", slug=slug))


@app.route("/admin/<slug>/export.csv")
@basic_auth_required
def export_csv(slug):
    event = db.get_event(slug)
    if not event:
        abort(404)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Adults", "Kids", "Notes"])
    for r in db.list_rsvps(event["id"]):
        writer.writerow([r["name"], r["adults"], r["kids"], r["notes"]])
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
    if not db.get_event(slug):
        abort(404)
    file = request.files.get("cover")
    if not file or not file.filename.lower().endswith(tuple(f".{e}" for e in ALLOWED_EXTENSIONS)):
        abort(400, "Invalid file")
    ext = file.filename.rsplit(".", 1)[-1].lower()
    os.makedirs(db.UPLOAD_DIR, exist_ok=True)
    _remove_cover_files(slug)  # only one cover per event
    img = Image.open(file.stream)
    img = ImageOps.exif_transpose(img)  # honor phone-photo rotation
    img.thumbnail((1600, 900))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    filename = f"{slug}.{ext}"
    img.save(os.path.join(db.UPLOAD_DIR, filename))
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
