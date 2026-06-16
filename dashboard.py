#!/usr/bin/env python3
"""
Password-protected web dashboard for the reply management system.

Two parts:
  - Leads table: every processed reply with Reply / FUP status.
  - Control panel: workspaces (API keys, base URLs, campaign IDs, client
    profiles, reply formats) and global settings (OpenAI key, review webhook,
    reply delay) — all editable in the browser, no file editing needed.

Login: HTTP Basic auth.
  username = env DASHBOARD_USER     (default "admin")
  password = env DASHBOARD_PASSWORD (default "changeme" — SET THIS on Railway)
"""

import os
import json
import secrets
import html as _html

from fastapi import APIRouter, Depends, Request, Form, HTTPException
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


PAGE_CSS = """
:root { --bg:#0f1220; --card:#191d2e; --line:#2a3050; --txt:#e7e9f3;
        --muted:#9aa3c7; --accent:#6c8cff; --ok:#34d399; --no:#f87171; --warn:#fbbf24; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--txt);
       font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
a { color:var(--accent); text-decoration:none; }
.nav { display:flex; gap:18px; align-items:center; padding:14px 22px;
       background:var(--card); border-bottom:1px solid var(--line); }
.nav .brand { font-weight:700; margin-right:10px; }
.nav a { color:var(--muted); font-weight:600; }
.nav a.active { color:var(--txt); }
.wrap { max-width:1180px; margin:0 auto; padding:24px 22px 60px; }
h1 { font-size:20px; margin:0 0 16px; }
h2 { font-size:16px; margin:26px 0 10px; }
.cards { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:20px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:14px 18px; min-width:130px; }
.stat .n { font-size:24px; font-weight:700; }
.stat .l { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
table { width:100%; border-collapse:collapse; background:var(--card);
        border:1px solid var(--line); border-radius:12px; overflow:hidden; font-size:13px; }
th,td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em; }
tr:last-child td { border-bottom:none; }
.pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:11px; font-weight:700; }
.pill.ok { background:rgba(52,211,153,.15); color:var(--ok); }
.pill.no { background:rgba(248,113,113,.15); color:var(--no); }
.pill.warn { background:rgba(251,191,36,.15); color:var(--warn); }
.pill.muted { background:rgba(154,163,199,.15); color:var(--muted); }
.filters { margin-bottom:14px; display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
select,input[type=text],input[type=password],textarea {
        background:#11152a; color:var(--txt); border:1px solid var(--line);
        border-radius:8px; padding:9px 11px; font-size:13px; width:100%; }
textarea { min-height:150px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
label { display:block; color:var(--muted); font-size:12px; margin:14px 0 5px; font-weight:600; }
.row2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.btn { display:inline-block; background:var(--accent); color:#fff; border:none;
       border-radius:8px; padding:10px 16px; font-weight:700; font-size:13px; cursor:pointer; }
.btn.sec { background:#2a3050; }
.btn.danger { background:#3a1d2a; color:var(--no); }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px 20px; margin-bottom:16px; }
.muted { color:var(--muted); }
.small { font-size:12px; }
.flex { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.right { margin-left:auto; }
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


# -------------------------
# Leads
# -------------------------

@router.get("/dashboard", response_class=HTMLResponse)
def leads_page(request: Request, workspace: str = "", status: str = "", _: str = Depends(require_login)):
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
        rows = '<tr><td colspan="9" class="muted">No leads yet. They appear here as replies are processed.</td></tr>'
    for ld in leads:
        when = ld.created_at.strftime("%b %d, %H:%M") if ld.created_at else ""
        intent = e(ld.intent) or '<span class="muted">-</span>'

        if ld.action == "send":
            action_pill = '<span class="pill ok">send</span>'
        elif ld.action == "skip_enrich":
            action_pill = '<span class="pill warn">enrich</span>'
        elif ld.action == "stop":
            action_pill = '<span class="pill muted">stop</span>'
        else:
            action_pill = f'<span class="muted">{e(ld.action)}</span>'

        reply_cell = yesno(ld.replied, "Replied", "Not sent")
        if ld.reply_added and not ld.replied:
            reply_cell = '<span class="pill warn">Added, not sent</span>'

        rows += f"""<tr>
          <td class="small muted">{e(when)}</td>
          <td>{e(ld.workspace_name)}</td>
          <td>{e(ld.name) or '<span class="muted">-</span>'}</td>
          <td class="small">{e(ld.email)}</td>
          <td>{intent}</td>
          <td>{e(ld.confidence)}</td>
          <td>{action_pill}</td>
          <td>{reply_cell}</td>
          <td>{yesno(ld.fup_added, "Added", "No")}</td>
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
        <th>Intent</th><th>Conf</th><th>Action</th><th>Reply</th><th>FUP</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    """
    return HTMLResponse(layout("Leads", "leads", body))


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
        <h2>Client profile (JSON)</h2>
        <p class="muted small">Positioning, offer, target customers, tone.</p>
        <textarea name="client_profile">{e(profile)}</textarea>
      </div>

      <div class="card">
        <h2>Reply format (JSON)</h2>
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
