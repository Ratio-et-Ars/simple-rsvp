"""SQLite data layer for the RSVP app.

One database file lives at ``$DATA_DIR/rsvp.db`` (default ``data/rsvp.db``),
which is the directory persisted by the Docker volume. Connections are opened
per request and stored on Flask's ``g`` so they are safe under the threaded
WSGI server.
"""

import os
import sqlite3
from datetime import datetime, timezone

from flask import g

DATA_DIR = os.environ.get("DATA_DIR", "data")
DB_PATH = os.path.join(DATA_DIR, "rsvp.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    UNIQUE NOT NULL,
    title       TEXT    NOT NULL,
    datetime    TEXT    NOT NULL,
    location    TEXT    NOT NULL DEFAULT '',
    address     TEXT    NOT NULL DEFAULT '',
    description TEXT    NOT NULL DEFAULT '',
    notes_hint  TEXT    NOT NULL DEFAULT '',
    cover       TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    listed      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS rsvps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    email      TEXT    NOT NULL DEFAULT '',
    adults     INTEGER NOT NULL DEFAULT 0,
    kids       INTEGER NOT NULL DEFAULT 0,
    notes      TEXT    NOT NULL DEFAULT '',
    token      TEXT,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rsvps_event ON rsvps(event_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    if "db" not in g:
        os.makedirs(DATA_DIR, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist. Safe to call on every startup."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA)
        # Add columns introduced after a DB may already exist.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(events)")]
        if "address" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN address TEXT NOT NULL DEFAULT ''")
        if "notes_hint" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN notes_hint TEXT NOT NULL DEFAULT ''")
        rcols = [r[1] for r in conn.execute("PRAGMA table_info(rsvps)")]
        if "token" not in rcols:
            conn.execute("ALTER TABLE rsvps ADD COLUMN token TEXT")
        if "email" not in rcols:
            conn.execute("ALTER TABLE rsvps ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        conn.commit()
    finally:
        conn.close()


# --- Events -----------------------------------------------------------------

def list_events(only_listed=False, only_active=False):
    sql = """
        SELECT e.*,
               COALESCE(SUM(r.adults), 0) AS total_adults,
               COALESCE(SUM(r.kids), 0)   AS total_kids
        FROM events e
        LEFT JOIN rsvps r ON r.event_id = e.id
    """
    where = []
    if only_listed:
        where.append("e.listed = 1")
    if only_active:
        where.append("e.active = 1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY e.id ORDER BY e.datetime ASC"
    return get_db().execute(sql).fetchall()


def get_event(slug):
    return get_db().execute(
        "SELECT * FROM events WHERE slug = ?", (slug,)
    ).fetchone()


def slug_exists(slug):
    return get_event(slug) is not None


def create_event(slug, title, dt, location, description, active=True, listed=False,
                 address="", notes_hint=""):
    db = get_db()
    db.execute(
        """INSERT INTO events (slug, title, datetime, location, address, description,
                               notes_hint, active, listed, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (slug, title, dt, location, address, description,
         notes_hint, int(active), int(listed), _now()),
    )
    db.commit()
    return get_event(slug)


def update_event(slug, title, dt, location, description, active, listed,
                 address="", notes_hint=""):
    db = get_db()
    db.execute(
        """UPDATE events
           SET title = ?, datetime = ?, location = ?, address = ?, description = ?,
               notes_hint = ?, active = ?, listed = ?
           WHERE slug = ?""",
        (title, dt, location, address, description, notes_hint, int(active), int(listed), slug),
    )
    db.commit()


def set_cover(slug, filename):
    db = get_db()
    db.execute("UPDATE events SET cover = ? WHERE slug = ?", (filename, slug))
    db.commit()


def delete_event(slug):
    db = get_db()
    db.execute("DELETE FROM events WHERE slug = ?", (slug,))
    db.commit()


# --- RSVPs ------------------------------------------------------------------

def list_rsvps(event_id):
    return get_db().execute(
        "SELECT * FROM rsvps WHERE event_id = ? ORDER BY created_at ASC",
        (event_id,),
    ).fetchall()


def guest_emails(event_id):
    """Distinct, non-empty guest emails for an event, in RSVP order.

    The reachable subset of guests — used to message everyone when an event
    changes (e.g. a reschedule). Guests who left the email blank are simply
    absent here; there's no other way to reach them.
    """
    rows = get_db().execute(
        """SELECT email FROM rsvps
           WHERE event_id = ? AND email != ''
           ORDER BY created_at ASC""",
        (event_id,),
    ).fetchall()
    seen, out = set(), []
    for r in rows:
        e = r["email"]
        if e.lower() not in seen:
            seen.add(e.lower())
            out.append(e)
    return out


def guest_counts(event_id):
    row = get_db().execute(
        """SELECT COALESCE(SUM(adults), 0) AS adults,
                  COALESCE(SUM(kids), 0)   AS kids
           FROM rsvps WHERE event_id = ?""",
        (event_id,),
    ).fetchone()
    adults, kids = row["adults"], row["kids"]
    return {"adults": adults, "kids": kids, "total": adults + kids}


def add_rsvp(event_id, name, adults, kids, notes, token=None, email=""):
    db = get_db()
    db.execute(
        """INSERT INTO rsvps (event_id, name, email, adults, kids, notes, token, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_id, name, email, adults, kids, notes, token, _now()),
    )
    db.commit()


def get_rsvp_by_token(token):
    if not token:
        return None
    return get_db().execute(
        "SELECT * FROM rsvps WHERE token = ?", (token,)
    ).fetchone()


def update_rsvp(rsvp_id, name, adults, kids, notes, email=""):
    db = get_db()
    db.execute(
        "UPDATE rsvps SET name = ?, email = ?, adults = ?, kids = ?, notes = ? WHERE id = ?",
        (name, email, adults, kids, notes, rsvp_id),
    )
    db.commit()


def delete_rsvp(rsvp_id):
    db = get_db()
    db.execute("DELETE FROM rsvps WHERE id = ?", (rsvp_id,))
    db.commit()


# --- Settings (key/value) ---------------------------------------------------

def get_setting(key, default=None):
    row = get_db().execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        """INSERT INTO settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, str(value)),
    )
    db.commit()


def stats():
    """At-a-glance totals across the whole instance, for the admin dashboard."""
    row = get_db().execute(
        """SELECT (SELECT COUNT(*) FROM events) AS events,
                  (SELECT COUNT(*) FROM rsvps)  AS rsvps,
                  (SELECT COALESCE(SUM(adults + kids), 0) FROM rsvps) AS guests"""
    ).fetchone()
    return {"events": row["events"], "rsvps": row["rsvps"], "guests": row["guests"]}
