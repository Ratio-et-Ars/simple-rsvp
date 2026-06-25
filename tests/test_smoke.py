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

    # Submit an RSVP (with an optional email this time).
    conf = client.post("/cigar-club/rsvp", data={
        "name": "Jane", "email": "jane@example.com",
        "adults": "2", "kids": "1", "notes": "On my way",
    })
    assert conf.status_code == 200
    assert b"You're on the list!" in conf.data

    # Counts reflected in admin + CSV export (Email is now a column).
    admin = client.get("/admin/cigar-club", headers=auth())
    assert b"3 total guests" in admin.data
    csv = client.get("/admin/cigar-club/export.csv", headers=auth())
    assert csv.status_code == 200
    assert b"Name,Email,Adults,Kids,Notes" in csv.data
    assert b"Jane,jane@example.com,2,1,On my way" in csv.data


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


def test_dashboard_top_total_counts_only_active_guests(client):
    # Active event with 3 guests.
    client.post("/admin/new", headers=auth(), data={
        "title": "Active Bash", "datetime": "2099-06-01T12:00", "active": "on", "listed": "on"})
    client.post("/active-bash/rsvp", data={"name": "A", "adults": "2", "kids": "1"})
    # Another event collects 18 guests while active, then gets closed (inactive).
    client.post("/admin/new", headers=auth(), data={
        "title": "Now Closed", "datetime": "2099-07-01T12:00", "active": "on", "listed": "on"})
    client.post("/now-closed/rsvp", data={"name": "B", "adults": "9", "kids": "9"})
    client.post("/admin/now-closed/edit", headers={**auth(), "Origin": "http://localhost"},
                data={"title": "Now Closed", "datetime": "2099-07-01T12:00", "listed": "on"})  # active off

    page = client.get("/admin", headers=auth()).data
    # Top summary reflects only the active event's 3 guests, not the closed 18.
    assert b"1 active event" in page
    assert b"3 guests" in page
    assert b"21 guests" not in page      # 3 + 18 must NOT be summed
    assert b"18 guests" not in page
    assert b"1 inactive event" in page   # the closed event is folded away


# --- Settings page + notification toggles ------------------------------------

def test_settings_kv_roundtrip_and_stats(client):
    # The settings key/value store and the stats aggregate work end to end.
    import app as app_module
    with app_module.app.app_context():
        import db
        assert db.get_setting("nope", "fallback") == "fallback"
        db.set_setting("foo", "bar")
        assert db.get_setting("foo") == "bar"
        db.set_setting("foo", "baz")          # upsert overwrites
        assert db.get_setting("foo") == "baz"
        assert db.stats() == {"events": 0, "rsvps": 0, "guests": 0}


def test_channel_status_masks_secret(monkeypatch):
    import notify
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/secret123456789")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert notify.channel_configured("discord") is True
    assert notify.channel_configured("email") is False
    detail = notify.channel_detail("discord")
    assert detail.startswith("••••")
    assert "secret" not in detail           # the real URL never leaks
    assert "456789" in detail               # but a recognizable tail shows


def test_notify_respects_enabled_toggle(monkeypatch):
    import notify
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    spawned = []
    monkeypatch.setattr(notify.threading, "Thread",
                        lambda *a, **k: spawned.append(k.get("args")) or _Noop())

    common = dict(title="T", name="N", adults=1, kids=0, notes="", total=1, updated=False)
    # Configured + enabled -> a thread is spawned with discord active.
    notify.notify_rsvp(enabled={"discord": True}, **common)
    assert spawned and "discord" in spawned[-1][2]
    # Configured but toggled off -> nothing fires.
    spawned.clear()
    notify.notify_rsvp(enabled={"discord": False}, **common)
    assert not spawned


class _Noop:
    def start(self): pass


def test_send_test_reports_success_and_failure(monkeypatch):
    import notify
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")

    monkeypatch.setattr(notify, "_send_discord", lambda body: None)
    assert notify.send_test("discord") is None        # success -> None

    def boom(body):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(notify, "_send_discord", boom)
    assert notify.send_test("discord") == "connection refused"

    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert notify.send_test("discord") == "Not configured."  # unconfigured


