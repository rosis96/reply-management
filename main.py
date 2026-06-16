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

import db
from dashboard import router as dashboard_router

load_dotenv()

app = FastAPI()


@app.on_event("startup")
def _startup():
    db.init_db()
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


def decide_reply_action(ai_result):
    """Return one of: 'send', 'skip_enrich', 'stop'.

    send        -> auto-reply the prospect + enrich + push to follow-up
    skip_enrich -> do NOT reply (human handles it) but still enrich + push to follow-up
    stop        -> do nothing (opt-out, out-of-office, automated, wrong person)
    """
    intent = str(ai_result.get("intent", "")).strip().lower()

    if intent in STOP_INTENTS:
        return "stop"

    # Out-of-office / automated / wrong-person are flagged by the model here.
    if ai_result.get("human_review_needed") is True:
        return "stop"

    if intent in AUTO_SEND_INTENTS:
        return "send"

    return "skip_enrich"


def record_lead(platform, workspace_name, external_lead_id, reply_id,
                ai_result, action, replied, fup_added,
                latest_reply=None, name="", email=""):
    """Write/update the lead row that powers the dashboard. Never raises."""
    try:
        if latest_reply:
            name = name or latest_reply.get("from_name", "") or ""
            email = email or latest_reply.get("from_email_address", "") or ""

        db.upsert_lead(
            platform=platform,
            external_lead_id=external_lead_id,
            reply_id=reply_id,
            workspace_name=workspace_name,
            name=name,
            email=email,
            interested=(latest_reply.get("interested") if latest_reply else True),
            intent=ai_result.get("intent", ""),
            confidence=ai_result.get("confidence", ""),
            action=action,
            reply_added=bool(ai_result.get("main_reply")),
            replied=replied,
            fup_added=fup_added,
            main_reply=ai_result.get("main_reply", ""),
        )
    except Exception as ex:
        log(f"Failed to record lead in dashboard DB: {ex}")


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


def extract_name_from_anything(value):
    invalid_names = {"on", "from", "sent", "re", "fw", "fwd", "hey", "hello"}

    if not value:
        return ""

    if isinstance(value, dict):
        name = value.get("name") or value.get("full_name") or value.get("sender_name")
        if name and isinstance(name, str):
            clean_name = name.strip().split(" ")[0].title()
            if clean_name.lower() not in invalid_names:
                return clean_name

        email = extract_email_from_anything(value)
        return sender_name_from_email(email)

    value = str(value).strip()

    name_match = re.search(r"([^<>\n]+)<[\w\.-]+@[\w\.-]+\.\w+>", value)
    if name_match:
        name = name_match.group(1).strip().strip('"').strip("'")
        if name:
            clean_name = name.split(" ")[0].title()
            if clean_name.lower() not in invalid_names:
                return clean_name

    email = extract_email_from_anything(value)
    return sender_name_from_email(email)


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


