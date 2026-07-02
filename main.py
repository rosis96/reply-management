#!/usr/bin/env python3

import os
import json
import time
import re
import requests
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import RedirectResponse

import db
from dashboard import router as dashboard_router

load_dotenv()

app = FastAPI()


@app.on_event("startup")
def _startup():
    db.init_db()
    db.migrate()
    db.seed_if_empty()
    if db.IS_SQLITE:
        log("DB backend: SQLITE (ephemeral). On Railway this means DATABASE_URL is NOT connected -- data WILL reset on every redeploy. Connect Postgres!")
    else:
        log("DB backend: POSTGRES (persistent).")


app.include_router(dashboard_router)

DEFAULT_REPLY_DELAY_SECONDS = 420
REPLY_DELAY_SECONDS = DEFAULT_REPLY_DELAY_SECONDS  # kept for reference / fallback
PROCESSED_FILE = "logs/processed_replies.json"


def get_openai_client():
    api_key = db.get_setting("openai_api_key", "") or os.getenv("OPENAI_API_KEY", "")
    return OpenAI(api_key=api_key)


def get_reply_delay():
    raw = db.get_setting("reply_delay_seconds", str(DEFAULT_REPLY_DELAY_SECONDS))
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_REPLY_DELAY_SECONDS


def get_human_review_webhook():
    return db.get_setting("human_review_webhook_url", "") or os.getenv("HUMAN_REVIEW_WEBHOOK_URL", "")


# -------------------------
# Reply routing
# -------------------------
# Only these "simple positive" intents get an AUTO-SENT reply to the prospect.
# Everything else (case studies, pricing, proof, off-topic, objections, etc.)
# is left for a human to answer manually, but the lead is STILL enriched with
# follow-up variables and pushed into the follow-up campaign.
AUTO_SEND_INTENTS = {
    "simple_positive",
    "positive_interest",
    "positive_share_more",
    "positive_share_more_info",
    "share_more",
    "yes_interested",
    "meeting_ready",
}

# These never get a reply AND never get pushed to follow-up (opt-out / junk).
STOP_INTENTS = {
    "unsubscribe",
    "negative_not_interested",
    "not_interested",
    "out_of_office_or_automated",
    "out_of_office",
    "automated",
    "wrong_person",
}


# Keyword signals (matched as substrings, so the exact intent name the model
# picks doesn't have to match perfectly).
STOP_KEYWORDS = [
    "unsubscribe", "not_interested", "not interested", "negative",
    "out_of_office", "out of office", "ooo", "automated", "auto_reply",
    "wrong_person", "wrong person",
]
SEND_KEYWORDS = [
    "simple_positive", "positive", "interested", "yes_", "share_more",
    "share more", "meeting_ready", "ready_to_talk",
]


def _auto_send_types(reply_format):
    """If the reply_format uses the new `response_types` schema, return the set
    of type-ids marked auto_send=true. Returns None for old/legacy formats so
    the legacy keyword logic is used instead."""
    if not isinstance(reply_format, dict):
        return None
    rts = reply_format.get("response_types")
    if not isinstance(rts, list) or not rts:
        return None
    return {str(r.get("type", "")).strip().lower()
            for r in rts if isinstance(r, dict) and r.get("auto_send")}


def decide_reply_action(ai_result, reply_format=None):
    """Return one of: 'send', 'skip_enrich', 'stop'.

    send        -> auto-reply the prospect + enrich + push to follow-up
    skip_enrich -> do NOT reply (human reviews/sends) but still enrich + push to follow-up
    stop        -> do nothing (opt-out, out-of-office, automated, wrong person)

    With the new response_types schema, ONLY types explicitly marked
    auto_send=true are sent automatically; every other (and any unclear) reply
    is drafted and routed to Needs Review for a human to check and hit send.
    """
    intent = str(ai_result.get("intent", "")).strip().lower()

    # 1) Hard stops first (opt-out / junk). Checked before anything else so
    #    "not_interested" never gets read as "interested".
    if intent in STOP_INTENTS:
        return "stop"
    for kw in STOP_KEYWORDS:
        if kw in intent:
            return "stop"

    # Out-of-office / automated / wrong-person are flagged by the model here.
    if ai_result.get("human_review_needed") is True:
        return "stop"

    # 2) Format-driven decision (new schema): auto-send only the marked types,
    #    draft everything else for human review.
    auto_types = _auto_send_types(reply_format)
    if auto_types is not None:
        return "send" if intent in auto_types else "skip_enrich"

    # 3) Legacy fallback (old single-main_reply formats): keyword whitelist.
    if intent in AUTO_SEND_INTENTS:
        return "send"
    for kw in SEND_KEYWORDS:
        if kw in intent:
            return "send"

    return "skip_enrich"


def record_lead(platform, workspace_name, external_lead_id, reply_id,
                ai_result, action, replied, fup_added,
                latest_reply=None, name="", email="",
                reply_text="", subject="", campaign="", thread=None,
                company="", lead_data=None, send_meta=None):
    """Write/update the lead row that powers the dashboard. Never raises."""
    try:
        if latest_reply:
            name = name or latest_reply.get("from_name", "") or ""
            email = email or latest_reply.get("from_email_address", "") or ""
            reply_text = reply_text or latest_reply.get("body", "") or latest_reply.get("text_body", "") or ""
            subject = subject or latest_reply.get("subject", "") or ""

        followups = [ai_result.get(f"followup_{i}", "") for i in range(1, 9)]

        db.upsert_lead(
            platform=platform,
            external_lead_id=external_lead_id,
            reply_id=reply_id,
            workspace_name=workspace_name,
            name=name,
            email=email,
            company=company,
            interested=(latest_reply.get("interested") if latest_reply else True),
            intent=ai_result.get("intent", ""),
            confidence=ai_result.get("confidence", ""),
            action=action,
            reply_added=bool(ai_result.get("main_reply")),
            replied=replied,
            fup_added=fup_added,
            main_reply=ai_result.get("main_reply", ""),
            reply_text=reply_text,
            subject=subject,
            campaign=campaign,
            followups=followups,
            thread=thread or [],
            lead_data=lead_data or {},
            send_meta=send_meta or {},
        )
    except Exception as ex:
        log(f"Failed to record lead in dashboard DB: {ex}")


# Payload keys that aren't useful lead-profile data (operational/noisy/long).
LEAD_DATA_EXCLUDE = {
    "reply_html", "reply_text", "reply_text_snippet", "timestamp", "event_type",
    "unibox_url", "campaign_id", "workspace", "step", "variant", "reply_subject",
    "main_reply", "workspace_name",
}


def build_instantly_lead_data(payload, sender_email=""):
    data = {k: v for k, v in payload.items()
            if k not in LEAD_DATA_EXCLUDE and v not in (None, "")}
    if sender_email:
        data["Sending email"] = sender_email
    return data


def log(message):
    print(f"[{datetime.now()}] {message}", flush=True)


def bison_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


def instantly_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


def load_json_file(path):
    with open(path, "r") as f:
        return json.load(f)


def save_log(filename, data):
    os.makedirs("logs", exist_ok=True)
    path = f"logs/{filename}"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path


def load_processed_replies():
    os.makedirs("logs", exist_ok=True)

    if not os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "w") as f:
            json.dump([], f)

    with open(PROCESSED_FILE, "r") as f:
        try:
            return json.load(f)
        except Exception:
            return []


def save_processed_reply(reply_id):
    processed = load_processed_replies()

    if reply_id not in processed:
        processed.append(reply_id)

    with open(PROCESSED_FILE, "w") as f:
        json.dump(processed, f, indent=2)


def clean_html(text):
    if not text:
        return ""

    text = str(text)
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = text.replace("</p>", "\n").replace("<p>", "")
    text = text.replace("&nbsp;", " ")
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    return text.strip()


def normalize_email_spacing(message):
    if not message:
        return ""

    message = str(message).replace("\\n", "\n").strip()
    lines = [line.strip() for line in message.splitlines()]

    cleaned = []
    blank_added = False

    for line in lines:
        if line == "":
            if not blank_added:
                cleaned.append("")
                blank_added = True
        else:
            cleaned.append(line)
            blank_added = False

    return "\n".join(cleaned).strip()


def format_email_for_bison(message):
    message = normalize_email_spacing(message)
    return message.replace("\n", "<br>")


def extract_email_from_anything(value):
    if not value:
        return ""

    if isinstance(value, dict):
        for key in [
            "email",
            "email_address",
            "from_email",
            "from_email_address",
            "sender_email",
            "sender_email_address",
            "account_email",
            "sending_email"
        ]:
            if value.get(key) and "@" in str(value.get(key)):
                return str(value.get(key)).strip()

        value = json.dumps(value)

    value = str(value)

    angle_match = re.findall(r"<([^<>@\s]+@[^<>@\s]+\.[^<>@\s]+)>", value)
    if angle_match:
        return angle_match[-1].strip()

    plain_match = re.findall(r"[\w\.-]+@[\w\.-]+\.\w+", value)
    if plain_match:
        return plain_match[-1].strip()

    return ""


INVALID_NAMES = {"on", "from", "sent", "re", "fw", "fwd", "hey", "hello", "to", "cc", "subject", "date"}


