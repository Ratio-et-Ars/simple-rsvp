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
    description TEXT    NOT NULL DEFAULT '',
    cover       TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    listed      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS rsvps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    adults     INTEGER NOT NULL DEFAULT 0,
    kids       INTEGER NOT NULL DEFAULT 0,
    notes      TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rsvps_event ON rsvps(event_id);
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


def create_event(slug, title, dt, location, description, active=True, listed=False):
    db = get_db()
    db.execute(
        """INSERT INTO events (slug, title, datetime, location, description,
                               active, listed, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (slug, title, dt, location, description,
         int(active), int(listed), _now()),
    )
    db.commit()
    return get_event(slug)


def update_event(slug, title, dt, location, description, active, listed):
    db = get_db()
    db.execute(
        """UPDATE events
           SET title = ?, datetime = ?, location = ?, description = ?,
               active = ?, listed = ?
           WHERE slug = ?""",
        (title, dt, location, description, int(active), int(listed), slug),
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


def guest_counts(event_id):
    row = get_db().execute(
        """SELECT COALESCE(SUM(adults), 0) AS adults,
                  COALESCE(SUM(kids), 0)   AS kids
           FROM rsvps WHERE event_id = ?""",
        (event_id,),
    ).fetchone()
    adults, kids = row["adults"], row["kids"]
    return {"adults": adults, "kids": kids, "total": adults + kids}


def add_rsvp(event_id, name, adults, kids, notes):
    db = get_db()
    db.execute(
        """INSERT INTO rsvps (event_id, name, adults, kids, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_id, name, adults, kids, notes, _now()),
    )
    db.commit()


def update_rsvp(rsvp_id, name, adults, kids, notes):
    db = get_db()
    db.execute(
        "UPDATE rsvps SET name = ?, adults = ?, kids = ?, notes = ? WHERE id = ?",
        (name, adults, kids, notes, rsvp_id),
    )
    db.commit()


def delete_rsvp(rsvp_id):
    db = get_db()
    db.execute("DELETE FROM rsvps WHERE id = ?", (rsvp_id,))
    db.commit()
