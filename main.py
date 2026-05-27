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

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMAILBISON_BASE_URL = os.getenv("EMAILBISON_BASE_URL", "").rstrip("/")
HUMAN_REVIEW_WEBHOOK_URL = os.getenv("HUMAN_REVIEW_WEBHOOK_URL")

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

with open("config.json", "r") as f:
    CONFIG = json.load(f)

REPLY_DELAY_SECONDS = 180
PROCESSED_FILE = "logs/processed_replies.json"


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


def sender_name_from_email(email):
    if not email:
        return ""

    name = email.split("@")[0]
    name = name.replace(".", " ").replace("_", " ").replace("-", " ")
    return " ".join(word.capitalize() for word in name.split())


def extract_sender_email_from_reply_text(reply_text):
    matches = re.findall(r"<([^<>@\s]+@[^<>@\s]+\.[^<>@\s]+)>", reply_text or "")
    if matches:
        return matches[-1]
    return ""


def get_sender_email_from_sent_emails(sent_emails):
    if not sent_emails:
        return ""

    latest_sent = sent_emails[0]

    possible_fields = [
        "from_email",
        "from_email_address",
        "sender_email",
        "sender_email_address",
        "email_account",
        "sending_email",
        "account_email"
    ]

    for field in possible_fields:
        value = latest_sent.get(field)
        if value and "@" in str(value):
            return str(value).strip()

    body = latest_sent.get("email_body", "") or ""
    return extract_sender_email_from_reply_text(body)


def add_signature_once(message, sender_name, website):
    message = normalize_email_spacing(message)

    lowered = message.lower()

    if "kind regards," in lowered or "\nbest," in lowered:
        return message

    if not sender_name:
        sender_name = "Team"

    if website:
        return f"{message}\n\nKind regards,\n{sender_name}\n{website}"

    return f"{message}\n\nKind regards,\n{sender_name}"