def _clean_name_token(name):
    """Take the first token of a name and keep only its leading letters, so quoted
    headers like 'From:' don't get treated as a name. Returns '' if invalid."""
    first = (name or "").strip().strip('"').strip("'").split(" ")[0]
    first = re.sub(r"[^A-Za-z].*$", "", first)  # cut at first non-letter (e.g. 'From:' -> 'From')
    if not first:
        return ""
    titled = first.title()
    return "" if titled.lower() in INVALID_NAMES else titled


def extract_name_from_anything(value):
    if not value:
        return ""

    if isinstance(value, dict):
        name = value.get("name") or value.get("full_name") or value.get("sender_name")
        if name and isinstance(name, str):
            clean_name = _clean_name_token(name)
            if clean_name:
                return clean_name
        return sender_name_from_email(extract_email_from_anything(value))

    value = str(value).strip()

    name_match = re.search(r"([^<>\n]+)<[\w\.-]+@[\w\.-]+\.\w+>", value)
    if name_match:
        clean_name = _clean_name_token(name_match.group(1))
        if clean_name:
            return clean_name

    return sender_name_from_email(extract_email_from_anything(value))


def sender_name_from_email(email):
    email = extract_email_from_anything(email)

    if not email:
        return ""

    local = email.split("@")[0]
    local = local.replace(".", " ").replace("_", " ").replace("-", " ").strip()

    if not local:
        return ""

    name = local.split(" ")[0].title()

    invalid_names = {"on", "from", "sent", "re", "fw", "fwd", "hey", "hello"}
    if name.lower() in invalid_names:
        return ""

    return name


def get_sender_email_from_sent_emails(sent_emails):
    if not sent_emails:
        return ""

    latest_sent = sent_emails[0]

    for field in [
        "from",
        "sender",
        "email_sender",
        "email_account",
        "from_email",
        "from_email_address",
        "sender_email",
        "sender_email_address",
        "sending_email",
        "account_email"
    ]:
        value = latest_sent.get(field)
        email = extract_email_from_anything(value)
        if email:
            return email

    body = latest_sent.get("email_body", "") or ""
    return extract_email_from_anything(body)


def get_sender_name_from_sent_emails(sent_emails):
    if not sent_emails:
        return ""

    latest_sent = sent_emails[0]

    for field in [
        "from",
        "sender",
        "email_sender",
        "email_account",
        "from_name",
        "sender_name",
        "name"
    ]:
        value = latest_sent.get(field)
        name = extract_name_from_anything(value)
        if name:
            return name

    email = get_sender_email_from_sent_emails(sent_emails)
    return sender_name_from_email(email)


def strip_existing_signature(message):
    message = normalize_email_spacing(message)

    patterns = [
        r"\n+(Best|Best regards|Kind regards|Regards|Warm regards|Thanks|Thanks in advance|Thank you|Sincerely|Cheers),?\s*\n?.*$",
        r"\n+\{\{sendingAccountName\}\}.*$",
        r"\n+\{\{sending_account_name\}\}.*$",
        r"\n+\{\{website\}\}.*$",
        r"\n+https?://.*$"
    ]

    for pattern in patterns:
        message = re.sub(
            pattern,
            "",
            message,
            flags=re.IGNORECASE | re.DOTALL
        ).strip()

    return normalize_email_spacing(message)


def add_signature(message, sender_name, website):
    message = strip_existing_signature(message)

    sender_name = (sender_name or "").strip()
    website = (website or "").strip()

    signature_parts = []

    if sender_name:
        signature_parts.append(sender_name)

    if website:
        signature_parts.append(website)

    signature = "\n".join(signature_parts).strip()

    if signature:
        return f"{message}\n\n{signature}"

    return message


# ============================================================
# Calendly scheduling (real availability, prospect-timezone aware)
# ============================================================

US_STATE_TZ = {
    "alabama": "America/Chicago", "alaska": "America/Anchorage", "arizona": "America/Phoenix",
    "arkansas": "America/Chicago", "california": "America/Los_Angeles", "colorado": "America/Denver",
    "connecticut": "America/New_York", "delaware": "America/New_York", "florida": "America/New_York",
    "georgia": "America/New_York", "hawaii": "Pacific/Honolulu", "idaho": "America/Denver",
    "illinois": "America/Chicago", "indiana": "America/Indiana/Indianapolis", "iowa": "America/Chicago",
    "kansas": "America/Chicago", "kentucky": "America/New_York", "louisiana": "America/Chicago",
    "maine": "America/New_York", "maryland": "America/New_York", "massachusetts": "America/New_York",
    "michigan": "America/New_York", "minnesota": "America/Chicago", "mississippi": "America/Chicago",
    "missouri": "America/Chicago", "montana": "America/Denver", "nebraska": "America/Chicago",
    "nevada": "America/Los_Angeles", "new hampshire": "America/New_York", "new jersey": "America/New_York",
    "new mexico": "America/Denver", "new york": "America/New_York", "north carolina": "America/New_York",
    "north dakota": "America/Chicago", "ohio": "America/New_York", "oklahoma": "America/Chicago",
    "oregon": "America/Los_Angeles", "pennsylvania": "America/New_York", "rhode island": "America/New_York",
    "south carolina": "America/New_York", "south dakota": "America/Chicago", "tennessee": "America/Chicago",
    "texas": "America/Chicago", "utah": "America/Denver", "vermont": "America/New_York",
    "virginia": "America/New_York", "washington": "America/Los_Angeles", "west virginia": "America/New_York",
    "wisconsin": "America/Chicago", "wyoming": "America/Denver", "district of columbia": "America/New_York",
}

COUNTRY_TZ = {
    "united states": "America/New_York", "usa": "America/New_York", "canada": "America/Toronto",
    "united kingdom": "Europe/London", "england": "Europe/London", "scotland": "Europe/London",
    "ireland": "Europe/Dublin", "france": "Europe/Paris", "germany": "Europe/Berlin",
    "spain": "Europe/Madrid", "italy": "Europe/Rome", "netherlands": "Europe/Amsterdam",
    "india": "Asia/Kolkata", "australia": "Australia/Sydney", "singapore": "Asia/Singapore",
    "united arab emirates": "Asia/Dubai", "new zealand": "Pacific/Auckland",
    "brazil": "America/Sao_Paulo", "mexico": "America/Mexico_City", "south africa": "Africa/Johannesburg",
    "japan": "Asia/Tokyo", "philippines": "Asia/Manila",
}


def timezone_from_location(location, default="America/New_York"):
    loc = (location or "").lower()
    if not loc:
        return default
    for state, tz in US_STATE_TZ.items():
        if state in loc:
            return tz
    for country, tz in COUNTRY_TZ.items():
        if country in loc:
            return tz
    return default


def _calendly_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def resolve_calendly_event_type(token, scheduling_url=""):
    """Return (event_type_uri, user_timezone) for the client's Calendly."""
    try:
        r = requests.get("https://api.calendly.com/users/me", headers=_calendly_headers(token), timeout=15)
        if r.status_code != 200:
            log(f"Calendly /users/me failed: {r.status_code} {r.text[:160]}")
            return None, None
        user = r.json().get("resource", {})
        user_uri = user.get("uri")
        user_tz = user.get("timezone")

        r2 = requests.get("https://api.calendly.com/event_types", headers=_calendly_headers(token),
                          params={"user": user_uri, "active": "true", "count": 100}, timeout=15)
        if r2.status_code != 200:
            log(f"Calendly event_types failed: {r2.status_code}")
            return None, user_tz
        items = r2.json().get("collection", [])
        if not items:
            return None, user_tz

        slug = (scheduling_url or "").rstrip("/").split("/")[-1].lower() if scheduling_url else ""
        chosen = None
        if slug:
            for it in items:
                if slug and slug in (it.get("scheduling_url", "") or "").lower():
                    chosen = it
                    break
        chosen = chosen or items[0]
        return chosen.get("uri"), user_tz
    except Exception as ex:
        log(f"Calendly resolve error: {ex}")
        return None, None