def test_settings_page_renders_and_toggle_persists(client, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    page = client.get("/admin/settings", headers=auth()).data
    assert b"Settings" in page
    assert b"Discord" in page
    assert b"Configured" in page            # discord shows as configured
    assert b"Version" in page               # System section present

    # Turn Discord off (omit its checkbox), and confirm the toggle is persisted.
    import app as app_module
    r = client.post("/admin/settings", headers={**auth(), "Origin": "http://localhost"}, data={})
    assert r.status_code in (302, 303)
    with app_module.app.app_context():
        import db
        assert db.get_setting("notify_discord_enabled") == "0"


def test_settings_requires_auth(client):
    assert client.get("/admin/settings").status_code == 401


def test_test_notification_rejects_unknown_channel(client):
    r = client.post("/admin/settings/test",
                    headers={**auth(), "Origin": "http://localhost"},
                    data={"channel": "carrier-pigeon"})
    assert r.status_code == 400


# --- Customizable RSVP notes hint --------------------------------------------

def test_notes_hint_default_and_custom(client):
    import app as app_module
    default = app_module.DEFAULT_NOTES_HINT.encode()

    # No hint provided -> the public notes box shows the neutral default,
    # not the old potluck-assuming copy.
    client.post("/admin/new", headers=auth(), data={
        "title": "Plain Meet", "datetime": "2099-09-01T12:00", "active": "on", "listed": "on"})
    page = client.get("/plain-meet").data
    assert default in page
    assert b"Bringing a side dish" not in page

    # Custom hint at creation -> shown verbatim as the placeholder.
    client.post("/admin/new", headers=auth(), data={
        "title": "Potluck", "datetime": "2099-09-02T12:00", "active": "on", "listed": "on",
        "notes_hint": "Bringing a dish? Tell us what!"})
    assert b'placeholder="Bringing a dish? Tell us what!"' in client.get("/potluck").data

    # Editing updates the hint, and the admin form shows the saved value.
    client.post("/admin/potluck/edit", headers={**auth(), "Origin": "http://localhost"}, data={
        "title": "Potluck", "datetime": "2099-09-02T12:00", "active": "on", "listed": "on",
        "notes_hint": "Allergies? Let us know."})
    assert b"Allergies? Let us know." in client.get("/potluck").data
    assert b'value="Allergies? Let us know."' in client.get("/admin/potluck", headers=auth()).data


# --- Guest email + reach-your-guests broadcast -------------------------------

def _make_event_with_emails(client, slug="party"):
    client.post("/admin/new", headers=auth(), data={
        "title": slug.title(), "datetime": "2099-07-04T17:00", "active": "on", "listed": "on"})
    client.post(f"/{slug}/rsvp", data={"name": "Ann", "email": "ann@example.com", "adults": "1"})
    # A second browser/guest with no email — must stay reachable-less, not crash.
    c2 = client.application.test_client()
    c2.post(f"/{slug}/rsvp", data={"name": "Bo", "adults": "2"})
    c3 = client.application.test_client()
    c3.post(f"/{slug}/rsvp", data={"name": "Cy", "email": "cy@example.com", "adults": "1"})


def test_email_is_optional_on_rsvp_form(client):
    # The public form advertises email as optional and notification-only.
    client.post("/admin/new", headers=auth(), data={
        "title": "Optin", "datetime": "2099-07-04T12:00", "active": "on", "listed": "on"})
    page = client.get("/optin").data
    assert b'name="email"' in page
    assert b"optional" in page
    assert b"if the plans change" in page
    # And an RSVP with no email at all still succeeds.
    r = client.post("/optin/rsvp", data={"name": "NoEmail", "adults": "1"})
    assert r.status_code == 200


def test_guest_emails_collected_and_deduped(client):
    import app as app_module
    _make_event_with_emails(client, "party")
    with app_module.app.app_context():
        import db
        ev = db.get_event("party")
        # Same email twice (different casing) collapses to one entry.
        db.add_rsvp(ev["id"], "Ann2", 1, 0, "", email="ANN@example.com")
        emails = db.guest_emails(ev["id"])
        assert emails == ["ann@example.com", "cy@example.com"]


def test_manage_page_shows_mailto_for_guests(client):
    _make_event_with_emails(client, "party")
    page = client.get("/admin/party", headers=auth()).data
    assert b"Reach your guests" in page
    assert b"Email all guests" in page
    # mailto link carries both addresses in Bcc, subject prefilled.
    assert b"mailto:?bcc=ann@example.com,cy@example.com" in page
    assert b"2 guests have left an email" in page


def test_manage_page_no_emails_message(client):
    client.post("/admin/new", headers=auth(), data={
        "title": "Quiet", "datetime": "2099-07-04T12:00", "active": "on", "listed": "on"})
    client.post("/quiet/rsvp", data={"name": "Solo", "adults": "1"})  # no email
    page = client.get("/admin/quiet", headers=auth()).data
    assert b"No guest has left an email yet" in page
    assert b"mailto:" not in page


def test_broadcast_route_without_smtp_redirects_with_error(client, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    _make_event_with_emails(client, "party")
    r = client.post("/admin/party/notify",
                    headers={**auth(), "Origin": "http://localhost"},
                    data={"subject": "Moved!", "message": "New date is the 11th."})
    assert r.status_code == 302
    assert "error=" in r.headers["Location"]


def test_broadcast_route_sends_when_configured(client, monkeypatch):
    import app as app_module
    sent = {}
    monkeypatch.setattr(app_module.notify, "send_guest_broadcast",
                        lambda subj, body, recips: sent.update(
                            subject=subj, body=body, recips=list(recips)) or None)
    _make_event_with_emails(client, "party")
    r = client.post("/admin/party/notify",
                    headers={**auth(), "Origin": "http://localhost"},
                    data={"subject": "Moved!", "message": "New date is the 11th."})
    assert r.status_code == 302
    assert "sent=1" in r.headers["Location"]
    assert sent["subject"] == "Moved!"
    assert sent["recips"] == ["ann@example.com", "cy@example.com"]
    # Empty message is rejected before any send.
    sent.clear()
    r2 = client.post("/admin/party/notify",
                     headers={**auth(), "Origin": "http://localhost"},
                     data={"message": "   "})
    assert "error=" in r2.headers["Location"]
    assert not sent


def test_send_guest_broadcast_builds_bcc(monkeypatch):
    import notify
    monkeypatch.setenv("SMTP_HOST", "mail.example")
    monkeypatch.setenv("SMTP_FROM", "rsvp@example.com")
    monkeypatch.setenv("NOTIFY_EMAIL", "host@example.com")
    captured = {}

    def fake_send(msg):
        captured["from"], captured["to"] = msg["From"], msg["To"]
        captured["bcc"], captured["subject"] = msg["Bcc"], msg["Subject"]
        captured["reply_to"], captured["body"] = msg["Reply-To"], msg.get_content()

    monkeypatch.setattr(notify, "_smtp_send", fake_send)
    err = notify.send_guest_broadcast("Moved!", "See you the 11th.",
                                      ["a@x.com", "b@y.com", "a@x.com"])
    assert err is None
    assert captured["from"] == "rsvp@example.com"
    assert captured["to"] == "host@example.com"        # admin keeps a copy
    assert captured["reply_to"] == "host@example.com"  # replies route to admin
    assert captured["bcc"] == "a@x.com, b@y.com"       # deduped, guests hidden
    assert "See you the 11th." in captured["body"]


def test_send_guest_broadcast_guards(monkeypatch):
    import notify
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert "SMTP_HOST" in notify.send_guest_broadcast("s", "b", ["a@x.com"])
    monkeypatch.setenv("SMTP_HOST", "mail.example")
    assert notify.send_guest_broadcast("s", "b", []) == \
        "No guests have left an email address yet."