def generate_ai_reply(client_profile, reply_format, thread):
    prompt = f"""
You are an expert outbound sales reply assistant.

CLIENT PROFILE:
{json.dumps(client_profile, indent=2)}

REPLY FORMAT RULES:
{json.dumps(reply_format, indent=2)}

EMAIL THREAD:
{json.dumps(thread, indent=2)}

TASK:
Write the immediate reply we should send now and generate 6 future follow-ups.

RULES:
- Sound human
- Do not sound robotic
- Do not use em dashes
- Do not over-explain
- Match the prospect's tone
- Continue from the actual thread
- Keep main_reply under 100 words
- Use short paragraphs
- Use only one empty line between paragraphs
- Never add double blank lines
- Keep formatting compact and readable
- Do not invent facts
- Main reply must end with a clear CTA or question
- Do not add sender signature yourself unless explicitly required by the provided format
- If unsubscribe intent, set intent to "unsubscribe" and main_reply empty
- If wrong person, out of office, automated, unclear, or risky, set human_review_needed true

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

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4.1-mini",
                temperature=0.4,
                messages=[
                    {"role": "system", "content": "Return only valid JSON. No markdown."},
                    {"role": "user", "content": prompt}
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
    if not HUMAN_REVIEW_WEBHOOK_URL:
        log("No HUMAN_REVIEW_WEBHOOK_URL set.")
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
        response = requests.post(HUMAN_REVIEW_WEBHOOK_URL, json=payload, timeout=15)
        log(f"Human review webhook sent: {response.status_code}")
        return response.text
    except Exception as e:
        log(f"Human review webhook failed: {e}")
        return None


# -------------------------
# EmailBison
# -------------------------

def fetch_replies(lead_id, api_key):
    url = f"{EMAILBISON_BASE_URL}/api/leads/{lead_id}/replies"
    response = requests.get(url, headers=bison_headers(api_key))

    if response.status_code != 200:
        log(f"Failed fetching replies: {response.text}")
        return []

    return response.json().get("data", [])


def fetch_sent_emails(lead_id, api_key):
    url = f"{EMAILBISON_BASE_URL}/api/leads/{lead_id}/sent-emails"
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


def send_reply_to_bison(reply_id, message, sender_email_id, to_name, to_email, api_key):
    url = f"{EMAILBISON_BASE_URL}/api/replies/{reply_id}/reply"

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


def update_bison_lead_variables(lead_id, ai_result, latest_reply, api_key):
    url = f"{EMAILBISON_BASE_URL}/api/leads/{lead_id}"

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


def attach_lead_to_followup_campaign(lead_id, campaign_id, api_key):
    url = f"{EMAILBISON_BASE_URL}/api/campaigns/{campaign_id}/leads/attach-leads"

    payload = {"lead_ids": [lead_id]}

    response = requests.post(url, headers=bison_headers(api_key), json=payload)

    if response.status_code not in [200, 201]:
        log(f"Failed attaching lead to campaign: {response.text}")
        return None

    return response.json()


def process_reply(lead_id, workspace_name):
    log(f"Received job for lead {lead_id}. Waiting {REPLY_DELAY_SECONDS} seconds before replying.")
    time.sleep(REPLY_DELAY_SECONDS)

    if workspace_name not in CONFIG["workspaces"]:
        log(f"Unknown workspace: {workspace_name}")
        return

    workspace = CONFIG["workspaces"][workspace_name]

    api_key_env = workspace["api_key_env"]
    workspace_bison_api_key = os.getenv(api_key_env)

    if not workspace_bison_api_key:
        log(f"Missing API key for workspace {workspace_name}. Expected env: {api_key_env}")
        return

    reply_followup_campaign_id = workspace["reply_followup_campaign_id"]

    client_profile = load_json_file(f"client_profiles/{workspace['client_profile']}")
    website = workspace.get("website", "")
    default_sender_email = workspace.get("default_sender_email", "")
    default_sender_name = workspace.get("sender_name", "")
    reply_format = load_json_file(f"reply_formats/{workspace['reply_format']}")

    log("Fetching replies...")
    replies = fetch_replies(lead_id, workspace_bison_api_key)

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
    sent_emails = fetch_sent_emails(lead_id, workspace_bison_api_key)

    sender_email = get_sender_email_from_sent_emails(sent_emails) or default_sender_email
    sender_name = sender_name_from_email(sender_email) or default_sender_name or "Team"

    log("Building thread...")
    thread = build_thread(sent_emails, valid_replies)

    log("Generating AI reply + followups...")
    ai_result = generate_ai_reply(client_profile, reply_format, thread)

    if ai_result.get("main_reply"):
        ai_result["main_reply"] = add_signature_once(
            ai_result.get("main_reply", ""),
            sender_name,
            website
        )

    if ai_result.get("intent") == "unsubscribe":
        log("Unsubscribe detected. Not sending.")
        save_processed_reply(reply_id)
        return

    if ai_result.get("human_review_needed") is True:
        log("Human review needed. Not sending.")
        notify_human_review(lead_id, reply_id, workspace_name, thread, ai_result)
        save_processed_reply(reply_id)
        return

    log("Sending immediate reply through EmailBison...")
    send_result = send_reply_to_bison(
        reply_id=reply_id,
        message=format_email_for_bison(ai_result.get("main_reply", "")),
        sender_email_id=latest_reply["sender_email_id"],
        to_name=latest_reply["from_name"],
        to_email=latest_reply["from_email_address"],
        api_key=workspace_bison_api_key
    )

    if send_result:
        save_processed_reply(reply_id)

    log("Updating lead custom variables...")
    update_result = update_bison_lead_variables(
        lead_id=lead_id,
        ai_result=ai_result,
        latest_reply=latest_reply,
        api_key=workspace_bison_api_key
    )

    log("Attaching lead to reply follow-up campaign...")
    attach_result = attach_lead_to_followup_campaign(
        lead_id=lead_id,
        campaign_id=reply_followup_campaign_id,
        api_key=workspace_bison_api_key
    )

    final_log = {
        "lead_id": lead_id,
        "reply_id": reply_id,
        "workspace_name": workspace_name,
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


def process_instantly_reply(payload):
    log("Processing Instantly reply webhook...")
    log(json.dumps(payload, indent=2))

    workspace_name = "Webaholics"
    workspace = CONFIG["workspaces"][workspace_name]

    api_key_env = workspace["api_key_env"]
    instantly_api_key = os.getenv(api_key_env)

    if not instantly_api_key:
        log(f"Missing Instantly API key: {api_key_env}")
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

    client_profile = load_json_file("client_profiles/webaholics.json")
    reply_format = load_json_file("reply_formats/webaholics_reply_formats.json")

    log("Generating Instantly AI reply + followups...")
    ai_result = generate_ai_reply(client_profile, reply_format, thread)

    sender_email = extract_sender_email_from_reply_text(reply_text)

    if not sender_email:
        sender_email = payload.get("eaccount", "") or payload.get("from_email", "")

    sender_name = sender_name_from_email(sender_email) or "Team"
    website = workspace.get("website", "https://www.webaholics.ai")

    if ai_result.get("main_reply"):
        ai_result["main_reply"] = add_signature_once(
            ai_result.get("main_reply", ""),
            sender_name,
            website
        )

    if ai_result.get("intent") == "unsubscribe":
        log("Instantly unsubscribe detected. Not sending.")
        save_log(f"instantly_unsubscribe_{lead_id}.json", {
            "lead_id": lead_id,
            "email": email,
            "payload": payload,
            "ai_result": ai_result
        })
        return

    if ai_result.get("human_review_needed") is True:
        log("Instantly human review needed. Not sending.")
        notify_human_review(lead_id, "instantly", workspace_name, thread, ai_result)
        return

    reply_to_uuid = get_latest_instantly_email_id(
        email=email,
        campaign_id=campaign_id,
        api_key=instantly_api_key
    )

    send_result = None

    if reply_to_uuid:
        if not sender_email:
            log("No sender email found. Skipping reply send to avoid wrong sender.")
        else:
            log("Sending Instantly main reply...")
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

    log("Updating Instantly lead variables...")
    update_result = update_instantly_lead(
        lead_id=lead_id,
        ai_result=ai_result,
        api_key=instantly_api_key
    )

    final_log = {
        "lead_id": lead_id,
        "email": email,
        "first_name": first_name,
        "campaign_id": campaign_id,
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
        "delay_seconds": REPLY_DELAY_SECONDS
    }


@app.post("/instantly-reply")
async def instantly_reply_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()

    background_tasks.add_task(process_instantly_reply, payload)

    return {
        "success": True,
        "message": "Instantly reply received"
    }