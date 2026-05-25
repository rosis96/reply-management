#!/usr/bin/env python3

import os
import json
import time
import requests
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from fastapi import FastAPI, BackgroundTasks, Request

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMAILBISON_API_KEY = os.getenv("EMAILBISON_API_KEY")
EMAILBISON_BASE_URL = os.getenv("EMAILBISON_BASE_URL", "").rstrip("/")

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

with open("config.json", "r") as f:
    CONFIG = json.load(f)


REPLY_DELAY_SECONDS = 180
PROCESSED_FILE = "logs/processed_replies.json"


def log(message):
    print(f"[{datetime.now()}] {message}")


def bison_headers():
    return {
        "Authorization": f"Bearer {EMAILBISON_API_KEY}",
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


def fetch_replies(lead_id):
    url = f"{EMAILBISON_BASE_URL}/api/leads/{lead_id}/replies"
    response = requests.get(url, headers=bison_headers())

    if response.status_code != 200:
        log(f"Failed fetching replies: {response.text}")
        return []

    return response.json().get("data", [])


def fetch_sent_emails(lead_id):
    url = f"{EMAILBISON_BASE_URL}/api/leads/{lead_id}/sent-emails"
    response = requests.get(url, headers=bison_headers())

    if response.status_code != 200:
        log(f"Failed fetching sent emails: {response.text}")
        return []

    return response.json().get("data", [])


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


def generate_ai_reply(client_profile, thread):
    prompt = f"""
You are an expert outbound sales reply assistant.

CLIENT PROFILE:
{json.dumps(client_profile, indent=2)}

EMAIL THREAD:
{json.dumps(thread, indent=2)}

TASK:
Write the immediate reply we should send now and generate 5 future follow-ups.

RULES:
- Sound human
- Do not sound robotic
- Do not over-explain
- Match the prospect's tone
- Continue from the actual thread
- Keep main_reply under 90 words
- Use short paragraphs
- Use a soft CTA
- Do not invent facts
- If the prospect is clearly asking to unsubscribe, reply should be empty and intent should be "unsubscribe"
- If the prospect is automated/out of office/wrong person, mark human_review_needed true

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
  "followup_5": ""
}}
"""

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


def send_reply_to_bison(reply_id, message, sender_email_id, to_name, to_email):
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

    response = requests.post(url, headers=bison_headers(), json=payload)

    if response.status_code not in [200, 201]:
        log(f"Failed sending reply: {response.text}")
        return None

    return response.json()


def update_bison_lead_variables(lead_id, ai_result, latest_reply):
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
            {"name": "reply_intent", "value": ai_result.get("intent", "")},
            {"name": "reply_confidence", "value": str(ai_result.get("confidence", ""))}
        ]
    }

    response = requests.put(url, headers=bison_headers(), json=payload)

    if response.status_code not in [200, 201]:
        log(f"Failed updating lead variables: {response.text}")
        return None

    return response.json()


def attach_lead_to_followup_campaign(lead_id, campaign_id):
    url = f"{EMAILBISON_BASE_URL}/api/campaigns/{campaign_id}/leads/attach-leads"

    payload = {
        "lead_ids": [lead_id]
    }

    response = requests.post(url, headers=bison_headers(), json=payload)

    if response.status_code not in [200, 201]:
        log(f"Failed attaching lead to campaign: {response.text}")
        return None

    return response.json()


def process_reply(lead_id, workspace_name):
    log(f"Received job for lead {lead_id}. Waiting {REPLY_DELAY_SECONDS} seconds before replying.")
    time.sleep(REPLY_DELAY_SECONDS)

    workspace = CONFIG["workspaces"][workspace_name]
    reply_followup_campaign_id = workspace["reply_followup_campaign_id"]
    client_profile = load_json_file(f"client_profiles/{workspace['client_profile']}")

    log("Fetching replies...")
    replies = fetch_replies(lead_id)

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
    sent_emails = fetch_sent_emails(lead_id)

    log("Building thread...")
    thread = build_thread(sent_emails, valid_replies)

    log("Generating AI reply + followups...")
    ai_result = generate_ai_reply(client_profile, thread)

    if ai_result.get("human_review_needed") is True:
        log("Human review needed. Not sending.")
        save_log(f"human_review_needed_{lead_id}_{reply_id}.json", {
            "lead_id": lead_id,
            "reply_id": reply_id,
            "thread": thread,
            "ai_result": ai_result
        })
        return

    if ai_result.get("intent") == "unsubscribe":
        log("Unsubscribe detected. Not sending.")
        save_log(f"unsubscribe_detected_{lead_id}_{reply_id}.json", {
            "lead_id": lead_id,
            "reply_id": reply_id,
            "thread": thread,
            "ai_result": ai_result
        })
        save_processed_reply(reply_id)
        return

    log("Sending immediate reply through EmailBison...")
    send_result = send_reply_to_bison(
        reply_id=reply_id,
        message=ai_result["main_reply"],
        sender_email_id=latest_reply["sender_email_id"],
        to_name=latest_reply["from_name"],
        to_email=latest_reply["from_email_address"]
    )

    if send_result:
        save_processed_reply(reply_id)

    log("Updating lead custom variables...")
    update_result = update_bison_lead_variables(lead_id, ai_result, latest_reply)

    log("Attaching lead to reply follow-up campaign...")
    attach_result = attach_lead_to_followup_campaign(
        lead_id=lead_id,
        campaign_id=reply_followup_campaign_id
    )

    final_log = {
        "lead_id": lead_id,
        "reply_id": reply_id,
        "workspace_name": workspace_name,
        "latest_reply": latest_reply,
        "thread": thread,
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


@app.get("/")
def health_check():
    return {"status": "reply_management_running"}


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


@app.get("/test")
def test_run():
    process_reply(20470, "Insight Media Labs")
    return {"success": True, "message": "Test completed"}