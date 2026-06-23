# Reply Management System — Developer Handoff & Technical Documentation

_Last updated: 2026-06-22_

This document explains the entire Reply Management system end to end: what it does, how it's
built, every recent change, and concrete optimization opportunities. It's written so a developer
new to the codebase can understand each step and improve it.

---

## 1. What the system does (in one paragraph)

Cold-email campaigns (run in **EmailBison** and **Instantly.ai**) generate replies from prospects.
When a prospect replies positively, this system: (1) receives a webhook, (2) reads the full email
thread, (3) uses OpenAI to classify the reply and draft a human-sounding response plus a sequence
of follow-ups, (4) **auto-sends** the reply only when it's a simple positive ("yes, interested,
tell me more"), otherwise flags it for a human, (5) writes the follow-up drafts back as campaign
variables so the email platform can drip them out, and (6) surfaces everything in a
password-protected dashboard. It now also proposes **real, timezone-correct Calendly times** and
avoids pitching the same slot to multiple prospects.

---

## 2. Architecture & stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI + Uvicorn |
| Hosting | Railway (`reply.ascendly.one`) |
| Database | PostgreSQL in prod (Railway `DATABASE_URL`), SQLite fallback locally |
| ORM | SQLAlchemy 2.0 (`psycopg2-binary` driver) |
| AI | OpenAI `gpt-4.1` via `chat.completions`, `temperature=0.6`, JSON output |
| Scheduling data | Calendly API v2 (Personal Access Token per client) |
| Async work | FastAPI `BackgroundTasks` + a configurable delay |

**Key environment variables**
- `DATABASE_URL` — Postgres connection string (Railway injects `${{Postgres.DATABASE_URL}}`).
  The legacy `postgres://` scheme is auto-normalized to `postgresql://`.
- OpenAI key, Bison/Instantly keys, base URLs, reply delay, and trigger tags are stored in the
  **database** (the `app_settings` table and per-workspace rows), not in env vars — so they can be
  edited from the dashboard without redeploying.

**Startup logs** print `DB backend: POSTGRES` or `SQLITE` so you can confirm the prod app is
actually connected to Postgres (a past bug: the web service had no `DATABASE_URL`, so data reset on
every deploy).

---

## 3. File map

| File | Responsibility |
|---|---|
| `main.py` | All webhook endpoints, AI generation, Bison/Instantly API calls, reply logic, Calendly integration. |
| `db.py` | SQLAlchemy models, migrations, and all DB helper functions. |
| `dashboard.py` | Server-rendered HTML dashboard (light "Sabrosso/Stitch" theme), CRUD, live send. |
| `requirements.txt` | Pinned dependencies. |
| `reply_formats/*.json` | Per-client reply + follow-up templates (Ascendly, Webaholics, etc.). |
| `logs/` | `processed_replies` dedupe file and saved payloads (local/file-based). |

---

## 4. Data model (`db.py`)

### `workspaces`
One row per client × flow. A single client can have two workspaces sharing the same API key:
one in `reply` mode and one in `followup` mode.

Important columns:
- `name`, `platform` (`bison`/`instantly`), `mode` (`reply`/`followup`), `active`
- `api_key`, `base_url` (Bison), `reply_followup_campaign_id`
- `website`, `sender_name`, `default_sender_email`
- `calendly_token`, `calendly_scheduling_url` — **added for the Calendly feature**
- `client_profile` (JSON), `reply_format` (JSON)

### `app_settings`
Simple key/value store: OpenAI key, base URLs, `reply_delay_seconds`, human-review webhook,
`reply_trigger_tag` (default "Interested"), `followup_trigger_tag` (default "Begin follow-up").

### `leads`
One row per processed reply (`dedupe_key = platform:external_lead_id:(reply_id or email)`). Holds
intent, confidence, action, `replied`, `fup_added`, `reply_added`, `reviewed`, `stage`
(new/replied/booked/won/lost/stopped), the prospect's reply, our `main_reply`, follow-ups JSON,
thread JSON, lead_data JSON, and `send_meta` (everything needed to re-send from the dashboard).

### `proposed_slots` — **NEW (slot reservation)**
One row per real Calendly time we've proposed to a prospect, so the same upcoming slot is never
pitched to two people.
- `workspace_name`, `prospect` (email/id), `slot_utc` (ISO-8601 UTC, the unique key), `label`,
  `created_at`.
