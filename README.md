# Simple RSVP App

<img src="assets/header.png" alt="Simple RSVP" width="300">

A lightweight, self-hosted RSVP tracker for events like family feasts, parties,
or community gatherings. Run one instance and host **many events**, each at its
own URL (`/cigar-club`, `/summer-feast`, …).

- 🎟️ **Multiple events**, each addressed by a slug
- 🏠 Public home page lists your **highlighted** events; unlisted events stay
  private — reachable only by their link
- ✉️ Collects RSVPs with names, adult/kid counts, and notes
- 🧑‍💻 Admin dashboard to create/edit events, edit the guest list, and export CSV
- 📸 Per-event cover image
- 🔒 Admin protected by HTTP Basic Auth
- 🗄️ SQLite storage, no external services, no CDN
- 📦 Docker-ready and volume-persistent
- 💡 Mobile-friendly, self-contained theme

---

## 🚀 Quickstart

### Docker (recommended)

```bash
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
python app.py            # http://localhost:3022
```

---

## ⚙️ Configuration

| Env var          | Default   | Purpose                                  |
| ---------------- | --------- | ---------------------------------------- |
| `ADMIN_PASSWORD` | `letmein` | Password for the `admin` user. **Change it.** |
| `PORT`           | `3022`    | Port the app listens on.                 |
| `DATA_DIR`       | `data`    | Where `rsvp.db` and uploaded covers live (the Docker volume). |
| `FLASK_DEBUG`    | _off_     | Set to `1` to enable Flask debug mode (local dev only). |

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
