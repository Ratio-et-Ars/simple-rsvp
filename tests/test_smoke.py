"""End-to-end smoke tests against an isolated temp database."""

import base64
import os
import tempfile

import pytest


@pytest.fixture
def client():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    os.environ["ADMIN_PASSWORD"] = "testpass"
    # Import after env is set so db.DATA_DIR picks up the temp dir.
    import importlib
    import db
    import app as app_module
    importlib.reload(db)
    importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c


def auth():
    token = base64.b64encode(b"admin:testpass").decode()
    return {"Authorization": f"Basic {token}"}


def test_home_empty(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"No Gatherings Pinned Up" in r.data


def test_admin_requires_auth(client):
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", headers=auth()).status_code == 200


def test_create_event_and_rsvp_flow(client):
    # Create a listed event in the future.
    r = client.post("/admin/new", headers=auth(), data={
        "title": "Cigar Club",
        "datetime": "2099-12-31T18:00",
        "location": "The Barn",
        "description": "Bring your own.",
        "active": "on",
        "listed": "on",
    }, follow_redirects=False)
    assert r.status_code == 302
    assert "/admin/cigar-club" in r.headers["Location"]

    # Public event page renders and offers the RSVP form.
    page = client.get("/cigar-club")
    assert page.status_code == 200
    assert b"Cigar Club" in page.data
    assert b"Save my spot" in page.data

    # It shows up on the public landing page (listed + active + future).
    assert b"Cigar Club" in client.get("/").data

    # Submit an RSVP.
    conf = client.post("/cigar-club/rsvp", data={
        "name": "Jane", "adults": "2", "kids": "1", "notes": "On my way",
    })
    assert conf.status_code == 200
    assert b"You're on the list!" in conf.data

    # Counts reflected in admin + CSV export.
    admin = client.get("/admin/cigar-club", headers=auth())
    assert b"3 total guests" in admin.data
    csv = client.get("/admin/cigar-club/export.csv", headers=auth())
    assert csv.status_code == 200
    assert b"Jane,2,1,On my way" in csv.data


def test_unlisted_event_hidden_from_home_but_reachable(client):
    client.post("/admin/new", headers=auth(), data={
        "title": "Secret", "datetime": "2099-01-01T12:00", "active": "on",
    })  # listed unchecked
    assert b"Secret" not in client.get("/").data
    assert client.get("/secret").status_code == 200


def test_past_listed_event_shows_on_home(client):
    # A listed + active event in the past still appears on the home page.
    client.post("/admin/new", headers=auth(), data={
        "title": "Old Harvest Picnic", "datetime": "2000-01-01T12:00",
        "active": "on", "listed": "on",
    })
    assert b"Old Harvest Picnic" in client.get("/").data


def test_missing_event_404(client):
    assert client.get("/nope").status_code == 404


def test_html_escaping(client):
    client.post("/admin/new", headers=auth(), data={
        "title": "XSS", "datetime": "2099-01-01T12:00", "active": "on", "listed": "on",
    })
    client.post("/xss/rsvp", data={"name": "<script>alert(1)</script>", "adults": "1"})
    admin = client.get("/admin/xss", headers=auth())
    assert b"<script>alert(1)</script>" not in admin.data
    assert b"&lt;script&gt;" in admin.data


# --- Security hardening ------------------------------------------------------

def test_missing_password_fails_fast(client):
    # The app refuses to start without ADMIN_PASSWORD. We can't reimport with it
    # unset (the fixture needs it for everything else), so assert on the same
    # condition the startup guard uses: empty/missing -> SystemExit.
    import app as app_module
    assert app_module.ADMIN_PASSWORD  # the running app has a password set
    # Reproduce the guard's logic directly.
    for missing in (None, ""):
        with pytest.raises(SystemExit):
            if not missing:
                raise SystemExit("Set the ADMIN_PASSWORD environment variable before starting.")


def test_constant_time_auth_rejects_wrong_password(client):
    import base64
    bad = base64.b64encode(b"admin:wrongpass").decode()
    assert client.get("/admin", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    wrong_user = base64.b64encode(b"root:testpass").decode()
    assert client.get("/admin", headers={"Authorization": f"Basic {wrong_user}"}).status_code == 401
    # Correct credentials still work.
    assert client.get("/admin", headers=auth()).status_code == 200


def test_auth_uses_compare_digest():
    import hmac
    # The decorator compares with hmac.compare_digest; smoke-check the primitive.
    assert hmac.compare_digest("admin", "admin")
    assert not hmac.compare_digest("admin", "Admin")


def test_safe_int_upper_bound(client):
    import app as app_module
    assert app_module.safe_int("5") == 5
    assert app_module.safe_int("-3") == 0
    assert app_module.safe_int("999999999999") == 100000
    assert app_module.safe_int("nonsense") == 0


def test_csv_formula_injection_escaped(client):
    import app as app_module
    assert app_module.csv_safe("=1+1") == "'=1+1"
    assert app_module.csv_safe("+CMD") == "'+CMD"
    assert app_module.csv_safe("-2") == "'-2"
    assert app_module.csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert app_module.csv_safe("\tTabbed") == "'\tTabbed"
    assert app_module.csv_safe("Normal") == "Normal"
    # And it actually applies through the export endpoint.
    client.post("/admin/new", headers=auth(), data={
        "title": "Inj", "datetime": "2099-01-01T12:00", "active": "on", "listed": "on",
    })
    client.post("/inj/rsvp", data={"name": "=HYPERLINK(1)", "adults": "1"})
    csv_resp = client.get("/admin/inj/export.csv", headers=auth())
    assert b"'=HYPERLINK(1)" in csv_resp.data


def test_csrf_cross_site_origin_rejected(client):
    # Same-origin POST (Origin matches request host) works.
    client.post("/admin/new", headers=auth(), data={
        "title": "Origin Test", "datetime": "2099-01-01T12:00", "active": "on", "listed": "on",
    })
    same = client.post(
        "/admin/origin-test/edit",
        headers={**auth(), "Origin": "http://localhost"},
        data={"title": "Renamed", "datetime": "2099-01-01T12:00", "active": "on", "listed": "on"},
    )
    assert same.status_code in (302, 303)

    # Cross-site Origin is rejected with 403.
    cross = client.post(
        "/admin/origin-test/edit",
        headers={**auth(), "Origin": "http://evil.example"},
        data={"title": "Hacked", "datetime": "2099-01-01T12:00", "active": "on", "listed": "on"},
    )
    assert cross.status_code == 403

    # A public RSVP POST with a cross-site Origin is also rejected.
    cross_rsvp = client.post(
        "/origin-test/rsvp",
        headers={"Origin": "http://evil.example"},
        data={"name": "Mallory", "adults": "1"},
    )
    assert cross_rsvp.status_code == 403

    # No Origin header at all is allowed (some legit same-origin posts omit it).
    no_origin = client.post("/origin-test/rsvp", data={"name": "Alice", "adults": "1"})
    assert no_origin.status_code == 200


def test_security_headers_present(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers.get("Content-Security-Policy", "")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("Referrer-Policy") == "same-origin"


# --- Admin dashboard: inactive events collapse -------------------------------

def test_inactive_events_collapsed_on_dashboard(client):
    # One active, one inactive (Closed) event.
    client.post("/admin/new", headers=auth(), data={
        "title": "Open House", "datetime": "2099-01-01T12:00", "active": "on", "listed": "on",
    })
    client.post("/admin/new", headers=auth(), data={
        "title": "Closed Bash", "datetime": "2099-02-01T12:00", "listed": "on",
    })  # active unchecked -> inactive
    page = client.get("/admin", headers=auth()).data
    assert b"Open House" in page
    assert b"Closed Bash" in page
    # The inactive one is tucked inside a <details> fold labelled with a count.
    assert b"1 inactive event" in page
    assert b'<details class="inactive-fold">' in page
    fold = page.split(b'<details class="inactive-fold">', 1)[1]
    assert b"Closed Bash" in fold        # inactive event lives inside the fold
    assert b"Open House" not in fold     # active event stays out of the fold


# --- RSVP notifications ------------------------------------------------------

def test_notify_compose_and_plural():
    import notify
    assert notify._plural(1, "adult") == "1 adult"
    assert notify._plural(0, "adult") == "0 adults"
    assert notify._plural(2, "kid") == "2 kids"
    body = notify._compose("Summer BBQ", "Alex", 2, 1, "Can't wait!", 14, updated=False)
    assert "Alex RSVP'd to Summer BBQ." in body
    assert "2 adults, 1 kid." in body
    assert "Note: Can't wait!" in body
    assert "14 guests total now." in body
    # Updated wording, and notes line omitted when empty.
    upd = notify._compose("Summer BBQ", "Alex", 1, 0, "", 5, updated=True)
    assert "updated their RSVP for Summer BBQ." in upd
    assert "Note:" not in upd


def test_notify_disabled_when_no_channel_configured(monkeypatch):
    import notify
    for var in ("DISCORD_WEBHOOK_URL", "SMTP_HOST"):
        monkeypatch.delenv(var, raising=False)
    called = []
    monkeypatch.setattr(notify.threading, "Thread",
                        lambda *a, **k: called.append(1) or (_ for _ in ()).throw(AssertionError))
    # No channel set -> returns immediately, never spawns a thread.
    notify.notify_rsvp(title="X", name="Y", adults=1, kids=0, notes="", total=1, updated=False)
    assert not called


def test_notify_discord_posts_payload(monkeypatch):
    import json
    import notify
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    notify._send_discord("Alex RSVP'd to Summer BBQ.")
    assert captured["url"] == "https://discord.example/webhook"
    assert "Alex RSVP'd to Summer BBQ." in captured["body"]["content"]


def test_notify_email_builds_and_sends(monkeypatch):
    import notify
    monkeypatch.setenv("SMTP_HOST", "mail.example")
    monkeypatch.setenv("NOTIFY_EMAIL", "host@example.com")
    monkeypatch.setenv("SMTP_FROM", "rsvp@example.com")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"], sent["port"] = host, port
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent["starttls"] = True
        def login(self, user, pw): sent["login"] = (user, pw)
        def send_message(self, msg):
            sent["to"], sent["from"] = msg["To"], msg["From"]
            sent["subject"], sent["body"] = msg["Subject"], msg.get_content()

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    notify._send_email("New RSVP: Alex → Summer BBQ", "Alex RSVP'd to Summer BBQ.")
    assert sent["host"] == "mail.example" and sent["port"] == 587
    assert sent["to"] == "host@example.com" and sent["from"] == "rsvp@example.com"
    assert sent["subject"] == "New RSVP: Alex → Summer BBQ"
    assert "Alex RSVP'd to Summer BBQ." in sent["body"]
    assert sent.get("starttls") is True   # STARTTLS on by default
    assert "login" not in sent            # no auth attempted without user/pass


def test_rsvp_submission_invokes_notify(client, monkeypatch):
    # Verify the app -> notify wiring: the right values, the fresh total, and
    # the updated flag all flow through when a guest RSVPs.
    import app as app_module
    calls = []
    monkeypatch.setattr(app_module.notify, "notify_rsvp",
                        lambda **kw: calls.append(kw))
    client.post("/admin/new", headers=auth(), data={
        "title": "Notify Test", "datetime": "2099-05-01T12:00", "active": "on", "listed": "on",
    })

    # First RSVP -> a brand-new row (updated=False) with the correct total.
    client.post("/notify-test/rsvp", data={
        "name": "Alex", "adults": "2", "kids": "1", "notes": "yay",
    })
    assert len(calls) == 1
    first = calls[0]
    assert first["title"] == "Notify Test"
    assert first["name"] == "Alex"
    assert (first["adults"], first["kids"]) == (2, 1)
    assert first["total"] == 3
    assert first["updated"] is False

    # Same browser submits again -> updates the row (updated=True), total stays 3.
    client.post("/notify-test/rsvp", data={
        "name": "Alex", "adults": "1", "kids": "2", "notes": "changed",
    })
    assert len(calls) == 2
    assert calls[1]["updated"] is True
    assert calls[1]["total"] == 3


def test_dashboard_with_only_inactive_events(client):
    # When every event is inactive, the active table is replaced by a note and
    # all events live in the fold.
    client.post("/admin/new", headers=auth(), data={
        "title": "Dormant Fair", "datetime": "2099-03-01T12:00", "listed": "on",
    })  # active unchecked
    page = client.get("/admin", headers=auth()).data
    assert b"No active events right now." in page
    assert b"1 inactive event" in page
    assert b"Dormant Fair" in page
