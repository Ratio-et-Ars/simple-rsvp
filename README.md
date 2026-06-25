# Simple RSVP App

<img src="assets/header.png" alt="Simple RSVP" width="300">

A lightweight, self-hosted RSVP tracker for events like family feasts, parties,
or community gatherings. Run one instance and host **many events**, each at its
own URL (`/cigar-club`, `/summer-feast`, …).

- 🎟️ **Multiple events**, each addressed by a slug
- 🏠 Public home page lists your **highlighted** events; unlisted events stay
  private — reachable only by their link
- ✉️ Collects RSVPs with names, adult/kid counts, notes, and an **optional** email
- 📣 Reach every guest who left an email — a one-click BCC draft in your own mail
  app, or a server-sent broadcast (when SMTP is configured) for reschedules & updates
- 🧑‍💻 Admin dashboard to create/edit events, edit the guest list, and export CSV
- 📸 Per-event cover image
- 🔒 Admin protected by HTTP Basic Auth
- 🗄️ SQLite storage, no external services, no CDN
- 📦 Docker-ready and volume-persistent
- 💡 Mobile-friendly, self-contained theme

---

## 🚀 Quickstart

### Docker (recommended)

`ADMIN_PASSWORD` is **required** — the app refuses to start without it.

```bash
export ADMIN_PASSWORD="choose-a-strong-password"
docker compose up -d
```

Then open <http://localhost:8080>. The admin dashboard is at `/admin`
(user `admin`, password from `ADMIN_PASSWORD`).

Or use the prebuilt image:

```bash
docker run -d -p 8080:3022 \
  -e ADMIN_PASSWORD=change-me \
  -v rsvp_data:/app/data \
  ghcr.io/ratio-et-ars/simple-rsvp:latest
```

### Local (development)

```bash
pip install -r requirements.txt
export ADMIN_PASSWORD="choose-a-strong-password"   # required
python app.py            # http://localhost:3022
```

---

## ⚙️ Configuration

| Env var          | Default   | Purpose                                  |
| ---------------- | --------- | ---------------------------------------- |
| `ADMIN_PASSWORD` | _(none)_  | **Required.** Password for the `admin` user; the app exits at startup if unset. |
| `PORT`           | `3022`    | Port the app listens on.                 |
| `DATA_DIR`       | `data`    | Where `rsvp.db` and uploaded covers live (the Docker volume). |
| `FLASK_DEBUG`    | _off_     | Set to `1` to enable Flask debug mode (local dev only). |

### 🔔 RSVP notifications (optional)

Get a ping when someone RSVPs. Endpoints and credentials are configured with the
env vars below — **secrets stay in the environment, never in the database.** The
admin **Settings** page (`/admin/settings`) shows which channels are configured,
lets you switch each one on/off, and has a *Send test* button. A channel fires
only when it's both configured *and* enabled. Delivery is best-effort — a broken
endpoint never affects the guest. Stdlib only.

| Env var               | Purpose                                                        |
| --------------------- | -------------------------------------------------------------- |
| `DISCORD_WEBHOOK_URL` | Discord channel webhook URL — posts a message on each RSVP.    |
| `SMTP_HOST`           | Mail server host. Enables email (with `NOTIFY_EMAIL`).         |
| `NOTIFY_EMAIL`        | Recipient address for email notifications.                     |
| `SMTP_PORT`           | Mail server port (default `587`).                              |
| `SMTP_USER` / `SMTP_PASSWORD` | SMTP login, if your server requires auth.              |
| `SMTP_FROM`           | From address (defaults to `SMTP_USER`).                        |
| `SMTP_STARTTLS`       | STARTTLS on by default; set to `0` to disable.                 |

To get a Discord webhook: **Server Settings → Integrations → Webhooks → New Webhook**,
pick a channel, **Copy Webhook URL**. (Prefer phone push? ntfy.sh is a ~10-line add in
`notify.py`.)

### 📣 Reaching your guests (reschedules & updates)

RSVPs collect an **optional** email — used for nothing but reaching that guest if
plans change. Each event's manage page (`/admin/<slug>`) has a **Reach your guests**
section with two ways to message everyone who left one:

- **Email all guests** — opens a draft in *your own* mail app with every address in
  BCC (hidden from each other). Works with zero configuration.
- **Send from the server** — appears when `SMTP_HOST` is set; writes a subject +
  message and the server sends it (guests BCC'd, replies routed to `NOTIFY_EMAIL`).

So the same SMTP settings above power both admin notifications *and* outbound guest
broadcasts. Guests who skip the email field simply aren't reachable this way.

---

## 📝 Creating an event

1. Go to `/admin` and sign in.
2. **New Event** → give it a title, date/time, and (optionally) a custom slug.
3. Tick **Show on the public home page** to feature it; leave it unticked for a
   private, link-only event.
4. Share the event link: `https://your-host/<slug>`.

Existing single-event installs are migrated automatically on first run — your
old event and its RSVPs become the first event in the database.

---

## 🧪 Tests

```bash
pip install pytest
pytest
```

---

See [CONTRIBUTING.md](./CONTRIBUTING.md). Pax et bonum.
