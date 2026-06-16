# Dashboard + Database — Setup Guide

This adds a password-protected **dashboard** (leads table + control panel) and a
**Postgres database** so you no longer edit files to change API keys, formats, or
profiles. Everything is managed in the browser and stays in sync with the webhooks.

You run the terminal/Railway steps; I can't push to git or touch Railway from here.
If any command errors, paste the output back and I'll fix it.

---

## What changed in the code

- `db.py` — new. Database (Postgres in production, SQLite locally for preview).
- `dashboard.py` — new. The web dashboard (`/dashboard`), password protected.
- `main.py` — webhooks now read config from the database and write a lead row for
  every reply (so the dashboard fills up automatically).
- `requirements.txt` — added SQLAlchemy, psycopg2-binary, python-multipart.

Your existing `config.json`, `client_profiles/`, and `reply_formats/` are **imported
into the database automatically on first run**, so nothing is lost.

---

## Step 1 — (Optional) Preview on your Mac first

This lets you see the dashboard before deploying. It uses a temporary local
database file, so it won't touch anything real.

Run these lines one at a time (don't paste the `#` comment lines — your terminal
will try to run them):

```bash
cd ~/Desktop/reply\ management
python3 -m pip install sqlalchemy python-multipart
DASHBOARD_PASSWORD=test1234 python3 -m uvicorn main:app
```

Then open **http://127.0.0.1:8000/dashboard** in your browser.
Username `admin`, password `test1234`. Press `Ctrl + C` in the terminal to stop.

> Note: for local preview you only need `sqlalchemy` and `python-multipart`
> (it uses a local SQLite file, so `psycopg2-binary` isn't required here — Railway
> installs everything from `requirements.txt` automatically).
> If you ever see `no such option: --break-system-packages`, your pip is older —
> just leave that flag off, as above.

---

## Step 2 — Commit and push

```bash
cd ~/Desktop/reply\ management

python3 -m py_compile main.py db.py dashboard.py   # quick sanity check

git add .
git commit -m "Add Postgres + dashboard (leads table + control panel)"
git push
```

---

## Step 3 — Add a Postgres database on Railway

1. Open your project on **railway.app**.
2. Click **+ New** (or **Create**) → **Database** → **Add PostgreSQL**.
3. Railway creates the database and automatically gives your app a `DATABASE_URL`.
   You don't need to copy it manually — Railway injects it.

> If your app and database are in the same Railway project, `DATABASE_URL` is shared
> automatically. If not, go to your app's **Variables** and add a reference to the
> Postgres `DATABASE_URL`.

---

## Step 4 — Set the dashboard password (and check keys)

In Railway, open your **app service → Variables** and add:

| Variable | Value | Required |
|---|---|---|
| `DASHBOARD_PASSWORD` | a strong password you choose | **Yes** |
| `DASHBOARD_USER` | a username (defaults to `admin` if you skip it) | Optional |
| `OPENAI_API_KEY` | your OpenAI key | Yes (or paste it in Settings later) |
| `EMAILBISON_BASE_URL` | your Bison base URL | Recommended |

Your existing Bison/Instantly key variables (e.g. `BISON_KEY_ASCENDLY`) can stay —
they'll be imported automatically the first time. After that you manage keys in the
dashboard.

Railway redeploys automatically after you push and after variable changes.

---

## Step 5 — Open the dashboard

Go to: `https://YOUR-APP.up.railway.app/dashboard`

Log in with `DASHBOARD_USER` / `DASHBOARD_PASSWORD`.

You'll see three tabs:

- **Leads** — every processed reply, with **Reply** (Replied / Added, not sent / Not
  sent) and **FUP** (Added / No) status columns, plus filters.
- **Workspaces** — add/edit each client: API key, base URL, follow-up campaign ID,
  website, sender name, and the client profile + reply format (as JSON boxes).
- **Settings** — OpenAI key, human-review webhook, default Bison base URL, reply delay.

---

## Step 6 — First-time check inside the dashboard

1. Open **Workspaces**. Your 4 existing clients should already be there.
2. Click each one and confirm the **API key**, **Base URL** (Bison), and **Follow-up
   campaign ID** are filled in. Fill any blanks and **Save**.
   - Webaholics: set the follow-up campaign ID (it was `TO_BE_ADDED`).
3. Open **Settings**, confirm the OpenAI key is set, and set the reply delay if you
   want something other than 420 seconds.

That's it — no webhook changes needed. The Make.com / Instantly webhooks still point
at `/bison-reply` and `/instantly-reply` as before.

---

## Notes

- The leads table fills automatically as new replies come in. Old replies processed
  before this deploy won't appear (they were never recorded).
- Auto-send vs. skip+enrich logic is unchanged from the previous update: simple
  positives get an auto-reply; everything else is enriched + pushed to follow-up for
  you to answer manually.
- Keep `DASHBOARD_PASSWORD` private — the dashboard is on a public URL.
