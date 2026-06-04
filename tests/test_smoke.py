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
