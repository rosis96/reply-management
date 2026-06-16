#!/usr/bin/env python3
"""
Password-protected web dashboard for the reply management system.

Parts:
  - Leads table: every processed reply with Reply / FUP status (Bison-style).
  - Lead detail (unibox): the thread + each AI-drafted follow-up step.
  - Control panel: workspaces (API keys, base URLs, campaign IDs, client
    profiles, reply formats) and global settings.

Login: HTTP Basic auth.
  username = env DASHBOARD_USER     (default "admin")
  password = env DASHBOARD_PASSWORD (default "changeme" — SET THIS on Railway)
"""

import os
import json
import secrets
import html as _html

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import db

router = APIRouter()
security = HTTPBasic()


# -------------------------
# Auth
# -------------------------

def require_login(credentials: HTTPBasicCredentials = Depends(security)):
    user = os.getenv("DASHBOARD_USER", "admin")
    password = os.getenv("DASHBOARD_PASSWORD", "changeme")

    ok_user = secrets.compare_digest(credentials.username, user)
    ok_pass = secrets.compare_digest(credentials.password, password)

    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# -------------------------
# HTML helpers
# -------------------------

def e(value):
    return _html.escape("" if value is None else str(value))


def nl2br(value):
    return e(value).replace("\n", "<br>")


PAGE_CSS = """
:root { --bg:#f5f7fb; --card:#ffffff; --line:#e4e8f0; --txt:#1c2230;
        --muted:#6b7385; --accent:#4f6bff; --accent-soft:#eef1ff;
        --ok:#15a36e; --ok-soft:#e6f6ee; --no:#d9434f; --no-soft:#fcebec;
        --warn:#b9770a; --warn-soft:#fdf3e2; --muted-soft:#eef0f5; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--txt);
       font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
a { color:var(--accent); text-decoration:none; }
.nav { display:flex; gap:20px; align-items:center; padding:14px 24px;
       background:var(--card); border-bottom:1px solid var(--line); }
.nav .brand { font-weight:800; margin-right:8px; letter-spacing:-.01em; }
.nav a { color:var(--muted); font-weight:600; font-size:14px; }
.nav a.active { color:var(--txt); }
.wrap { max-width:1180px; margin:0 auto; padding:26px 24px 70px; }
h1 { font-size:21px; margin:0 0 18px; letter-spacing:-.01em; }
h2 { font-size:15px; margin:24px 0 10px; }
.cards { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:22px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:14px;
        padding:14px 18px; min-width:128px; box-shadow:0 1px 2px rgba(20,30,60,.04); }
.stat .n { font-size:25px; font-weight:800; }
.stat .l { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.05em; }
table { width:100%; border-collapse:collapse; background:var(--card);
        border:1px solid var(--line); border-radius:14px; overflow:hidden;
        font-size:13px; box-shadow:0 1px 2px rgba(20,30,60,.04); }
th,td { text-align:left; padding:11px 14px; border-bottom:1px solid var(--line); vertical-align:middle; }
th { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em; background:#fafbfe; }
tr:last-child td { border-bottom:none; }
tbody tr:hover { background:#fafbff; }
.pill { display:inline-block; padding:3px 10px; border-radius:999px; font-size:11px; font-weight:700; }
.pill.ok { background:var(--ok-soft); color:var(--ok); }
.pill.no { background:var(--no-soft); color:var(--no); }
.pill.warn { background:var(--warn-soft); color:var(--warn); }
.pill.muted { background:var(--muted-soft); color:var(--muted); }
.filters { margin-bottom:16px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
select,input[type=text],input[type=password],textarea {
        background:#fff; color:var(--txt); border:1px solid var(--line);
        border-radius:9px; padding:9px 11px; font-size:13px; width:100%; }
select:focus,input:focus,textarea:focus { outline:2px solid var(--accent-soft); border-color:var(--accent); }
textarea { min-height:150px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; line-height:1.5; }
label { display:block; color:var(--muted); font-size:12px; margin:14px 0 5px; font-weight:600; }
.row2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.btn { display:inline-block; background:var(--accent); color:#fff; border:none;
       border-radius:9px; padding:10px 16px; font-weight:700; font-size:13px; cursor:pointer; }
.btn:hover { filter:brightness(1.05); }
.btn.sec { background:#fff; color:var(--txt); border:1px solid var(--line); }
.btn.danger { background:var(--no-soft); color:var(--no); }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px;
        padding:18px 20px; margin-bottom:16px; box-shadow:0 1px 2px rgba(20,30,60,.04); }
.muted { color:var(--muted); }
.small { font-size:12px; }
.flex { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.right { margin-left:auto; }
.viewlink { font-weight:700; }
/* unibox / thread */
.msg { border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin-bottom:12px; background:#fff; }
.msg.in { background:#fbfcff; }
.msg .who { font-size:12px; color:var(--muted); margin-bottom:7px; font-weight:600; }
.msg .body { font-size:14px; line-height:1.6; white-space:normal; }
.fup { border:1px dashed var(--line); border-radius:12px; padding:13px 16px; margin-bottom:10px; background:#fafbfe; }
.fup .step { font-size:11px; font-weight:800; color:var(--accent); text-transform:uppercase; letter-spacing:.05em; margin-bottom:6px; }
.kv { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px; }
.kv .tag { background:var(--muted-soft); color:var(--muted); border-radius:8px; padding:3px 9px; font-size:12px; }
"""