- Helpers: `get_reserved_slot_keys(workspace, now_iso)` (also prunes past slots),
  `reserve_slots(workspace, prospect, items)`.

**Migrations:** `migrate()` runs on every startup and `ALTER TABLE`s any missing columns (safe on
both Postgres and SQLite). New tables are created by `init_db()` → `Base.metadata.create_all`.

---

## 5. Reply pipeline (Bison) — `process_reply(lead_id, workspace_name)`

1. **Delay** `reply_delay_seconds` (BackgroundTask sleep; 15s in prod testing, 420s default).
2. Load workspace config from DB; bail if missing key/base URL.
3. **Follow-up-campaign guard (NEW — see §8).** In `reply` mode, if the lead was already handled,
   stop.
4. Fetch replies (`/api/leads/{id}/replies`); keep only `interested == true` and
   `automated_reply == false`.
5. Dedupe by `reply_id` against the `processed_replies` file (skipped in follow-up mode).
6. Fetch sent emails (`/api/leads/{id}/sent-emails`); **secondary campaign-id guard** (§8).
7. Build the thread (sent + replies, oldest→newest).
8. **Calendly scheduling context (NEW — §7)** if the workspace has a token.
9. `generate_ai_reply(...)` → intent + `main_reply` + `followup_1..6`.
10. `decide_reply_action(...)` → `send` / `skip_enrich` / `stop`.
11. If `send`: send via Bison reply API. If `skip_enrich`: don't send, but still enrich. If `stop`:
    do nothing.
12. `update_bison_lead_variables(...)` writes `main_reply`, `followup_1..N`, `reply_intent`,
    `reply_confidence` as custom variables (with the resilient retry below).
13. `attach_lead_to_followup_campaign(...)` pushes the lead into the drip campaign.
14. `record_lead(...)` upserts the dashboard row.

**Resilient variable write:** Bison rejects the *entire* update if any custom variable doesn't
exist on the account. `update_bison_lead_variables` parses the error
(`re.findall(r"custom variable named\s+([\w\-]+)", text)`), drops the unknown variable(s), and
retries up to 4× — so one missing variable doesn't wipe all of them.

---

## 6. Reply pipeline (Instantly) — `process_instantly_reply(payload, workspace_name)`

Instantly differs from Bison:
- `find_instantly_lead(email, campaign_id)` tries **every active Instantly workspace's API key** so
  multi-account routing is automatic.
- `find_instantly_reply_target(email, campaign_id, api_key)` returns `(reply_to_uuid, eaccount)`:
  it replies to the **prospect's inbound email** (so the reply goes to the prospect, not ourselves)
  and uses that email's `eaccount` as the sending mailbox.
- `build_reply_quote(payload)` builds a Gmail-style quoted thread
  ("On {when}, {name} <{email}> wrote:" + blockquote) so replies look natural.
- Sender name/email are derived from the **eaccount**, never parsed from the prospect's reply text
  (which caused a "From:" artifact bug — fixed in `_clean_name_token`).
- Follow-ups are delivered by setting `lead_label = "FUP1"` (triggers the subsequence) plus writing
  `followup_*` custom variables.
- The **same follow-up-campaign guard (§8)** is applied here too.

### AI generation — `generate_ai_reply(client_profile, reply_format, thread, mode, scheduling_context)`
- `mode == "reply"`: a long, carefully tuned prompt that reads the prospect's energy, matches tone,
  handles out-of-context questions, forbids sign-offs (the system appends the signature), and
  returns strict JSON (`intent`, `confidence`, `human_review_needed`, `main_reply`,
  `followup_1..6`).
- `mode == "followup"`: `_followup_prompt(...)` + `FOLLOWUP_SYSTEM` — continues an existing thread
  from where it left off (used to catch up a backlog of un-followed-up leads).
- `_call_openai(prompt, system)` retries 3× and returns a safe fallback dict (intent
  `human_review`) on failure.

### Decision logic — `decide_reply_action(ai_result)`
- `STOP_INTENTS` / `STOP_KEYWORDS` → `stop` (unsubscribe, out-of-office, wrong person).
- `AUTO_SEND_INTENTS` (incl. `simple_positive`) / `SEND_KEYWORDS` → `send`.
- Everything else (pricing, objections, case-study requests, etc.) → `skip_enrich` (a human
  replies, but we still enrich + drip follow-ups).

---

## 7. Calendly integration (NEW)

**Goal:** propose meeting times that are (a) actually open on the client's calendar, (b) in the
**prospect's** timezone, (c) within business hours, and (d) **never the same slot pitched to two
prospects**.