def generate_ai_reply(client_profile, reply_format, thread):
    prompt = f"""
You are an expert outbound sales reply writer. Your job is to write replies that feel like they came from a sharp, real human — not a sales bot.

CLIENT PROFILE:
{json.dumps(client_profile, indent=2)}

REPLY FORMAT RULES:
{json.dumps(reply_format, indent=2)}

EMAIL THREAD:
{json.dumps(thread, indent=2)}

---

STEP 1 — READ THE PROSPECT FIRST.
Before writing anything, assess:
- What is their energy? (casual, formal, skeptical, warm, funny, rushed, curious)
- Are they being brief or detailed?
- Are they using humor, sarcasm, or lightness?
- What is the one thing they actually want to know or say?

STEP 2 — MATCH THEIR ENERGY EXACTLY.
- If they wrote 2 sentences, do not write 8.
- If they used humor or were casual, be casual and warm back. You are allowed to be funny.
- If they were formal and direct, be clean and professional.
- If they asked a specific question, answer it first before anything else.
- Never open with a compliment. Never say "Great question" or "Thanks for reaching out."

STEP 3 — WRITE LIKE A PERSON, NOT A TEMPLATE.
- Use the intent templates from REPLY FORMAT RULES as strategic guidance only, not as scripts to fill in.
- Do not mechanically complete placeholders. Use them as inspiration for what to say naturally.
- Vary your sentence rhythm. Mix short punchy sentences with longer ones.
- It is okay to be slightly self-aware or dry if the prospect is.
- Never use em dashes.
- Never use buzzwords or hype.
- Word count ranges are guides, not hard rules. A genuinely short sharp reply is better than padding to hit a number.

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

    client = get_openai_client()

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4.1",
                temperature=0.6,
                messages=[
                    {
                        "role": "system",
                        "content": "You write outbound sales replies that sound like a sharp, real human. Return only valid JSON. No markdown. Never add sign-offs, sender names, initials, websites, or signatures. Stop right after the CTA or final message. NEVER return an empty main_reply unless intent is unsubscribe or negative_not_interested."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            )

            raw = response.choices[0].message.content.strip()
            return json.loads(raw)

        except Exception as e:
            log(f"OpenAI attempt {attempt + 1} failed: {str(e)}")

            if attempt < 2:
                time.sleep(3)

    return {
        "intent": "human_review",
        "confidence": 0,
        "human_review_needed": True,
        "main_reply": "",
        "followup_1": "",
        "followup_2": "",
        "followup_3": "",
        "followup_4": "",
        "followup_5": "",
        "followup_6": ""
    }


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

    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "custom_variables": [
            {"name": "main_reply", "value": ai_result.get("main_reply", "")},
            {"name": "followup_1", "value": ai_result.get("followup_1", "")},
            {"name": "followup_2", "value": ai_result.get("followup_2", "")},
            {"name": "followup_3", "value": ai_result.get("followup_3", "")},
            {"name": "followup_4", "value": ai_result.get("followup_4", "")},
            {"name": "followup_5", "value": ai_result.get("followup_5", "")},
            {"name": "followup_6", "value": ai_result.get("followup_6", "")},
            {"name": "reply_intent", "value": ai_result.get("intent", "")},
            {"name": "reply_confidence", "value": str(ai_result.get("confidence", ""))}
        ]
    }

    response = requests.put(url, headers=bison_headers(api_key), json=payload)

    if response.status_code not in [200, 201]:
        log(f"Failed updating lead variables: {response.text}")
        return None

    return response.json()


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

    client_profile = workspace.get("client_profile", {})
    website = workspace.get("website", "")
    default_sender_email = workspace.get("default_sender_email", "")
    default_sender_name = workspace.get("sender_name", "")
    reply_format = workspace.get("reply_format", {})

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

    if reply_id in processed_replies:
        log(f"Reply {reply_id} already processed. Stopping.")
        return

    log("Fetching sent emails...")
    sent_emails = fetch_sent_emails(lead_id, workspace_bison_api_key, base_url)

    sender_email = get_sender_email_from_sent_emails(sent_emails) or default_sender_email
    sender_name = get_sender_name_from_sent_emails(sent_emails) or sender_name_from_email(sender_email) or default_sender_name or "Team"

    log(f"Bison sender_email: {sender_email}")
    log(f"Bison sender_name: {sender_name}")

    log("Building thread...")
    thread = build_thread(sent_emails, valid_replies)

    log("Generating AI reply + followups...")
    ai_result = generate_ai_reply(client_profile, reply_format, thread)

    if ai_result.get("main_reply"):
        ai_result["main_reply"] = add_signature(
            ai_result.get("main_reply", ""),
            sender_name,
            website
        )

    action = decide_reply_action(ai_result)
    log(f"Reply action: {action} (intent={ai_result.get('intent')})")

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


def get_latest_instantly_email_id(email, campaign_id, api_key):
    url = "https://api.instantly.ai/api/v2/emails"

    params = {
        "search": email,
        "campaign_id": campaign_id,
        "limit": 10
    }

    response = requests.get(url, headers=instantly_headers(api_key), params=params)

    log(f"Instantly email lookup: {response.status_code}")

    try:
        data = response.json()
        items = data.get("items", [])

        if not items:
            log(f"No Instantly email found for {email}")
            return None

        return items[0].get("id")

    except Exception as e:
        log(f"Instantly email lookup failed: {str(e)}")
        log(response.text)
        return None


def send_instantly_reply(reply_to_uuid, message, eaccount, subject, api_key):
    url = "https://api.instantly.ai/api/v2/emails/reply"

    message = normalize_email_spacing(message)

    payload = {
        "reply_to_uuid": reply_to_uuid,
        "eaccount": eaccount,
        "subject": subject,
        "body": {
            "text": message,
            "html": message.replace("\n", "<br>")
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

    payload = {
        "custom_variables": {
            "main_reply": ai_result.get("main_reply", ""),
            "followup_1": ai_result.get("followup_1", ""),
            "followup_2": ai_result.get("followup_2", ""),
            "followup_3": ai_result.get("followup_3", ""),
            "followup_4": ai_result.get("followup_4", ""),
            "followup_5": ai_result.get("followup_5", ""),
            "followup_6": ai_result.get("followup_6", ""),
            "reply_intent": ai_result.get("intent", ""),
            "reply_confidence": str(ai_result.get("confidence", ""))
        },
        "lead_label": "FUP1"
    }

    response = requests.patch(url, headers=instantly_headers(api_key), json=payload)

    log(f"Instantly lead update: {response.status_code}")
    log(f"Instantly lead update response: {response.text}")

    try:
        return response.json()
    except Exception:
        return response.text


def process_instantly_reply(payload, workspace_name="Webaholics"):
    delay = get_reply_delay()
    log(f"Received Instantly job. Waiting {delay} seconds before replying.")
    time.sleep(delay)
    log("Processing Instantly reply webhook...")
    log(json.dumps(payload, indent=2))

    workspace = db.get_workspace_config(workspace_name)

    if not workspace:
        log(f"Unknown Instantly workspace: {workspace_name}")
        return

    instantly_api_key = workspace.get("api_key")

    if not instantly_api_key:
        log(f"Missing Instantly API key for workspace {workspace_name}. Add it in the dashboard.")
        return

    email = payload.get("lead_email", "") or payload.get("email", "")
    campaign_id = payload.get("campaign_id", "")
    first_name = payload.get("firstName", "")
    reply_text = payload.get("reply_text", "")
    subject = payload.get("reply_subject", "Re:")

    if not email or not campaign_id:
        log("Missing email or campaign_id in Instantly webhook.")
        return

    lead_id = get_instantly_lead_id(
        email=email,
        campaign_id=campaign_id,
        api_key=instantly_api_key
    )

    if not lead_id:
        log("No lead_id found in Instantly.")
        return

    thread = [
        {
            "type": "reply",
            "body": reply_text,
            "payload": payload
        }
    ]

    client_profile = workspace.get("client_profile", {})
    reply_format = workspace.get("reply_format", {})

    log("Generating Instantly AI reply + followups...")
    ai_result = generate_ai_reply(client_profile, reply_format, thread)

    sender_email = extract_email_from_anything(payload.get("eaccount"))
    if not sender_email:
        sender_email = extract_email_from_anything(payload.get("from_email"))
    if not sender_email:
        sender_email = extract_email_from_anything(reply_text)

    sender_name = extract_name_from_anything(payload.get("eaccount"))
    if not sender_name:
        sender_name = extract_name_from_anything(payload.get("from"))
    if not sender_name:
        sender_name = extract_name_from_anything(reply_text)
    if not sender_name:
        sender_name = sender_name_from_email(sender_email)
    if not sender_name:
        sender_name = "Team"

    website = workspace.get("website", "") or "https://www.webaholics.ai"

    log(f"Instantly sender_email: {sender_email}")
    log(f"Instantly sender_name: {sender_name}")

    if ai_result.get("main_reply"):
        ai_result["main_reply"] = add_signature(
            ai_result.get("main_reply", ""),
            sender_name,
            website
        )

    action = decide_reply_action(ai_result)
    log(f"Instantly reply action: {action} (intent={ai_result.get('intent')})")

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
            name=first_name,
            email=email,
        )
        return

    send_result = None

    if action == "send":
        reply_to_uuid = get_latest_instantly_email_id(
            email=email,
            campaign_id=campaign_id,
            api_key=instantly_api_key
        )

        if reply_to_uuid:
            if not sender_email:
                log("No sender email found. Skipping reply send to avoid wrong sender.")
            else:
                log("Simple positive intent. Sending Instantly main reply...")
                send_result = send_instantly_reply(
                    reply_to_uuid=reply_to_uuid,
                    message=ai_result.get("main_reply", ""),
                    eaccount=sender_email,
                    subject=subject,
                    api_key=instantly_api_key
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
        name=first_name,
        email=email,
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
def health_check():
    return {"status": "reply_management_running"}


@app.get("/test")
def test_run():
    return {"success": True, "message": "test route active"}


@app.post("/bison-reply")
async def bison_reply_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()

    log(f"Bison webhook RECEIVED: {json.dumps(payload)[:2000]}")

    lead_id = payload.get("lead_id")
    workspace_name = payload.get("workspace_name", "Insight Media Labs")

    if not lead_id:
        return {"success": False, "error": "Missing lead_id"}

    background_tasks.add_task(process_reply, int(lead_id), workspace_name)

    return {
        "success": True,
        "message": "Reply job accepted",
        "lead_id": lead_id,
        "workspace_name": workspace_name,
        "delay_seconds": get_reply_delay()
    }


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
    payload = await request.json()

    log(f"Instantly webhook RECEIVED: {json.dumps(payload)[:2000]}")
    workspace_name = resolve_instantly_workspace(payload)
    log(f"Instantly webhook routed to workspace: {workspace_name}")

    background_tasks.add_task(process_instantly_reply, payload, workspace_name)

    return {
        "success": True,
        "message": "Instantly reply received",
        "workspace_name": workspace_name
    }