def layout(title, active, body):
    def navlink(href, label, key):
        cls = "active" if key == active else ""
        return f'<a class="{cls}" href="{href}">{label}</a>'

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{PAGE_CSS}</style></head><body>
<div class="nav">
  <span class="brand">Reply Manager</span>
  {navlink("/dashboard", "Leads", "leads")}
  {navlink("/dashboard/workspaces", "Workspaces", "workspaces")}
  {navlink("/dashboard/settings", "Settings", "settings")}
</div>
<div class="wrap">{body}</div></body></html>"""


def yesno(flag, yes="Yes", no="No"):
    if flag:
        return f'<span class="pill ok">{yes}</span>'
    return f'<span class="pill no">{no}</span>'


def action_pill(action):
    if action == "send":
        return '<span class="pill ok">send</span>'
    if action == "skip_enrich":
        return '<span class="pill warn">enrich</span>'
    if action == "stop":
        return '<span class="pill muted">stop</span>'
    if action == "error":
        return '<span class="pill no">error</span>'
    return f'<span class="muted">{e(action)}</span>'


def reply_cell(ld):
    if ld.replied:
        return '<span class="pill ok">Replied</span>'
    if ld.reply_added:
        return '<span class="pill warn">Added, not sent</span>'
    return '<span class="pill no">Not sent</span>'


# -------------------------
# Leads list
# -------------------------

@router.get("/dashboard", response_class=HTMLResponse)
def leads_page(workspace: str = "", status: str = "", _: str = Depends(require_login)):
    counts = db.lead_counts()
    leads = db.list_leads(workspace=workspace or None, status=status or None)
    workspaces = db.list_workspaces()

    ws_options = '<option value="">All workspaces</option>'
    for w in workspaces:
        sel = " selected" if w.name == workspace else ""
        ws_options += f'<option value="{e(w.name)}"{sel}>{e(w.name)}</option>'

    st_options = ""
    for val, lbl in [("", "All statuses"), ("replied", "Replied"),
                     ("enriched", "Enriched only"), ("stopped", "Stopped")]:
        sel = " selected" if val == status else ""
        st_options += f'<option value="{val}"{sel}>{lbl}</option>'

    rows = ""
    if not leads:
        rows = '<tr><td colspan="10" class="muted">No leads yet. They appear here as replies are processed.</td></tr>'
    for ld in leads:
        when = ld.created_at.strftime("%b %d, %H:%M") if ld.created_at else ""
        intent = e(ld.intent) or '<span class="muted">-</span>'
        rows += f"""<tr>
          <td class="small muted">{e(when)}</td>
          <td>{e(ld.workspace_name)}</td>
          <td>{e(ld.name) or '<span class="muted">-</span>'}</td>
          <td class="small">{e(ld.email)}</td>
          <td>{intent}</td>
          <td>{e(ld.confidence)}</td>
          <td>{action_pill(ld.action)}</td>
          <td>{reply_cell(ld)}</td>
          <td>{yesno(ld.fup_added, "Added", "No")}</td>
          <td><a class="viewlink" href="/dashboard/leads/{ld.id}">View &rarr;</a></td>
        </tr>"""

    body = f"""
    <h1>Leads</h1>
    <div class="cards">
      <div class="stat"><div class="n">{counts['total']}</div><div class="l">Total</div></div>
      <div class="stat"><div class="n">{counts['replied']}</div><div class="l">Replied</div></div>
      <div class="stat"><div class="n">{counts['enriched']}</div><div class="l">FUP added</div></div>
      <div class="stat"><div class="n">{counts['stopped']}</div><div class="l">Stopped</div></div>
    </div>
    <form class="filters" method="get" action="/dashboard">
      <select name="workspace" style="width:auto">{ws_options}</select>
      <select name="status" style="width:auto">{st_options}</select>
      <button class="btn sec" type="submit">Filter</button>
    </form>
    <table>
      <thead><tr>
        <th>When</th><th>Workspace</th><th>Name</th><th>Email</th>
        <th>Intent</th><th>Conf</th><th>Action</th><th>Reply</th><th>FUP</th><th></th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """
    return HTMLResponse(layout("Leads", "leads", body))


# -------------------------
# Lead detail (unibox)
# -------------------------

@router.get("/dashboard/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, _: str = Depends(require_login)):
    ld = db.get_lead(lead_id)
    if ld is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    try:
        followups = json.loads(ld.followups) if ld.followups else []
    except Exception:
        followups = []
    try:
        thread = json.loads(ld.thread) if ld.thread else []
    except Exception:
        thread = []

    # --- conversation preview ---
    convo = ""
    if ld.reply_text:
        convo += f"""<div class="msg in">
            <div class="who">Prospect &mdash; {e(ld.name)} &lt;{e(ld.email)}&gt;</div>
            <div class="body">{nl2br(ld.reply_text)}</div></div>"""
    if ld.main_reply:
        sent_label = "Sent reply" if ld.replied else "Drafted reply (not sent — for you to send manually)"
        convo += f"""<div class="msg">
            <div class="who">{e(sent_label)}</div>
            <div class="body">{nl2br(ld.main_reply)}</div></div>"""
    if not convo and thread:
        for m in thread:
            who = m.get("type", "message")
            body = m.get("body", "") or m.get("text_body", "")
            convo += f"""<div class="msg"><div class="who">{e(who)}</div>
                <div class="body">{nl2br(body)}</div></div>"""
    if not convo:
        convo = '<p class="muted">No conversation text was captured for this lead.</p>'

    # --- follow-up sequence ---
    fup_html = ""
    real_fups = [(i + 1, f) for i, f in enumerate(followups) if (f or "").strip()]
    if real_fups:
        for step, text_ in real_fups:
            fup_html += f"""<div class="fup">
                <div class="step">Follow-up {step}</div>
                <div class="body">{nl2br(text_)}</div></div>"""
    else:
        fup_html = '<p class="muted">No follow-up drafts were generated for this lead.</p>'

    when = ld.created_at.strftime("%b %d, %Y %H:%M") if ld.created_at else ""

    body = f"""
    <a href="/dashboard" class="muted small">&larr; Back to leads</a>
    <h1>{e(ld.name) or e(ld.email) or 'Lead'}</h1>
    <div class="kv">
      <span class="tag">{e(ld.email)}</span>
      <span class="tag">{e(ld.workspace_name)}</span>
      <span class="tag">{e(ld.platform)}</span>
      {f'<span class="tag">{e(ld.campaign)}</span>' if ld.campaign else ''}
      <span class="tag">{e(when)}</span>
    </div>
    <div class="flex" style="margin-bottom:18px">
      <span>Intent: <b>{e(ld.intent) or '-'}</b></span>
      <span class="muted">conf {e(ld.confidence)}</span>
      {action_pill(ld.action)}
      {reply_cell(ld)}
      <span>FUP: {yesno(ld.fup_added, "Added", "No")}</span>
    </div>

    <div class="card">
      <h2 style="margin-top:0">Conversation</h2>
      {convo}
    </div>

    <div class="card">
      <h2 style="margin-top:0">Follow-up sequence (AI-drafted)</h2>
      <p class="muted small" style="margin-top:0">These are loaded into the platform as follow-up variables and sent on schedule.</p>
      {fup_html}
    </div>
    """
    return HTMLResponse(layout(ld.name or "Lead", "leads", body))


# -------------------------
# Workspaces
# -------------------------

@router.get("/dashboard/workspaces", response_class=HTMLResponse)
def workspaces_page(_: str = Depends(require_login)):
    workspaces = db.list_workspaces()
    rows = ""
    if not workspaces:
        rows = '<tr><td colspan="5" class="muted">No workspaces yet.</td></tr>'
    for w in workspaces:
        key_state = yesno(bool(w.api_key), "Set", "Missing")
        rows += f"""<tr>
          <td><a href="/dashboard/workspaces/{w.id}">{e(w.name)}</a></td>
          <td>{e(w.platform)}</td>
          <td>{key_state}</td>
          <td>{e(w.reply_followup_campaign_id)}</td>
          <td>{yesno(w.active, "Active", "Off")}</td>
        </tr>"""

    body = f"""
    <div class="flex">
      <h1>Workspaces</h1>
      <a class="btn right" href="/dashboard/workspaces/new">+ Add workspace</a>
    </div>
    <table>
      <thead><tr><th>Name</th><th>Platform</th><th>API key</th><th>Follow-up campaign</th><th>Status</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """
    return HTMLResponse(layout("Workspaces", "workspaces", body))


def _workspace_form(ws=None, error=""):
    is_new = ws is None
    name = "" if is_new else ws.name
    platform = "bison" if is_new else (ws.platform or "bison")
    api_key = "" if is_new else (ws.api_key or "")
    base_url = "" if is_new else (ws.base_url or "")
    camp = "" if is_new else (ws.reply_followup_campaign_id or "")
    website = "" if is_new else (ws.website or "")
    sender = "" if is_new else (ws.sender_name or "")
    sender_email = "" if is_new else (ws.default_sender_email or "")
    active_checked = "checked" if (is_new or ws.active) else ""
    profile = "{}" if is_new else json.dumps(ws.client_profile or {}, indent=2)
    rformat = "{}" if is_new else json.dumps(ws.reply_format or {}, indent=2)

    action = "/dashboard/workspaces" if is_new else f"/dashboard/workspaces/{ws.id}"
    title = "Add workspace" if is_new else f"Edit: {name}"

    bison_sel = " selected" if platform == "bison" else ""
    inst_sel = " selected" if platform == "instantly" else ""

    err_html = f'<div class="card" style="border-color:var(--no);color:var(--no)">{e(error)}</div>' if error else ""

    delete_btn = ""
    if not is_new:
        delete_btn = f"""<form method="post" action="/dashboard/workspaces/{ws.id}/delete"
            onsubmit="return confirm('Delete this workspace?')" style="display:inline">
            <button class="btn danger" type="submit">Delete</button></form>"""

    body = f"""
    <a href="/dashboard/workspaces" class="muted small">&larr; Back to workspaces</a>
    <h1>{e(title)}</h1>
    {err_html}
    <form method="post" action="{action}">
      <div class="card">
        <div class="row2">
          <div><label>Workspace name</label><input type="text" name="name" value="{e(name)}" required></div>
          <div><label>Platform</label>
            <select name="platform"><option value="bison"{bison_sel}>bison</option><option value="instantly"{inst_sel}>instantly</option></select>
          </div>
        </div>
        <label>API key</label>
        <input type="text" name="api_key" value="{e(api_key)}" placeholder="Bison or Instantly API key">
        <div class="row2">
          <div><label>Base URL (Bison only)</label><input type="text" name="base_url" value="{e(base_url)}" placeholder="https://your-bison-instance.com"></div>
          <div><label>Follow-up campaign ID</label><input type="text" name="reply_followup_campaign_id" value="{e(camp)}"></div>
        </div>
        <div class="row2">
          <div><label>Website (signature)</label><input type="text" name="website" value="{e(website)}"></div>
          <div><label>Default sender name</label><input type="text" name="sender_name" value="{e(sender)}"></div>
        </div>
        <label>Default sender email</label>
        <input type="text" name="default_sender_email" value="{e(sender_email)}">
        <label class="flex"><input type="checkbox" name="active" value="1" {active_checked} style="width:auto;margin-right:8px"> Active</label>
      </div>

      <div class="card">
        <h2 style="margin-top:0">Client profile (JSON)</h2>
        <p class="muted small">Positioning, offer, target customers, tone.</p>
        <textarea name="client_profile">{e(profile)}</textarea>
      </div>

      <div class="card">
        <h2 style="margin-top:0">Reply format (JSON)</h2>
        <p class="muted small">Intents, word counts, CTA style, scheduling language.</p>
        <textarea name="reply_format">{e(rformat)}</textarea>
      </div>

      <div class="flex">
        <button class="btn" type="submit">Save</button>
        {delete_btn}
      </div>
    </form>
    """
    return layout(title, "workspaces", body)


@router.get("/dashboard/workspaces/new", response_class=HTMLResponse)
def workspace_new(_: str = Depends(require_login)):
    return HTMLResponse(_workspace_form(None))


@router.get("/dashboard/workspaces/{ws_id}", response_class=HTMLResponse)
def workspace_edit(ws_id: int, _: str = Depends(require_login)):
    ws = db.get_workspace(ws_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return HTMLResponse(_workspace_form(ws))


def _parse_json_field(raw, field_name):
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as ex:
        raise ValueError(f"{field_name} is not valid JSON: {ex}")


async def _read_workspace_form(request: Request):
    form = await request.form()
    data = {
        "name": form.get("name", ""),
        "platform": form.get("platform", "bison"),
        "api_key": form.get("api_key", ""),
        "base_url": form.get("base_url", ""),
        "reply_followup_campaign_id": form.get("reply_followup_campaign_id", ""),
        "website": form.get("website", ""),
        "sender_name": form.get("sender_name", ""),
        "default_sender_email": form.get("default_sender_email", ""),
        "active": form.get("active") == "1",
        "client_profile": _parse_json_field(form.get("client_profile"), "Client profile"),
        "reply_format": _parse_json_field(form.get("reply_format"), "Reply format"),
    }
    return data


@router.post("/dashboard/workspaces")
async def workspace_create(request: Request, _: str = Depends(require_login)):
    try:
        data = await _read_workspace_form(request)
    except ValueError as ex:
        return HTMLResponse(_workspace_form(None, error=str(ex)))
    db.save_workspace(None, data)
    return RedirectResponse("/dashboard/workspaces", status_code=303)


@router.post("/dashboard/workspaces/{ws_id}")
async def workspace_update(ws_id: int, request: Request, _: str = Depends(require_login)):
    ws = db.get_workspace(ws_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        data = await _read_workspace_form(request)
    except ValueError as ex:
        return HTMLResponse(_workspace_form(ws, error=str(ex)))
    db.save_workspace(ws_id, data)
    return RedirectResponse("/dashboard/workspaces", status_code=303)


@router.post("/dashboard/workspaces/{ws_id}/delete")
def workspace_delete(ws_id: int, _: str = Depends(require_login)):
    db.delete_workspace(ws_id)
    return RedirectResponse("/dashboard/workspaces", status_code=303)


# -------------------------
# Settings
# -------------------------

@router.get("/dashboard/settings", response_class=HTMLResponse)
def settings_page(saved: str = "", _: str = Depends(require_login)):
    s = db.all_settings()
    saved_html = '<div class="card" style="border-color:var(--ok);color:var(--ok)">Saved.</div>' if saved else ""
    body = f"""
    <h1>Settings</h1>
    {saved_html}
    <form method="post" action="/dashboard/settings">
      <div class="card">
        <label>OpenAI API key</label>
        <input type="text" name="openai_api_key" value="{e(s.get('openai_api_key',''))}">
        <label>Human review webhook URL</label>
        <input type="text" name="human_review_webhook_url" value="{e(s.get('human_review_webhook_url',''))}">
        <div class="row2">
          <div><label>Default EmailBison base URL</label><input type="text" name="emailbison_base_url" value="{e(s.get('emailbison_base_url',''))}"></div>
          <div><label>Reply delay (seconds)</label><input type="text" name="reply_delay_seconds" value="{e(s.get('reply_delay_seconds','420'))}"></div>
        </div>
      </div>
      <button class="btn" type="submit">Save settings</button>
    </form>
    <p class="muted small" style="margin-top:18px">
      The dashboard password is set with the <b>DASHBOARD_PASSWORD</b> environment variable on Railway (not here, for security).
    </p>
    """
    return HTMLResponse(layout("Settings", "settings", body))


@router.post("/dashboard/settings")
async def settings_save(request: Request, _: str = Depends(require_login)):
    form = await request.form()
    for key in ["openai_api_key", "human_review_webhook_url",
                "emailbison_base_url", "reply_delay_seconds"]:
        db.set_setting(key, form.get(key, ""))
    return RedirectResponse("/dashboard/settings?saved=1", status_code=303)