### Per-workspace setup
Each client workspace stores a **Calendly Personal Access Token** and a **scheduling link**
(dashboard → Workspaces → Calendly fields). Required token scopes (read-only, cannot book):
`event_types:read` + `availability:read` (and a user-read scope if shown). No `:write` scopes.

### Flow (in `main.py`)
1. `timezone_from_location(location)` — maps a prospect's location string to an IANA timezone
   (US state map first, then country map; falls back to `America/New_York`). Location comes from
   Bison lead custom variables (`fetch_bison_lead_location`) or the Instantly webhook payload
   (`extract_location_from_data`).
2. `resolve_calendly_event_type(token, scheduling_url)` — `GET /users/me` then `GET /event_types`;
   matches the event type by the scheduling-link slug, else takes the first.
3. `get_calendly_slots(token, url, prospect_tz, count, exclude)` —
   `GET /event_type_available_times` (max 7-day window, starting 2 days out), converts each UTC
   slot to the prospect's timezone, keeps **weekdays in the 10 AM–2 PM** window (relaxes to 9–5 if
   nothing fits), **excludes already-reserved UTC keys**, and picks one slot per distinct day.
   Returns `[{"utc": iso, "label": "Tuesday, Jun 23 at 11:00 AM PDT"}]`.
4. `build_scheduling_context(workspace, location, prospect_key, mode)` —
   pulls reserved keys from `proposed_slots`, fetches unreserved slots (`count=3` for reply,
   `count=6` for follow-up), **reserves the chosen ones**, and returns a prompt block instructing
   the AI to propose **only** those times + the booking link. Follow-up mode is told to use a
   *different* real time in each follow-up.
5. The context string is injected into both the reply and follow-up prompts.

### Graceful degradation
No token, no slots, or any Calendly error → `build_scheduling_context` returns `""` and the reply
is generated exactly as before. If every slot is reserved, it falls back to the open pool (a slight
overlap beats proposing nothing). The system **only reads** times; the prospect books themselves
via the link — it never creates or confirms a booking.

### Anti-double-booking (the key request)
Because each proposed time is written to `proposed_slots` and excluded for the next prospect,
prospect 1 gets Tuesday, prospect 2 Wednesday, prospect 3 Thursday — verified zero overlap in
testing. Past slots auto-prune, freeing them up again.

---

## 8. Follow-up-campaign reply guard (NEW)

**Problem:** After we reply and push a lead into the follow-up campaign, the campaign drips
follow-ups. When the prospect replies to one of those, Bison marks them "interested" again, which
re-triggered our webhook and caused a **second auto-reply**. A human should handle an
already-engaged lead.

**Fix — two layers, both in `process_reply` and `process_instantly_reply` (reply mode only):**

1. **Primary (DB, reliable):** `db.lead_already_handled(workspace_name, external_lead_id)` returns
   true if any prior lead row for that workspace+lead has `replied`, `fup_added`, or `reply_added`.
   If handled, we skip — the only way a handled lead replies again as "interested" is from inside
   the follow-up campaign.
2. **Backstop (campaign-id scan):** `_scan_for_campaign_id(obj, target)` recursively looks for any
   field named like `*campaign*` whose value equals `reply_followup_campaign_id` in the lead's
   sent-emails/replies. Catches the case even if the DB was reset. Uses the campaign ID already
   configured per workspace.

