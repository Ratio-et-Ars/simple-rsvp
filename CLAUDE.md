# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted, multi-event RSVP tracker. One instance hosts many events, each at
`/<slug>`. The public home page (`/`) lists every event that is **listed AND active**
(upcoming first, then past events most-recent-first); the `listed` toggle is the
only curation control. Unlisted events stay reachable by their slug URL but
never appear there — that is the entire privacy model (no per-event password).

Read `CONTRIBUTING.md`: simplicity is treated as a hard product constraint
("If it feels like a platform, it's too much"). Stay Flask + SQLite + server-rendered
templates. Don't add a JS framework, an ORM, or external services.

## Commands

```bash
python app.py            # local dev server on :3022 (set FLASK_DEBUG=1 for debug)
pip install pytest && pytest   # run the smoke tests
docker compose up -d     # build + run, maps host 8080 -> container 3022
```

There is no linter. Verify behavior with `pytest` and by running the app.

## Configuration (env vars)

- `ADMIN_PASSWORD` (default `letmein`) — password for the hardcoded `admin` Basic-Auth user.
- `PORT` (default `3022`).
- `DATA_DIR` (default `data`) — holds `rsvp.db` and `uploads/`; this is the Docker volume.
- `FLASK_DEBUG` — `1` enables debug mode (dev only; off by default, unlike the old app).
- **RSVP notifications** (all optional; unset = that channel off): `DISCORD_WEBHOOK_URL`
  pings a Discord channel webhook. Email needs `SMTP_HOST` + `NOTIFY_EMAIL` (plus optional
  `SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`/`SMTP_FROM`/`SMTP_STARTTLS`). See `notify.py`.

## Architecture

Three layers, all server-rendered:

- **`db.py`** — SQLite data layer. Schema + all queries live here. Connections are
  per-request via Flask's `g` (`get_db`/`close_db`), so it's safe under the threaded
  WSGI server. Two tables: `events` and `rsvps` (FK `ON DELETE CASCADE`). Counts are
  computed with SQL aggregates, not in Python.
- **`app.py`** — Flask routes + request helpers (`slugify`, `unique_slug`, `safe_int`,
  `format_datetime`, `event_view`, `basic_auth_required`). No HTML lives here anymore.
- **`templates/`** — Jinja2 templates extending `base.html`. Jinja autoescaping is what
  prevents the stored-XSS issue the old f-string templates had — keep user data in
  `{{ }}`, never build HTML by string concatenation.
- **`static/style.css`** — the entire theme, self-hosted (no CDN). Light/dark via
  `prefers-color-scheme`, CSS custom properties for tokens. There is no build step.
- **`notify.py`** — best-effort RSVP notifications (Discord webhook + SMTP email),
  stdlib only. `submit_rsvp` calls `notify_rsvp(...)`, which fires on a daemon thread
  and swallows all errors, so a slow/broken endpoint never blocks or breaks an RSVP.
  Channels are off unless their env var is set.

**Storage of state** (all under `DATA_DIR`, persisted by the Docker volume):
- `rsvp.db` — events and RSVPs.
- `uploads/<slug>.<ext>` — one cover image per event, resized via Pillow. Served by the
  `/cover/<slug>` route (NOT from `static/`, so covers persist across container rebuilds).

**Route groups:**
- Public: `/` (landing), `/<slug>` (event page + RSVP form), `/<slug>/rsvp` (POST),
  `/cover/<slug>`.
- Admin (`@basic_auth_required`): `/admin`, `/admin/new`, `/admin/<slug>` (manage),
  `/admin/<slug>/edit`, `/admin/<slug>/delete`, `/admin/<slug>/rsvp/<id>` (edit/delete),
  `/admin/<slug>/upload`, `/admin/<slug>/export.csv`.

## Conventions / gotchas

- Slugs are generated from the title (or an admin override), de-duplicated by
  `unique_slug`. They are **immutable** after creation — there's no rename route, because
  changing a slug would break already-shared links. The cover filename is keyed off the slug.
- Reuse `safe_int` (clamps to ≥ 0) for any count coming from a form.
- `format_datetime` avoids glibc-only `%-d`/`%-I` so it works on macOS too.
- CSV export uses the stdlib `csv` module (proper quoting) — don't hand-roll it.
- **Legacy migration**: `migrate_legacy()` runs once at startup. If a pre-SQLite
  `data/event.json` exists and the DB is empty, it imports that event + `rsvps.json` +
  any `static/cover.*`, then renames the JSON files to `*.migrated` so it never re-runs.
- The app is served by **waitress** in Docker (see `Dockerfile` CMD), not the Flask dev
  server. `python app.py` is dev-only.

## CI / releases

- `.github/workflows/ci.yml` — runs `pytest` on push/PR.
- `.github/workflows/release.yml` — builds and pushes the Docker image to **GHCR**
  (`ghcr.io/<owner>/simple-rsvp`) using the built-in `GITHUB_TOKEN` (no secrets needed):
  `:latest` on every push to `main`, and semver tags (`1.2.0`, `1.2`, `1`) when a `v*`
  git tag is pushed. Cut a release by tagging: `git tag v1.0.0 && git push --tags`.
