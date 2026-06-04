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