**Follow-up *mode* workspaces are exempt** (they're designed to re-run on past leads).

---

## 9. Dashboard (`dashboard.py`)

Server-rendered HTML, light grouped sidebar (All Replies / Needs Review / Replied / Meeting Booked
/ Stopped with live count badges, plus Workspaces / Settings / Support). Features:
- **Performance overview** stat cards (clickable filters).
- Leads table with avatar, intent dot, stage/reply/review pills, and an eye-icon drawer.
- **Drawer** (`build_panel`): lead details, stage select, collapsible conversation + follow-ups,
  an editable "Reply to send" box with **Approve & Send** (live send via Bison/Instantly), Save
  draft, Mark reviewed, Done, Stop, Delete.
- Bulk actions (mark booked/reviewed/stop/export CSV/delete), pagination, auto-applying filters.
- Workspace CRUD form now includes the two **Calendly fields**.
- Settings page exposes `reply_trigger_tag` + `followup_trigger_tag`.

Endpoints: `/dashboard`, `/dashboard/switch`, `/dashboard/leads/{id}/panel`,
`/dashboard/leads/{id}/action`, `/dashboard/leads/{id}/send`, `/dashboard/bulk`,
`/dashboard/export`, workspaces CRUD, `/dashboard/settings`, `/dashboard/logout`. `/` redirects to
`/dashboard`; `/healthz` for health checks.

---

## 10. Webhook routing reference

### Bison — `POST /bison-reply-webhook` (native events)
Query params select the workspace and flow:
`?reply_workspace=<name>&fup_workspace=<name>` (one combined webhook avoids double-sends).
- `TAG_ATTACHED`: routes by `data.tag_name`. Tag == `reply_trigger_tag` → reply flow; tag ==
  `followup_trigger_tag` → follow-up flow. Lead id from `data.taggable_id`.
- `LEAD_INTERESTED` / other `BISON_REPLY_EVENTS`: processed only if a `lead_id` is found
  (`deep_find_lead_id` searches the nested payload).

### Instantly — `POST /instantly-reply-webhook`
Reads `?workspace_name=` then `resolve_instantly_workspace(payload)`; `find_instantly_lead`
auto-detects the owning account by API key.

---

## 11. Known issues / things the developer should optimize

1. **OpenAI quota is a hard dependency.** When the OpenAI account hits `insufficient_quota` (429),
   all 3 attempts fail → fallback → nothing is drafted. This is account/billing, not code, but
   consider alerting (e.g., post to the human-review webhook) when generation fails so it's visible.
2. **`reply_delay_seconds` uses an in-memory `BackgroundTasks` sleep.** Fine for seconds/minutes,
   but a redeploy/restart drops in-flight jobs. For anything multi-hour (see §12), move to a
   DB-backed queue + scheduler.
3. **`processed_replies` is a local JSON file (`logs/`).** On Railway's ephemeral filesystem this
   resets on deploy. Consider moving dedupe into the `leads` table (we already store `reply_id`).
4. **Timezone map is hand-maintained.** Good enough, but a library (e.g., resolving via a geocoder)
   would cover edge cases (multi-zone states, cities). Current fallback is Eastern.
5. **Calendly event-type matching** falls back to "first event type" if the slug doesn't match.
   If a client has multiple event types, make the target event type explicit per workspace.
6. **Slot reservation never expires except by slot time.** If a proposed time is declined/ignored,
   it stays reserved until it passes. Consider releasing reservations after N days of no booking so
   popular slots get reused.
7. **Secrets hygiene.** API keys live in the DB and have appeared in screenshots/chat during setup.
   Rotate any exposed keys; consider encrypting `api_key`/`calendly_token` at rest.
8. **`_scan_for_campaign_id` is heuristic** (matches any `*campaign*` field). The DB guard is the
   reliable layer; if Bison exposes a documented campaign field on replies, prefer reading it
   explicitly.

---

## 12. Proposed (NOT yet built): same-day follow-up

**Goal:** after the initial reply is sent, if the prospect hasn't confirmed a time by end of day,
send one same-day nudge ("Hey {{FirstName}}, wrapping up my day and saw you haven't locked a time
yet…"). Bison's campaign only fires day-1; most conversions come from same-day touches.

**Design under discussion:**
- **Storage/timing:** a `pending_followups` table with `due_at`, plus a lightweight scheduler loop
  (wakes every ~15 min) — robust across restarts (do **not** use an in-memory sleep, see §11.2).
- **When:** target ~5 PM in the **prospect's** timezone (reuse the Calendly timezone logic), or a
  fixed 5–6h delay if timezone is unknown.
- **Condition:** before sending, re-check for any new prospect reply after our initial one; if they
  replied at all, skip (a human's handling it).
- **Content:** generate the nudge **upfront** in the same OpenAI call as the reply (no second call),
  store it, send later on the **same email thread** (reply API, not the campaign).
- **Open decisions:** 5 PM prospect-local vs fixed delay; one nudge vs per-workspace configurable
  cadence.

---

## 13. How to deploy

```bash
cd ~/Desktop/"reply management"
rm -f .git/index.lock        # only if a stale lock exists
git add -A
git commit -m "..."
git push                      # Railway auto-deploys
```

New DB tables/columns are created automatically on startup (`init_db()` + `migrate()`).
After deploy: hard-refresh the dashboard; set Calendly tokens per workspace; send a test reply and
watch the logs for `DB backend: POSTGRES`, the location guess, and "injected real open times".