def get_calendly_slots(token, scheduling_url, prospect_tz, count=3,
                       window_start=10, window_end=14, min_days=2, exclude=None):
    """Return up to `count` real open slots as dicts {"utc": iso, "label": str},
    in the prospect's timezone, spread across different days. `exclude` is a set
    of UTC-ISO keys already proposed to other prospects (skipped here)."""
    from datetime import datetime, timedelta, timezone as _utc
    from zoneinfo import ZoneInfo
    import random

    exclude = exclude or set()

    event_type_uri, _ = resolve_calendly_event_type(token, scheduling_url)
    if not event_type_uri:
        return []

    start = datetime.now(_utc.utc) + timedelta(days=min_days)
    end = start + timedelta(days=7)  # Calendly allows max 7-day window
    params = {
        "event_type": event_type_uri,
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        r = requests.get("https://api.calendly.com/event_type_available_times",
                         headers=_calendly_headers(token), params=params, timeout=20)
    except Exception as ex:
        log(f"Calendly available_times error: {ex}")
        return []
    if r.status_code != 200:
        log(f"Calendly available_times failed: {r.status_code} {r.text[:160]}")
        return []

    try:
        ptz = ZoneInfo(prospect_tz)
    except Exception:
        ptz = ZoneInfo("America/New_York")

    def norm_utc(s):
        # canonical UTC ISO key, e.g. 2026-06-23T18:00:00Z
        try:
            return (datetime.fromisoformat(s.replace("Z", "+00:00"))
                    .astimezone(_utc.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        except Exception:
            return None

    rows = []  # (local_dt, utc_key)
    for s in r.json().get("collection", []):
        raw = s.get("start_time")
        if not raw:
            continue
        key = norm_utc(raw)
        if not key or key in exclude:
            continue
        try:
            local = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(ptz)
        except Exception:
            continue
        if local.weekday() >= 5:
            continue
        rows.append((local, key))

    primary = [r for r in rows if window_start <= r[0].hour < window_end]
    pool = primary or [r for r in rows if 9 <= r[0].hour < 17] or rows
    if not pool:
        return []

    by_day = {}
    for local, key in pool:
        by_day.setdefault(local.date(), []).append((local, key))
    days = list(by_day.keys())
    random.shuffle(days)  # extra spread across prospects

    chosen = [random.choice(by_day[d]) for d in days[:count]]
    chosen.sort(key=lambda x: x[0])
    return [{"utc": key, "label": local.strftime("%A, %b %d at %-I:%M %p %Z")}
            for local, key in chosen]


def build_scheduling_context(workspace, location, prospect_key="", mode="reply"):
    """Build a prompt block of real open Calendly times in the prospect's
    timezone, EXCLUDING times already proposed to other prospects, and RESERVE
    the ones we pick so no other prospect gets them. Returns '' when no token /
    no slots (the AI then falls back to generic scheduling language)."""
    token = (workspace or {}).get("calendly_token", "")
    if not token:
        return ""
    sched_url = workspace.get("calendly_scheduling_url", "")
    ws_name = workspace.get("name", "")
    prospect_tz = timezone_from_location(location)

    # Follow-up sequences propose times in several messages, so they need more
    # distinct real slots than a single reply.
    want = 6 if mode == "followup" else 3

    from datetime import datetime, timezone as _utc
    now_iso = datetime.now(_utc.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        reserved = db.get_reserved_slot_keys(ws_name, now_iso)
    except Exception as ex:
        log(f"Calendly reserved-slot lookup failed: {ex}")
        reserved = set()

    try:
        slots = get_calendly_slots(token, sched_url, prospect_tz, count=want, exclude=reserved)
        if not slots:
            # Everything is reserved or nothing fits; fall back to the open pool
            # so we still propose real times rather than nothing.
            slots = get_calendly_slots(token, sched_url, prospect_tz, count=want)
    except Exception as ex:
        log(f"Calendly scheduling context failed: {ex}")
        return ""
    if not slots:
        return ""

    # Reserve these so the next prospect won't be offered the same times.
    try:
        db.reserve_slots(ws_name, prospect_key, slots)
    except Exception as ex:
        log(f"Calendly reserve_slots failed: {ex}")

    tzname = prospect_tz.split("/")[-1].replace("_", " ")
    lines = "\n".join(f"- {s['label']}" for s in slots)
    link = sched_url or ""
    extra = ""
    if mode == "followup":
        extra = ("\nThese are the ONLY real open times. When a follow-up proposes a meeting, "
                 "use a DIFFERENT one of these times in each message (do not repeat the same "
                 "time across follow-ups), and never invent times not listed here.")
    return (f"The prospect appears to be in {tzname} time. Propose ONLY these real, "
            f"currently-open times from the client's calendar, exactly as written (already "
            f"in the prospect's timezone). Do NOT invent any other times:\n{lines}{extra}\n"
            + (f"Always include this booking link so they can confirm: {link}" if link else ""))


# Keys we look at (in order) to figure out where a prospect is located.
LOCATION_KEYS = [
    "location", "Location", "prospect_location", "timezone", "time_zone", "tz",
    "city", "City", "state", "State", "region", "Region",
    "country", "Country", "country_name", "address", "Address",
]


def extract_location_from_data(data):
    """Pull a best-guess location string from a lead/custom-variable dict."""
    if not isinstance(data, dict):
        return ""
    parts = []
    for k in LOCATION_KEYS:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    # de-dup while preserving order
    seen, out = set(), []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ", ".join(out)


def fetch_bison_lead_location(lead_id, api_key, base_url):
    """Best-effort: GET the Bison lead and extract a location string from its
    fields + custom variables. Returns '' on any failure (graceful)."""
    try:
        r = requests.get(f"{base_url}/api/leads/{lead_id}",
                         headers=bison_headers(api_key), timeout=15)
        if r.status_code not in (200, 201):
            return ""
        lead = r.json().get("data", r.json())
        flat = dict(lead) if isinstance(lead, dict) else {}
        cv = flat.get("custom_variables")
        if isinstance(cv, dict):
            flat = {**flat, **cv}
        elif isinstance(cv, list):
            for item in cv:
                if isinstance(item, dict) and "name" in item:
                    flat[item["name"]] = item.get("value", "")
        return extract_location_from_data(flat)
    except Exception as ex:
        log(f"Bison lead location fetch failed: {ex}")
        return ""


FOLLOWUP_SYSTEM = (
    "You write outbound sales FOLLOW-UPS that continue an existing email thread to re-engage a "
    "quiet prospect and book a meeting. Return only valid JSON. No markdown. Never add sign-offs, "
    "sender names, initials, websites, or signatures (the system adds those). Each message stands "
    "alone, is short and human, continues the conversation (never restarts the pitch), and ends "
    "with a clear low-friction next step."
)


def _rules_block(rules):
    """Free-form, user-pasted rules from the dashboard Rules page. Highest
    priority so the operator can correct repeated mistakes without code changes."""
    rules = (rules or "").strip()
    if not rules:
        return ""
    return ("\n\nOPERATOR RULES (MANDATORY — follow these exactly; when they conflict with "
            "anything above, THESE WIN):\n" + rules + "\n")


def _followup_prompt(client_profile, reply_format, thread, scheduling_context="", extra_rules=""):
    sched = f"\nLIVE SCHEDULING (use these REAL open times whenever a follow-up proposes a meeting):\n{scheduling_context}\n" if scheduling_context else ""
    return f"""
You are writing a sequence of follow-ups for a prospect who replied positively earlier and whom we
ALREADY responded to, but who has since gone quiet. Read the full thread and continue naturally from
where it left off. Do NOT restate the original reply or re-introduce the offer from scratch.

CLIENT PROFILE:
{json.dumps(client_profile, indent=2)}

FOLLOW-UP FORMAT RULES:
{json.dumps(reply_format, indent=2)}

EMAIL THREAD (oldest to newest):
{json.dumps(thread, indent=2)}
{sched}{extra_rules}
---
Write a follow-up sequence:
- "main_reply": the FIRST follow-up to send NOW. Lightly reference the prior conversation, add a fresh
  reason to talk, and propose a next step. Short and human.
- "followup_1" through "followup_6": the next six follow-ups, each a DIFFERENT angle (new value,
  social proof, soft check-in, scheduling nudge, a relevant question, a light breakup near the end),
  escalating sensibly over a month. Each stands alone, short, ends with a clear next step.
  Do NOT produce followup_7 or followup_8.

RULES:
- Continue the thread; never re-introduce the offer from scratch or repeat the first reply.
- No sign-offs, names, initials, websites, or signatures.
- No em dashes. No buzzwords or hype. Vary rhythm. Keep most under ~90 words.
- Every message ends with a clear next step or question.

Return ONLY valid JSON:
{{
  "intent": "followup",
  "confidence": 1,
  "human_review_needed": false,
  "main_reply": "",
  "followup_1": "", "followup_2": "", "followup_3": "",
  "followup_4": "", "followup_5": "", "followup_6": ""
}}
"""


def generate_ai_reply(client_profile, reply_format, thread, mode="reply",
                      scheduling_context="", ai_cfg=None):
    extra_rules = _rules_block(db.get_setting("ai_rules", ""))

    if mode == "followup":
        prompt = _followup_prompt(client_profile, reply_format, thread, scheduling_context, extra_rules)
        system_msg = FOLLOWUP_SYSTEM
        return _call_llm(prompt, system_msg, ai_cfg)

    sched = f"\nLIVE SCHEDULING (use these REAL open times whenever you propose a meeting):\n{scheduling_context}\n" if scheduling_context else ""
    prompt = f"""
You are an expert outbound sales reply writer. Your job is to write replies that feel like they came from a sharp, real human — not a sales bot.

CLIENT PROFILE:
{json.dumps(client_profile, indent=2)}

REPLY FORMAT RULES:
{json.dumps(reply_format, indent=2)}

EMAIL THREAD:
{json.dumps(thread, indent=2)}
{sched}{extra_rules}
---

STEP 0 — USE THE RESPONSE TYPES (if the REPLY FORMAT RULES include "response_types").
- Read the prospect's LATEST message and pick the SINGLE best-matching entry in "response_types",
  using each entry's "examples" and "intent" to decide.
- Set the JSON "intent" field to that entry's "type" id exactly (e.g. "simple_positive", "pricing_question").
- Write "main_reply" by FOLLOWING that entry's "template" faithfully:
  * Keep the template's paragraph structure, order, and each of its sections. Do not drop, merge,
    reorder, or add paragraphs.
  * Replace every {{placeholder}} with specific, natural, personalized content for THIS prospect.
  * Keep the proposed-time structure exactly (e.g. two days with three time options each).
  * This is FILL-IN-THE-TEMPLATE, not rewrite-from-scratch. Match the template's length; do NOT
    shorten it just because the prospect wrote a brief message.
  * You may reword ONLY where the template's own rules allow it (e.g. "only the first sentence may
    change"); otherwise preserve the template's wording.
- SIGN-OFF: if the template ends with a closing such as "Kind regards, {{Initials}}", do NOT include
  it. Leave out the sign-off and initials entirely — the system appends the signature automatically.
- Obey that entry's "rules" exactly.
- If nothing clearly matches, choose "out_of_context_question" (or the closest non-positive type) so a
  human can review — do NOT force a positive.
- Base "followup_1" through "followup_6" on the "followups" list in order, following each follow-up's
  template with the same fill-in-faithfully approach.

STEP 1 — READ THE PROSPECT FIRST.
Before writing anything, assess:
- What is their energy? (casual, formal, skeptical, warm, funny, rushed, curious)
- Are they being brief or detailed?
- Are they using humor, sarcasm, or lightness?
- What is the one thing they actually want to know or say?

STEP 2 — MATCH THEIR ENERGY EXACTLY. (When a response_type template is provided, STEP 0 governs the
structure and length — follow the template; use this step only for tone.)
- If they wrote 2 sentences, do not write 8.
- If they used humor or were casual, be casual and warm back. You are allowed to be funny.
- If they were formal and direct, be clean and professional.
- If they asked a specific question, answer it first before anything else.
- Never open with a compliment. Never say "Great question" or "Thanks for reaching out."

STEP 3 — HUMAN, BUT FAITHFUL TO THE TEMPLATE.
- When a matched response_type "template" exists, FOLLOW IT (see STEP 0): fill the placeholders with
  natural, specific content and keep its structure and lines. Do NOT rewrite it in your own words.
- Only when NO template is provided, use the intent templates as loose guidance and write naturally.
- Either way: make the filled content read like a real person wrote it, vary sentence rhythm, never
  use em dashes, and never use buzzwords or hype.
- The template's word-count ranges are the target when a template is provided.

STEP 4 — OUT OF CONTEXT OR UNEXPECTED QUESTIONS.
If the prospect asks something that does not fit a standard intent — a clarification, an off-topic question, a question about what exactly was pitched, or anything unexpected — do NOT leave main_reply blank.
Instead:
- Answer their exact question first, directly and simply.
- Then briefly explain what the client actually does in one or two sentences, tied to what they just asked.
- End with a natural next step or soft call offer if it fits.
- Example: If they ask "Are you referring to website design?" — answer that directly, clarify what the service actually covers, and tie it back naturally.
This is the out_of_context_question intent. Use it whenever nothing else clearly fits.

STEP 5 — WHAT GOOD LOOKS LIKE.

BAD (robotic):
"Hey Sarah, thanks for being open to it. The goal is to help Acme Corp get in front of brand marketers and campaign leads. We do this through highly targeted cold email outreach that identifies high-fit prospects and manages replies through qualification and scheduling so the right opportunities reach your calendar. I'd love to walk you through how this would look for Acme Corp. Does Monday at 10 AM or 11 AM work?"

GOOD (human):
"Hey Sarah, glad it landed well.

In short — we'd be going after brand leads and campaign decision-makers at the kind of brands you actually want to work with. Targeted outreach, qualified replies, no noise reaching your calendar.

Would Monday around 10 or 11 work for a quick walkthrough?"

BAD (blank or signature-only when prospect asked a question):
[empty main_reply with just a signature]

GOOD (out of context question handled):
"Hey Line, not exactly — though there is some overlap.

We focus on the full acquisition side: paid traffic, SEO, conversion-focused landing pages, CRM follow-up, and reporting all connected into one system. Website design is part of it, but the goal is qualified inquiries, not just a new site.

Worth a quick call to see if that is relevant to MidCoast Solar?

Monday around 10 or 11, or Tuesday around noon work?"

STEP 6 — RULES THAT NEVER CHANGE.
- Do not write Best, Regards, Kind regards, Thanks, or any sign-off.
- Do not write sender name, initials, or website. The system adds this automatically.
- Stop immediately after the final CTA or question.
- NEVER return an empty main_reply unless the intent is unsubscribe or confirmed negative (not_interested). Every other reply type must have a written response.
- If out of office, automated, or wrong person — set human_review_needed to true.
- If the intent is unclear but the prospect clearly asked something — still write a reply and set confidence lower, do not leave it blank.
- Main reply must always end with a clear next step or question.

Return ONLY valid JSON:

{{
  "intent": "",
  "confidence": 0,
  "human_review_needed": false,
  "main_reply": "",
  "followup_1": "",
  "followup_2": "",
  "followup_3": "",
  "followup_4": "",
  "followup_5": "",
  "followup_6": ""
}}
"""

    system_msg = ("You write outbound sales replies that sound like a sharp, real human. Return only "
                  "valid JSON. No markdown. Never add sign-offs, sender names, initials, websites, or "
                  "signatures. Stop right after the CTA or final message. NEVER return an empty "
                  "main_reply unless intent is unsubscribe or negative_not_interested.")
    return _call_llm(prompt, system_msg, ai_cfg)


_LLM_FALLBACK = {
    "intent": "human_review",
    "confidence": 0,
    "human_review_needed": True,
    "main_reply": "",
    "followup_1": "", "followup_2": "", "followup_3": "", "followup_4": "",
    "followup_5": "", "followup_6": "", "followup_7": "", "followup_8": "",
}


def _parse_llm_json(raw):
    """Parse model output to a dict, tolerating ```json fences / stray text."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def build_ai_cfg(workspace):
    """Resolve which LLM to use for this workspace, with per-workspace key
    overrides falling back to the global settings / env."""
    provider = (workspace.get("ai_provider") or "openai").lower()
    return {
        "provider": provider,
        "fallback": bool(workspace.get("ai_fallback")),
        "openai_key": (workspace.get("openai_key")
                       or db.get_setting("openai_api_key", "")
                       or os.getenv("OPENAI_API_KEY", "")),
        "gemini_key": (workspace.get("gemini_key")
                       or db.get_setting("gemini_api_key", "")
                       or os.getenv("GEMINI_API_KEY", "")),
        "openai_model": db.get_setting("openai_model", "") or "gpt-4.1",
        "gemini_model": db.get_setting("gemini_model", "") or "gemini-2.5-pro",
    }


def _call_llm(prompt, system_msg, ai_cfg=None):
    """Try the workspace's chosen provider FIRST. If it fails (error/quota) and
    auto-fallback is on, try the other provider. Returns the human-review
    fallback only if every attempt fails."""
    ai_cfg = ai_cfg or {}
    primary = (ai_cfg.get("provider") or "openai").lower()
    other = "gemini" if primary == "openai" else "openai"

    order = [primary]
    if ai_cfg.get("fallback"):
        order.append(other)

    for prov in order:
        if prov == "gemini":
            result = _call_gemini(prompt, system_msg, ai_cfg.get("gemini_key", ""),
                                  ai_cfg.get("gemini_model", "gemini-2.5-pro"))
        else:
            result = _call_openai(prompt, system_msg, ai_cfg.get("openai_key", ""),
                                  ai_cfg.get("openai_model", "gpt-4.1"))
        if result is not None:
            if prov != primary:
                log(f"AI fallback: '{primary}' failed, used '{prov}' instead.")
            return result
        log(f"AI provider '{prov}' failed.")

    log("All AI providers failed. Returning human-review fallback.")
    return dict(_LLM_FALLBACK)


def _call_openai(prompt, system_msg, api_key="", model="gpt-4.1"):
    """Return a dict on success, or None on total failure (so _call_llm can fall back)."""
    try:
        client = OpenAI(api_key=api_key) if api_key else get_openai_client()
    except Exception as e:
        log(f"OpenAI client init failed: {e}")
        return None

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=model or "gpt-4.1",
                temperature=0.6,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = response.choices[0].message.content.strip()
            return _parse_llm_json(raw)
        except Exception as e:
            log(f"OpenAI attempt {attempt + 1} failed: {str(e)}")
            if attempt < 2:
                time.sleep(3)

    return None


def _call_gemini(prompt, system_msg, api_key="", model="gemini-2.5-pro"):
    """Return a dict on success, or None on total failure (so _call_llm can fall back)."""
    if not api_key:
        log("Gemini has no API key set (workspace or global).")
        return None

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model or 'gemini-2.5-pro'}:generateContent")
    body = {
        "systemInstruction": {"parts": [{"text": system_msg}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "responseMimeType": "application/json"},
    }
    for attempt in range(3):
        try:
            resp = requests.post(url, params={"key": api_key}, json=body, timeout=60)
            if resp.status_code != 200:
                log(f"Gemini attempt {attempt + 1} HTTP {resp.status_code}: {resp.text[:200]}")
                if attempt < 2:
                    time.sleep(3)
                continue
            data = resp.json()
            parts = (data.get("candidates", [{}])[0]
                         .get("content", {}).get("parts", [{}]))
            raw = "".join(p.get("text", "") for p in parts).strip()
            return _parse_llm_json(raw)
        except Exception as e:
            log(f"Gemini attempt {attempt + 1} failed: {str(e)}")
            if attempt < 2:
                time.sleep(3)

    return None


def notify_human_review(lead_id, reply_id, workspace_name, thread, ai_result):
    webhook_url = get_human_review_webhook()
    if not webhook_url:
        log("No human review webhook URL set.")
        return None

    payload = {
        "workspace_name": workspace_name,
        "lead_id": lead_id,
        "reply_id": reply_id,
        "reason": ai_result.get("intent", "human_review_needed"),
        "confidence": ai_result.get("confidence"),
        "ai_reply": ai_result.get("main_reply", ""),
        "thread": thread
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=15)
        log(f"Human review webhook sent: {response.status_code}")
        return response.text
    except Exception as e:
        log(f"Human review webhook failed: {e}")
        return None


# -------------------------
# EmailBison
# -------------------------

def fetch_replies(lead_id, api_key, base_url):
    url = f"{base_url}/api/leads/{lead_id}/replies"
    response = requests.get(url, headers=bison_headers(api_key))

    if response.status_code != 200:
        log(f"Failed fetching replies: {response.text}")
        return []

    return response.json().get("data", [])


def fetch_sent_emails(lead_id, api_key, base_url):
    url = f"{base_url}/api/leads/{lead_id}/sent-emails"
    response = requests.get(url, headers=bison_headers(api_key))

    if response.status_code != 200:
        log(f"Failed fetching sent emails: {response.text}")
        return []

    return response.json().get("data", [])


def build_thread(sent_emails, replies):
    thread = []

    for email in sent_emails:
        thread.append({
            "type": "sent",
            "subject": email.get("email_subject", ""),
            "body": clean_html(email.get("email_body", "")),
            "date": email.get("created_at", "")
        })

    for reply in replies:
        thread.append({
            "type": "reply",
            "id": reply.get("id"),
            "subject": reply.get("subject", ""),
            "body": reply.get("text_body", ""),
            "date": reply.get("date_received", ""),
            "sender_email_id": reply.get("sender_email_id"),
            "from_name": reply.get("from_name"),
            "from_email_address": reply.get("from_email_address"),
            "interested": reply.get("interested"),
            "automated_reply": reply.get("automated_reply")
        })

    return thread


def get_latest_reply(replies):
    if not replies:
        return None
    return replies[0]


def _scan_for_campaign_id(obj, target):
    """Recursively look for any field whose name contains 'campaign' whose value
    equals `target`. Used to detect that a reply/sent-email belongs to the
    follow-up campaign, regardless of Bison's exact field naming."""
    target = str(target or "").strip()
    if not target:
        return False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "campaign" in str(k).lower() and not isinstance(v, (dict, list)):
                if str(v).strip() == target:
                    return True
            if _scan_for_campaign_id(v, target):
                return True
    elif isinstance(obj, list):
        for it in obj:
            if _scan_for_campaign_id(it, target):
                return True
    return False


def send_reply_to_bison(reply_id, message, sender_email_id, to_name, to_email, api_key, base_url):
    url = f"{base_url}/api/replies/{reply_id}/reply"

    payload = {
        "message": message,
        "sender_email_id": sender_email_id,
        "to_emails": [
            {
                "name": to_name,
                "email_address": to_email
            }
        ],
        "content_type": "text",
        "inject_previous_email_body": True
    }

    response = requests.post(url, headers=bison_headers(api_key), json=payload)

    if response.status_code not in [200, 201]:
        log(f"Failed sending reply: {response.text}")
        return None

    return response.json()


def update_bison_lead_variables(lead_id, ai_result, latest_reply, api_key, base_url):
    url = f"{base_url}/api/leads/{lead_id}"

    full_name = latest_reply.get("from_name", "") or ""
    name_parts = full_name.split(" ", 1)

    first_name = name_parts[0] if len(name_parts) > 0 and name_parts[0] else "Unknown"
    last_name = name_parts[1] if len(name_parts) > 1 and name_parts[1] else "Unknown"
    email = latest_reply.get("from_email_address", "")

    custom_vars = [
        {"name": "main_reply", "value": ai_result.get("main_reply", "")},
        {"name": "followup_1", "value": ai_result.get("followup_1", "")},
        {"name": "followup_2", "value": ai_result.get("followup_2", "")},
        {"name": "followup_3", "value": ai_result.get("followup_3", "")},
        {"name": "followup_4", "value": ai_result.get("followup_4", "")},
        {"name": "followup_5", "value": ai_result.get("followup_5", "")},
        {"name": "followup_6", "value": ai_result.get("followup_6", "")},
    ]
    # FUP7/FUP8 only when present (follow-up mode), so normal workspaces are unchanged.
    for i in (7, 8):
        val = ai_result.get(f"followup_{i}", "")
        if val:
            custom_vars.append({"name": f"followup_{i}", "value": val})
    custom_vars += [
        {"name": "reply_intent", "value": ai_result.get("intent", "")},
        {"name": "reply_confidence", "value": str(ai_result.get("confidence", ""))},
    ]

    # EmailBison rejects the WHOLE update if any custom variable doesn't exist
    # on the account. So if it complains about a missing variable, drop that one
    # and retry, instead of losing every variable.
    for _ in range(4):
        payload = {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "custom_variables": custom_vars,
        }
        response = requests.put(url, headers=bison_headers(api_key), json=payload)

        if response.status_code in [200, 201]:
            return response.json()

        text = response.text or ""
        missing = set(re.findall(r"custom variable named\s+([\w\-]+)", text))
        remaining = [v for v in custom_vars if v.get("name") not in missing]
        if missing and len(remaining) < len(custom_vars):
            log(f"Lead variables: dropping unknown variable(s) {sorted(missing)} and retrying.")
            custom_vars = remaining
            if not custom_vars:
                break
            continue

        log(f"Failed updating lead variables: {text}")
        return None

    return None


def attach_lead_to_followup_campaign(lead_id, campaign_id, api_key, base_url):
    url = f"{base_url}/api/campaigns/{campaign_id}/leads/attach-leads"

    payload = {"lead_ids": [lead_id]}

    response = requests.post(url, headers=bison_headers(api_key), json=payload)

    if response.status_code not in [200, 201]:
        log(f"Failed attaching lead to campaign: {response.text}")
        return None

    return response.json()


def process_reply(lead_id, workspace_name):
    delay = get_reply_delay()
    log(f"Received job for lead {lead_id}. Waiting {delay} seconds before replying.")
    time.sleep(delay)

    workspace = db.get_workspace_config(workspace_name)

    if not workspace:
        log(f"Unknown workspace: {workspace_name}")
        return

    workspace_bison_api_key = workspace.get("api_key")
    base_url = (workspace.get("base_url") or db.get_setting("emailbison_base_url", "")).rstrip("/")

    if not workspace_bison_api_key:
        log(f"Missing API key for workspace {workspace_name}. Add it in the dashboard.")
        return

    if not base_url:
        log(f"Missing Bison base URL for workspace {workspace_name}. Add it in the dashboard or Settings.")
        return

    reply_followup_campaign_id = workspace["reply_followup_campaign_id"]
    mode = workspace.get("mode", "reply")

    client_profile = workspace.get("client_profile", {})
    website = workspace.get("website", "")
    default_sender_email = workspace.get("default_sender_email", "")
    default_sender_name = workspace.get("sender_name", "")
    reply_format = workspace.get("reply_format", {})

    # GUARD: don't auto-reply to follow-up-campaign replies.
    # Once we've replied to / enriched a lead, it's pushed into the follow-up
    # campaign. Bison flags later follow-up replies as "interested" too, which
    # would re-trigger us. In reply mode, if we've already handled this lead,
    # skip — a human handles an already-engaged lead. (Follow-up mode is meant
    # to re-run on past leads, so it is exempt.)
    if mode != "followup" and db.lead_already_handled(workspace_name, lead_id):
        log(f"Lead {lead_id} was already handled in '{workspace_name}'. This is most "
            f"likely a reply inside the follow-up campaign, so NOT auto-replying.")
        return

    log("Fetching replies...")
    replies = fetch_replies(lead_id, workspace_bison_api_key, base_url)

    valid_replies = [
        r for r in replies
        if r.get("interested") is True
        and r.get("automated_reply") is False
    ]

    if not valid_replies:
        log("No valid interested non-automated replies found. Stopping.")
        return

    latest_reply = get_latest_reply(valid_replies)

    if not latest_reply:
        log("No latest reply found. Stopping.")
        return

    reply_id = latest_reply["id"]
    processed_replies = load_processed_replies()

    # Follow-up mode is meant to be re-run on past leads, so skip the dedupe guard.
    if mode != "followup" and reply_id in processed_replies:
        log(f"Reply {reply_id} already processed. Stopping.")
        return

    log("Fetching sent emails...")
    sent_emails = fetch_sent_emails(lead_id, workspace_bison_api_key, base_url)

    # Secondary guard: if this reply is tied to the follow-up campaign (the lead
    # has already been sent follow-up-campaign emails), don't auto-reply.
    if (mode != "followup" and reply_followup_campaign_id
            and (_scan_for_campaign_id(sent_emails, reply_followup_campaign_id)
                 or _scan_for_campaign_id(valid_replies, reply_followup_campaign_id))):
        log(f"Reply on lead {lead_id} is tied to follow-up campaign "
            f"{reply_followup_campaign_id}. NOT auto-replying to a follow-up-campaign reply.")
        return

    sender_email = get_sender_email_from_sent_emails(sent_emails) or default_sender_email
    sender_name = get_sender_name_from_sent_emails(sent_emails) or sender_name_from_email(sender_email) or default_sender_name or "Team"

    log(f"Bison sender_email: {sender_email}")
    log(f"Bison sender_name: {sender_name}")

    log("Building thread...")
    thread = build_thread(sent_emails, valid_replies)

    # Real-availability scheduling (only when this client has a Calendly token).
    scheduling_context = ""
    if workspace.get("calendly_token"):
        prospect_location = fetch_bison_lead_location(lead_id, workspace_bison_api_key, base_url)
        log(f"Calendly: prospect location guess = '{prospect_location}'")
        scheduling_context = build_scheduling_context(
            workspace, prospect_location,
            prospect_key=str(lead_id), mode=mode)
        if scheduling_context:
            log("Calendly: injected real open times into the prompt.")

    ai_cfg = build_ai_cfg(workspace)
    log(f"Generating AI {'follow-ups' if mode == 'followup' else 'reply + followups'} "
        f"via {ai_cfg['provider']}...")
    ai_result = generate_ai_reply(client_profile, reply_format, thread, mode=mode,
                                  scheduling_context=scheduling_context, ai_cfg=ai_cfg)

    if ai_result.get("main_reply"):
        ai_result["main_reply"] = add_signature(
            ai_result.get("main_reply", ""),
            sender_name,
            website
        )

    # Follow-up mode always sends the first follow-up + enriches; otherwise classify.
    action = "send" if mode == "followup" else decide_reply_action(ai_result, reply_format)
    log(f"Reply action: {action} (mode={mode}, intent={ai_result.get('intent')})")

    if action == "stop":
        log("Stop intent (opt-out / out-of-office / automated / wrong person). Not sending, not enriching.")
        save_processed_reply(reply_id)
        record_lead(
            platform="bison",
            workspace_name=workspace_name,
            external_lead_id=lead_id,
            reply_id=reply_id,
            latest_reply=latest_reply,
            ai_result=ai_result,
            action=action,
            replied=False,
            fup_added=False,
            thread=thread,
        )
        return

    send_result = None

    if action == "send":
        log("Simple positive intent. Sending immediate reply through EmailBison...")
        send_result = send_reply_to_bison(
            reply_id=reply_id,
            message=format_email_for_bison(ai_result.get("main_reply", "")),
            sender_email_id=latest_reply["sender_email_id"],
            to_name=latest_reply["from_name"],
            to_email=latest_reply["from_email_address"],
            api_key=workspace_bison_api_key,
            base_url=base_url
        )
        if send_result:
            save_processed_reply(reply_id)
    else:
        # skip_enrich: complex reply, a human will answer it manually.
        # We still enrich the lead and push it into the follow-up campaign.
        log("Complex intent. Skipping auto-reply (human handles it). Enriching follow-ups only.")
        save_processed_reply(reply_id)

    log("Updating lead custom variables...")
    update_result = update_bison_lead_variables(
        lead_id=lead_id,
        ai_result=ai_result,
        latest_reply=latest_reply,
        api_key=workspace_bison_api_key,
        base_url=base_url
    )

    log("Attaching lead to reply follow-up campaign...")
    attach_result = attach_lead_to_followup_campaign(
        lead_id=lead_id,
        campaign_id=reply_followup_campaign_id,
        api_key=workspace_bison_api_key,
        base_url=base_url
    )

    record_lead(
        platform="bison",
        workspace_name=workspace_name,
        external_lead_id=lead_id,
        reply_id=reply_id,
        latest_reply=latest_reply,
        ai_result=ai_result,
        action=action,
        replied=bool(send_result),
        fup_added=bool(update_result),
        thread=thread,
        send_meta={
            "reply_id": reply_id,
            "sender_email_id": latest_reply.get("sender_email_id"),
            "to_name": latest_reply.get("from_name"),
            "to_email": latest_reply.get("from_email_address"),
        },
    )

    final_log = {
        "lead_id": lead_id,
        "reply_id": reply_id,
        "workspace_name": workspace_name,
        "action": action,
        "replied": bool(send_result),
        "thread": thread,
        "sender_email": sender_email,
        "sender_name": sender_name,
        "ai_result": ai_result,
        "send_result": send_result,
        "update_result": update_result,
        "attach_result": attach_result
    }

    save_log(f"sent_reply_{lead_id}_{reply_id}.json", final_log)

    log("Done.")
    log(f"Immediate reply sent: {bool(send_result)}")
    log(f"Lead variables updated: {bool(update_result)}")
    log(f"Lead attached to follow-up campaign: {bool(attach_result)}")


# -------------------------
# Instantly
# -------------------------

def get_instantly_lead_id(email, campaign_id, api_key):
    url = "https://api.instantly.ai/api/v2/leads/list"

    payload = {
        "campaign": campaign_id,
        "search": email,
        "limit": 10
    }

    response = requests.post(url, headers=instantly_headers(api_key), json=payload)

    log(f"Instantly lead lookup: {response.status_code}")

    try:
        data = response.json()
        items = data.get("items", [])

        for item in items:
            if item.get("email") == email or item.get("contact") == email:
                return item.get("id")

        if items:
            return items[0].get("id")

        log(f"No Instantly lead found for {email}")
        return None

    except Exception as e:
        log(f"Lead lookup failed: {str(e)}")
        log(response.text)
        return None


def _email_from_addr(item):
    return str(item.get("from_address_email") or item.get("from_email")
               or item.get("from") or "").lower()


def find_instantly_reply_target(email, campaign_id, api_key):
    """Return (reply_to_uuid, eaccount) for the PROSPECT's latest inbound email.

    Instantly addresses a reply to the sender of the email you reply into. If we
    reply to our own last sent message, it goes back to our own account. So we
    pick the latest email that is FROM the prospect, and use that email's
    `eaccount` (the mailbox that received it) as the sending account.
    """
    url = "https://api.instantly.ai/api/v2/emails"
    params = {"search": email, "campaign_id": campaign_id, "limit": 25}

    try:
        response = requests.get(url, headers=instantly_headers(api_key), params=params)
        log(f"Instantly email lookup: {response.status_code}")
        items = response.json().get("items", [])
    except Exception as e:
        log(f"Instantly email lookup failed: {str(e)}")
        return None, ""

    if not items:
        log(f"No Instantly email found for {email}")
        return None, ""

    target = (email or "").lower()
    inbound = [it for it in items if target and target in _email_from_addr(it)]
    chosen = inbound[0] if inbound else items[0]

    if not inbound:
        log(f"No inbound email from {email} found; falling back to latest email in thread.")

    return chosen.get("id"), (chosen.get("eaccount") or "")


def get_latest_instantly_email_id(email, campaign_id, api_key):
    """Backwards-compatible: returns just the reply target uuid."""
    uuid, _ = find_instantly_reply_target(email, campaign_id, api_key)
    return uuid


def build_reply_quote(payload):
    """Build a Gmail-style quoted block of the prospect's reply, so our reply
    reads like a natural thread instead of a standalone email."""
    reply_html = payload.get("reply_html") or ""
    reply_text = payload.get("reply_text") or ""
    if not reply_html and not reply_text:
        return "", ""

    name = (f"{payload.get('firstName','')} {payload.get('lastName','')}").strip() \
        or payload.get("firstName", "") or "they"
    email = payload.get("lead_email", "") or payload.get("email", "")
    ts = payload.get("timestamp", "")
    when = ""
    try:
        from datetime import datetime as _dt
        when = _dt.fromisoformat(ts.replace("Z", "+00:00")).strftime("%a, %b %d, %Y at %I:%M %p")
    except Exception:
        when = ts

    attr = f"On {when}, {name} <{email}> wrote:" if when else f"{name} <{email}> wrote:"

    quote_html = (f'<br><br><div class="gmail_quote">'
                  f'<div dir="ltr">{e_html(attr)}</div>'
                  f'<blockquote class="gmail_quote" style="margin:0 0 0 .8ex;'
                  f'border-left:1px solid #ccc;padding-left:1ex">'
                  f'{reply_html or e_html(reply_text).replace(chr(10), "<br>")}</blockquote></div>')

    quote_text = "\n\n" + attr + "\n" + "\n".join("> " + ln for ln in reply_text.splitlines())
    return quote_html, quote_text


def e_html(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def send_instantly_reply(reply_to_uuid, message, eaccount, subject, api_key, quote_html="", quote_text=""):
    url = "https://api.instantly.ai/api/v2/emails/reply"

    message = normalize_email_spacing(message)
    html = message.replace("\n", "<br>") + (quote_html or "")
    text = message + (quote_text or "")

    payload = {
        "reply_to_uuid": reply_to_uuid,
        "eaccount": eaccount,
        "subject": subject,
        "body": {
            "text": text,
            "html": html
        }
    }

    response = requests.post(url, headers=instantly_headers(api_key), json=payload)

    log(f"Instantly reply send: {response.status_code}")
    log(f"Instantly reply response: {response.text}")

    try:
        return response.json()
    except Exception:
        return response.text


def update_instantly_lead(lead_id, ai_result, api_key):
    url = f"https://api.instantly.ai/api/v2/leads/{lead_id}"

    custom_vars = {
        "main_reply": ai_result.get("main_reply", ""),
        "followup_1": ai_result.get("followup_1", ""),
        "followup_2": ai_result.get("followup_2", ""),
        "followup_3": ai_result.get("followup_3", ""),
        "followup_4": ai_result.get("followup_4", ""),
        "followup_5": ai_result.get("followup_5", ""),
        "followup_6": ai_result.get("followup_6", ""),
        "reply_intent": ai_result.get("intent", ""),
        "reply_confidence": str(ai_result.get("confidence", "")),
    }
    for i in (7, 8):
        val = ai_result.get(f"followup_{i}", "")
        if val:
            custom_vars[f"followup_{i}"] = val

    payload = {
        "custom_variables": custom_vars,
        "lead_label": "FUP1",
    }

    response = requests.patch(url, headers=instantly_headers(api_key), json=payload)

    log(f"Instantly lead update: {response.status_code}")
    log(f"Instantly lead update response: {response.text}")

    try:
        return response.json()
    except Exception:
        return response.text


def find_instantly_lead(email, campaign_id, preferred_name=None):
    """Find which Instantly account (workspace) owns this lead by trying each
    active Instantly workspace's API key. Returns (workspace_config, lead_id)."""
    candidates = []
    seen = set()

    if preferred_name:
        pref = db.get_workspace_config(preferred_name)
        if pref and pref.get("platform") == "instantly" and pref.get("api_key"):
            candidates.append(pref)
            seen.add(pref["name"])

    try:
        for w in db.list_workspaces():
            if w.platform == "instantly" and w.active and w.name not in seen and (w.api_key or ""):
                cfg = db.get_workspace_config(w.name)
                if cfg and cfg.get("api_key"):
                    candidates.append(cfg)
                    seen.add(w.name)
    except Exception as ex:
        log(f"Failed to list Instantly workspaces: {ex}")

    log(f"Trying {len(candidates)} Instantly workspace(s) to locate lead {email}...")

    for cfg in candidates:
        try:
            lead_id = get_instantly_lead_id(
                email=email,
                campaign_id=campaign_id,
                api_key=cfg["api_key"]
            )
        except Exception as ex:
            log(f"Instantly lookup error in workspace {cfg['name']}: {ex}")
            lead_id = None

        if lead_id:
            return cfg, lead_id

    return None, None


def process_instantly_reply(payload, workspace_name="Webaholics"):
    delay = get_reply_delay()
    log(f"Received Instantly job. Waiting {delay} seconds before replying.")
    time.sleep(delay)
    log("Processing Instantly reply webhook...")
    log(json.dumps(payload, indent=2))

    email = payload.get("lead_email", "") or payload.get("email", "") or payload.get("from_email", "")
    campaign_id = payload.get("campaign_id", "") or payload.get("campaign", "") or payload.get("campaignId", "")
    first_name = payload.get("firstName", "")
    last_name = payload.get("lastName", "")
    full_name = (f"{first_name} {last_name}").strip() or first_name
    company = payload.get("companyName", "") or payload.get("organization", "") or ""
    reply_text = payload.get("reply_text", "")
    subject = payload.get("reply_subject", "Re:")
    lead_data = build_instantly_lead_data(payload)

    if not email or not campaign_id:
        log("Missing email or campaign_id in Instantly webhook.")
        return

    # Find the Instantly account that actually owns this lead (tries every
    # active Instantly workspace's API key), so routing is automatic.
    workspace, lead_id = find_instantly_lead(email, campaign_id, preferred_name=workspace_name)

    if not workspace or not lead_id:
        log("No Instantly lead found in ANY active Instantly workspace. "
            "Make sure a workspace has the API key for the Instantly account that owns this campaign.")
        record_lead(
            platform="instantly",
            workspace_name=workspace_name,
            external_lead_id="",
            reply_id="",
            ai_result={"intent": "instantly_lead_not_found", "main_reply": ""},
            action="error",
            replied=False,
            fup_added=False,
            name=full_name,
            email=email,
            company=company,
            lead_data=lead_data,
        )
        return

    workspace_name = workspace["name"]
    instantly_api_key = workspace["api_key"]
    log(f"Instantly lead matched in workspace: {workspace_name} (lead_id={lead_id})")

    # Resolve the prospect's inbound email up front. It gives us BOTH the right
    # reply target and the sending account (the mailbox that received the reply),
    # which is the reliable source for the sender name/email.
    reply_to_uuid, thread_eaccount = find_instantly_reply_target(email, campaign_id, instantly_api_key)

    thread = [
        {
            "type": "reply",
            "body": reply_text,
            "payload": payload
        }
    ]

    client_profile = workspace.get("client_profile", {})
    reply_format = workspace.get("reply_format", {})
    mode = workspace.get("mode", "reply")

    # GUARD: don't auto-reply to a lead we've already handled (e.g. it's now in
    # a follow-up sequence and replied again). A human handles engaged leads.
    if mode != "followup" and db.lead_already_handled(workspace_name, lead_id):
        log(f"Instantly lead {lead_id} already handled in '{workspace_name}'. "
            f"Likely a follow-up/subsequence reply, so NOT auto-replying.")
        return

    # Real-availability scheduling (only when this client has a Calendly token).
    scheduling_context = ""
    if workspace.get("calendly_token"):
        prospect_location = extract_location_from_data(lead_data)
        log(f"Calendly: prospect location guess = '{prospect_location}'")
        scheduling_context = build_scheduling_context(
            workspace, prospect_location,
            prospect_key=(email or str(lead_id)), mode=mode)
        if scheduling_context:
            log("Calendly: injected real open times into the prompt.")

    ai_cfg = build_ai_cfg(workspace)
    log(f"Generating Instantly AI {'follow-ups' if mode == 'followup' else 'reply + followups'} "
        f"via {ai_cfg['provider']}...")
    ai_result = generate_ai_reply(client_profile, reply_format, thread, mode=mode,
                                  scheduling_context=scheduling_context, ai_cfg=ai_cfg)

    # Sender = the account that received the prospect's reply. Do NOT parse the
    # prospect's reply text for this (that grabs the prospect / quoted headers).
    sender_email = (thread_eaccount
                    or extract_email_from_anything(payload.get("eaccount"))
                    or extract_email_from_anything(payload.get("from_email")))
    sender_email = (sender_email or "").replace("mailto:", "").strip()
    sender_name = (extract_name_from_anything(payload.get("from"))
                   or sender_name_from_email(sender_email)
                   or "Team")

    website = workspace.get("website", "") or "https://www.webaholics.ai"

    if sender_email:
        lead_data["Sending email"] = sender_email

    log(f"Instantly sender_email: {sender_email}")
    log(f"Instantly sender_name: {sender_name}")

    if ai_result.get("main_reply"):
        ai_result["main_reply"] = add_signature(
            ai_result.get("main_reply", ""),
            sender_name,
            website
        )

    action = "send" if mode == "followup" else decide_reply_action(ai_result, reply_format)
    log(f"Instantly reply action: {action} (mode={mode}, intent={ai_result.get('intent')})")

    if action == "stop":
        log("Instantly stop intent (opt-out / out-of-office / automated / wrong person). Not sending, not enriching.")
        save_log(f"instantly_stop_{lead_id}.json", {
            "lead_id": lead_id,
            "email": email,
            "payload": payload,
            "ai_result": ai_result
        })
        record_lead(
            platform="instantly",
            workspace_name=workspace_name,
            external_lead_id=lead_id,
            reply_id="",
            ai_result=ai_result,
            action=action,
            replied=False,
            fup_added=False,
            name=full_name,
            email=email,
            company=company,
            reply_text=reply_text,
            subject=subject,
            campaign=payload.get("campaign_name", ""),
            thread=thread,
            lead_data=lead_data,
        )
        return

    send_result = None
    eaccount = sender_email  # default; refined below from the inbound email

    if action == "send":
        # reply_to_uuid + thread_eaccount were resolved earlier.
        eaccount = thread_eaccount or sender_email
        log(f"Instantly reply target uuid={reply_to_uuid}, eaccount={eaccount}")

        if reply_to_uuid:
            if not eaccount:
                log("No sending account found. Skipping reply send to avoid wrong sender.")
            else:
                log("Sending Instantly reply to the prospect's inbound email...")
                quote_html, quote_text = build_reply_quote(payload)
                send_result = send_instantly_reply(
                    reply_to_uuid=reply_to_uuid,
                    message=ai_result.get("main_reply", ""),
                    eaccount=eaccount,
                    subject=subject,
                    api_key=instantly_api_key,
                    quote_html=quote_html,
                    quote_text=quote_text,
                )
                log(f"Instantly send result: {send_result}")
        else:
            log("No reply_to_uuid found. Skipping reply send.")
    else:
        # skip_enrich: complex reply, a human will answer it manually.
        # We still enrich the lead + apply the FUP1 label below.
        log("Complex intent. Skipping auto-reply (human handles it). Enriching follow-ups only.")

    log("Updating Instantly lead variables...")
    update_result = update_instantly_lead(
        lead_id=lead_id,
        ai_result=ai_result,
        api_key=instantly_api_key
    )

    record_lead(
        platform="instantly",
        workspace_name=workspace_name,
        external_lead_id=lead_id,
        reply_id="",
        ai_result=ai_result,
        action=action,
        replied=bool(send_result),
        fup_added=bool(update_result),
        name=full_name,
        email=email,
        company=company,
        reply_text=reply_text,
        subject=subject,
        campaign=payload.get("campaign_name", ""),
        thread=thread,
        lead_data=lead_data,
        send_meta={
            "campaign_id": campaign_id,
            "eaccount": eaccount,
            "email": email,
            "subject": subject,
        },
    )

    final_log = {
        "lead_id": lead_id,
        "email": email,
        "first_name": first_name,
        "campaign_id": campaign_id,
        "action": action,
        "replied": bool(send_result),
        "reply_text": reply_text,
        "sender_email": sender_email,
        "sender_name": sender_name,
        "subject": subject,
        "payload": payload,
        "ai_result": ai_result,
        "send_result": send_result,
        "update_result": update_result
    }

    save_log(f"instantly_reply_{lead_id}.json", final_log)

    log("Instantly reply processing completed.")


# -------------------------
# Routes
# -------------------------

@app.get("/")
def root():
    # Send visitors straight to the dashboard.
    return RedirectResponse("/dashboard")


@app.get("/healthz")
def health_check():
    return {"status": "reply_management_running"}


@app.get("/test")
def test_run():
    return {"success": True, "message": "test route active"}


@app.post("/bison-reply")
async def bison_reply_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    log(f"Bison webhook RECEIVED: {json.dumps(payload)[:2000]}")

    # Workspace comes from the URL query (?workspace_name=...), so Bison's native
    # webhook can route directly without Make.com.
    qp = request.query_params
    workspace_name = (qp.get("workspace_name") or qp.get("ws")
                      or (payload.get("workspace_name") if isinstance(payload, dict) else None))

    event = payload.get("event") if isinstance(payload, dict) else None

    # ----- EmailBison native webhooks (event + data) -----
    if isinstance(event, dict):
        etype = event.get("type")

        # Interested / reply events -> normal reply flow (workspace decides mode).
        if etype in BISON_REPLY_EVENTS:
            lead_id = deep_find_lead_id(payload)
            if not lead_id:
                log(f"{etype}: no lead_id found in payload. Ignoring.")
                return {"success": False, "error": "no lead_id"}
            if not workspace_name:
                workspace_name = event.get("workspace_name") or "Insight Media Labs"
            try:
                lead_id = int(lead_id)
            except (TypeError, ValueError):
                pass
            log(f"Bison {etype} on lead {lead_id} -> workspace '{workspace_name}'")
            background_tasks.add_task(process_reply, lead_id, workspace_name)
            return {"success": True, "message": "Reply job accepted", "lead_id": lead_id,
                    "workspace_name": workspace_name}

        if etype != "TAG_ATTACHED":
            log(f"Ignoring Bison event: {etype}")
            return {"success": True, "ignored": etype}

        data = payload.get("data") or {}
        tag_name = (data.get("tag_name") or "").strip()
        taggable_type = (data.get("taggable_type") or "").strip().lower()
        followup_tag = db.get_setting("followup_trigger_tag", "Begin follow-up").strip()
        reply_tag = db.get_setting("reply_trigger_tag", "Interested").strip()

        if taggable_type != "lead":
            log(f"Tag '{tag_name}' attached to {taggable_type}, not a lead. Ignoring.")
            return {"success": True, "ignored": "not a lead"}

        lead_id = data.get("taggable_id")
        if not lead_id:
            return {"success": False, "error": "no taggable_id (lead id) in payload"}
        try:
            lead_id = int(lead_id)
        except (TypeError, ValueError):
            pass

        default_ws = workspace_name or event.get("workspace_name") or "Insight Media Labs"
        tn = tag_name.lower()

        if tn == followup_tag.lower():
            ws = qp.get("fup_workspace") or default_ws
            log(f"Follow-up tag '{tag_name}' on lead {lead_id} -> workspace '{ws}'")
            background_tasks.add_task(process_reply, lead_id, ws)
            return {"success": True, "message": "Follow-up job accepted",
                    "lead_id": lead_id, "workspace_name": ws}

        if tn == reply_tag.lower():
            ws = qp.get("reply_workspace") or default_ws
            log(f"Reply tag '{tag_name}' on lead {lead_id} -> workspace '{ws}'")
            background_tasks.add_task(process_reply, lead_id, ws)
            return {"success": True, "message": "Reply job accepted",
                    "lead_id": lead_id, "workspace_name": ws}

        log(f"Tag '{tag_name}' is neither the reply trigger ('{reply_tag}') nor "
            f"follow-up trigger ('{followup_tag}'). Ignoring.")
        return {"success": True, "ignored": "tag not a trigger"}

    # ----- legacy Make.com shape: {lead_id, workspace_name} -----
    workspace_name = workspace_name or "Insight Media Labs"
    raw_lead = extract_bison_lead_id(payload)
    if not raw_lead:
        log("Bison webhook: could not find a lead id in the payload.")
        return {"success": False, "error": "Missing lead_id"}

    try:
        lead_id = int(raw_lead)
    except (TypeError, ValueError):
        lead_id = raw_lead

    background_tasks.add_task(process_reply, lead_id, workspace_name)

    return {
        "success": True,
        "message": "Reply job accepted",
        "lead_id": lead_id,
        "workspace_name": workspace_name,
        "delay_seconds": get_reply_delay()
    }


# EmailBison native event types that mean "the prospect replied / is interested"
# and should run the normal reply flow.
BISON_REPLY_EVENTS = {
    "LEAD_INTERESTED", "CONTACT_INTERESTED", "CONTACT_REPLIED",
    "REPLY_RECEIVED", "UNTRACKED_REPLY_RECEIVED",
}


def deep_find_lead_id(obj):
    """Recursively look for a key literally named lead_id/leadId anywhere in the
    payload (Bison nests it as data.reply.lead_id). Avoids grabbing a reply 'id'."""
    if isinstance(obj, dict):
        for k in ("lead_id", "leadId"):
            if obj.get(k) not in (None, ""):
                return obj.get(k)
        for v in obj.values():
            found = deep_find_lead_id(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = deep_find_lead_id(item)
            if found is not None:
                return found
    return None


def extract_bison_lead_id(payload):
    """Pull a lead id out of whatever shape Bison's webhook sends."""
    if not isinstance(payload, dict):
        return None
    for key in ("lead_id", "leadId", "lead_uuid", "id"):
        if payload.get(key):
            return payload.get(key)
    for parent in ("lead", "data", "reply", "payload", "resource"):
        sub = payload.get(parent)
        if isinstance(sub, dict):
            for key in ("lead_id", "leadId", "id", "uuid"):
                if sub.get(key):
                    return sub.get(key)
    return None


def resolve_instantly_workspace(payload):
    """Figure out which workspace an Instantly reply belongs to.

    1. Use workspace_name from the payload if it's present and valid.
    2. Otherwise, if exactly one Instantly workspace is marked Active, use it.
    3. Otherwise fall back to 'Webaholics'.
    """
    name = payload.get("workspace_name") or payload.get("workspace")
    if name and db.get_workspace_config(name):
        return name

    try:
        active_instantly = [
            w for w in db.list_workspaces()
            if (w.platform == "instantly" and w.active)
        ]
        if len(active_instantly) == 1:
            return active_instantly[0].name
    except Exception as ex:
        log(f"Instantly workspace auto-detect failed: {ex}")

    return "Webaholics"


@app.post("/instantly-reply")
async def instantly_reply_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    log(f"Instantly webhook RECEIVED: {json.dumps(payload)[:2000]}")

    # Explicit routing via the webhook URL (?workspace_name=...), so a follow-up
    # workspace can be targeted even when it shares the Instantly account/key.
    qp = request.query_params
    workspace_name = (qp.get("workspace_name") or qp.get("ws")
                      or resolve_instantly_workspace(payload))
    log(f"Instantly webhook routed to workspace: {workspace_name}")

    background_tasks.add_task(process_instantly_reply, payload, workspace_name)

    return {
        "success": True,
        "message": "Instantly reply received",
        "workspace_name": workspace_name
    }