#!/usr/bin/env python3
"""
Reply Manager — a polished reply-operations console.

Layout: left sidebar + topbar + main content, with a right-side detail drawer
for reviewing/acting on a reply without leaving the list.

Login: HTTP Basic auth.
  username = env DASHBOARD_USER     (default "admin")
  password = env DASHBOARD_PASSWORD (default "changeme" — SET THIS on Railway)
"""

import os
import io
import csv
import json
import math
import secrets
import html as _html
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import db

router = APIRouter()
security = HTTPBasic()

PAGE_SIZE = 25


# -------------------------
# Auth
# -------------------------

def require_login(credentials: HTTPBasicCredentials = Depends(security)):
    user = os.getenv("DASHBOARD_USER", "admin")
    password = os.getenv("DASHBOARD_PASSWORD", "changeme")
    ok = (secrets.compare_digest(credentials.username, user)
          and secrets.compare_digest(credentials.password, password))
    if not ok:
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": "Basic"})
    return credentials.username


# -------------------------
# HTML helpers
# -------------------------

def e(value):
    return _html.escape("" if value is None else str(value))


def nl2br(value):
    return e(value).replace("\n", "<br>")


def pill(text, kind="muted"):
    return f'<span class="pill {kind}">{e(text)}</span>'


def action_badge(action):
    return {
        "send": pill("Send", "ok"),
        "skip_enrich": pill("Enrich", "warn"),
        "stop": pill("Stopped", "muted"),
        "error": pill("Error", "no"),
    }.get(action, pill(action or "-", "muted"))


def reply_badge(ld):
    if ld.replied:
        return pill("Replied", "ok")
    if ld.reply_added:
        return pill("Drafted", "warn")
    return pill("Not sent", "no")


def fup_badge(ld):
    return pill("Added", "blue") if ld.fup_added else pill("No", "muted")


def review_badge(ld):
    if ld.reviewed:
        return pill("Reviewed", "ok")
    if ld.action in ("skip_enrich", "error"):
        return pill("Needs review", "warn")
    return pill("—", "muted")


PAGE_CSS = """
:root { --bg:#f4f6fb; --panel:#ffffff; --line:#e6e9f2; --txt:#1b2230; --muted:#71798e;
        --accent:#4f6bff; --accent-soft:#eef1ff; --ok:#108b5b; --ok-soft:#e6f6ee;
        --no:#d23b48; --no-soft:#fcebed; --warn:#a8730a; --warn-soft:#fdf2dd;
        --blue:#2563c9; --blue-soft:#e7eefc; --muted-soft:#eef0f5; --sidebar:#0f1426; }
* { box-sizing:border-box; }
html,body { margin:0; height:100%; }
body { background:var(--bg); color:var(--txt);
       font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; font-size:14px; }
a { color:var(--accent); text-decoration:none; }
.app { display:flex; min-height:100vh; }
/* sidebar */
.sidebar { width:230px; background:var(--sidebar); color:#c7cde0; flex:0 0 230px;
           position:sticky; top:0; height:100vh; display:flex; flex-direction:column; padding:16px 12px; }
.sidebar .logo { color:#fff; font-weight:800; font-size:16px; padding:6px 10px 16px; letter-spacing:-.01em; }
.sidebar .navi { display:flex; flex-direction:column; gap:2px; overflow:auto; }
.sidebar a { color:#aab2cd; padding:9px 12px; border-radius:9px; font-weight:600; font-size:13.5px;
             display:flex; align-items:center; gap:9px; }
.sidebar a:hover { background:rgba(255,255,255,.06); color:#fff; }
.sidebar a.active { background:var(--accent); color:#fff; }
.sidebar .spacer { flex:1; }
.sidebar .me { display:flex; gap:10px; align-items:center; padding:10px; border-top:1px solid rgba(255,255,255,.08); margin-top:8px; }
.sidebar .ava { width:34px; height:34px; border-radius:50%; background:var(--accent); color:#fff;
                display:flex; align-items:center; justify-content:center; font-weight:800; font-size:13px; }
.sidebar .me .nm { color:#fff; font-weight:700; font-size:13px; }
.sidebar .me .ws { color:#8a93b2; font-size:11.5px; }
/* main */
.main { flex:1; min-width:0; display:flex; flex-direction:column; }
.topbar { position:sticky; top:0; z-index:30; background:var(--panel); border-bottom:1px solid var(--line);
          display:flex; align-items:center; gap:18px; padding:11px 22px; }
.topbar .tabs { display:flex; gap:16px; }
.topbar .tabs a { color:var(--muted); font-weight:600; font-size:13.5px; }
.topbar .tabs a.active { color:var(--txt); }
.content { padding:22px 24px 60px; }
h1 { font-size:20px; margin:0 0 4px; letter-spacing:-.01em; }
.sub { color:var(--muted); margin:0 0 18px; font-size:13px; }
/* stat cards */
.cards { display:grid; grid-template-columns:repeat(6,1fr); gap:12px; margin-bottom:18px; }
.stat { background:var(--panel); border:1px solid var(--line); border-radius:13px; padding:13px 15px;
        box-shadow:0 1px 2px rgba(20,30,60,.04); }
.stat .n { font-size:23px; font-weight:800; }
.stat .l { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em; margin-top:2px; }
.stat.click { cursor:pointer; } .stat.click:hover { border-color:var(--accent); }
/* filter bar */
.filterbar { background:var(--panel); border:1px solid var(--line); border-radius:13px; padding:13px 15px; margin-bottom:16px;
             display:flex; gap:10px; flex-wrap:wrap; align-items:center; box-shadow:0 1px 2px rgba(20,30,60,.04); }
.filterbar .grow { flex:1; min-width:200px; }
select,input[type=text],input[type=date],input[type=password],textarea {
        background:#fff; color:var(--txt); border:1px solid var(--line); border-radius:9px;
        padding:8px 11px; font-size:13px; }
select:focus,input:focus,textarea:focus { outline:2px solid var(--accent-soft); border-color:var(--accent); }
textarea { width:100%; min-height:120px; font-family:inherit; line-height:1.55; }
.btn { background:var(--accent); color:#fff; border:none; border-radius:9px; padding:9px 15px;
       font-weight:700; font-size:13px; cursor:pointer; }
.btn:hover { filter:brightness(1.05); }
.btn.sec { background:#fff; color:var(--txt); border:1px solid var(--line); }
.btn.danger { background:var(--no-soft); color:var(--no); }
.btn.ok { background:var(--ok-soft); color:var(--ok); }
.btn.sm { padding:6px 11px; font-size:12px; }
/* table */
.tablewrap { background:var(--panel); border:1px solid var(--line); border-radius:13px; overflow:hidden;
             box-shadow:0 1px 2px rgba(20,30,60,.04); }
table { width:100%; border-collapse:collapse; font-size:13px; }
thead th { position:sticky; top:0; background:#f7f9fd; color:var(--muted); font-size:11px; text-transform:uppercase;
           letter-spacing:.04em; text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); z-index:1; }
tbody td { padding:11px 12px; border-bottom:1px solid var(--line); vertical-align:middle; }
tbody tr:last-child td { border-bottom:none; }
tbody tr:hover { background:#fafbff; }
.pill { display:inline-block; padding:3px 9px; border-radius:999px; font-size:11px; font-weight:700; white-space:nowrap; }
.pill.ok { background:var(--ok-soft); color:var(--ok); }
.pill.no { background:var(--no-soft); color:var(--no); }
.pill.warn { background:var(--warn-soft); color:var(--warn); }
.pill.blue { background:var(--blue-soft); color:var(--blue); }
.pill.muted { background:var(--muted-soft); color:var(--muted); }
.muted { color:var(--muted); } .small { font-size:12px; }
.linkbtn { color:var(--accent); font-weight:700; cursor:pointer; background:none; border:none; font-size:13px; }
.empty { padding:46px 20px; text-align:center; color:var(--muted); }
.pager { display:flex; gap:8px; align-items:center; justify-content:flex-end; margin-top:14px; }
.pager a, .pager span { padding:6px 11px; border:1px solid var(--line); border-radius:8px; background:#fff; font-size:13px; }
.pager .cur { background:var(--accent); color:#fff; border-color:var(--accent); }
/* bulk bar */
.bulkbar { display:none; gap:10px; align-items:center; background:var(--accent-soft); border:1px solid var(--accent);
           border-radius:11px; padding:9px 14px; margin-bottom:12px; }
.bulkbar.show { display:flex; }
/* workspace switcher */
.ws-switch { position:relative; margin-left:auto; }
.ws-btn { background:#fff; border:1px solid var(--line); border-radius:9px; padding:7px 13px; font-weight:700; font-size:13px; cursor:pointer; color:var(--txt); }
.ws-btn:hover { border-color:var(--accent); }
.ws-menu { position:absolute; top:118%; right:0; width:262px; background:#fff; border:1px solid var(--line);
           border-radius:12px; box-shadow:0 10px 28px rgba(20,30,60,.16); padding:8px; display:none; z-index:80; }
.ws-menu.open { display:block; }
.ws-search { width:100%; margin-bottom:6px; }
.ws-list { max-height:300px; overflow:auto; display:flex; flex-direction:column; }
.ws-item { padding:8px 10px; border-radius:8px; color:var(--txt); display:block; }
.ws-item:hover { background:var(--accent-soft); } .ws-item.sel { background:var(--accent-soft); font-weight:700; }
/* drawer */
.overlay { position:fixed; inset:0; background:rgba(15,20,38,.38); display:none; z-index:90; }
.overlay.show { display:block; }
.drawer { position:fixed; top:0; right:0; height:100vh; width:560px; max-width:94vw; background:var(--panel);
          border-left:1px solid var(--line); box-shadow:-12px 0 40px rgba(20,30,60,.18); transform:translateX(100%);
          transition:transform .18s ease; z-index:100; display:flex; flex-direction:column; }
.drawer.show { transform:translateX(0); }
.drawer .dhead { display:flex; align-items:center; gap:10px; padding:14px 18px; border-bottom:1px solid var(--line); }
.drawer .dbody { padding:18px; overflow:auto; }
.card { background:#fff; border:1px solid var(--line); border-radius:12px; padding:15px 16px; margin-bottom:14px; }
.card h3 { margin:0 0 9px; font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }
.msg { border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin-bottom:10px; }
.msg.in { background:#fbfcff; }
.msg .who { font-size:12px; color:var(--muted); margin-bottom:6px; font-weight:600; }
.msg .body { line-height:1.6; }
.fup { border:1px dashed var(--line); border-radius:10px; padding:11px 14px; margin-bottom:9px; background:#fafbfe; }
.fup .step { font-size:11px; font-weight:800; color:var(--accent); text-transform:uppercase; margin-bottom:5px; }
.kv { display:flex; gap:7px; flex-wrap:wrap; margin-bottom:10px; }
.kv .tag { background:var(--muted-soft); color:var(--muted); border-radius:8px; padding:3px 9px; font-size:12px; }
.drawer .dfoot { margin-top:auto; padding:13px 18px; border-top:1px solid var(--line); display:flex; gap:8px; flex-wrap:wrap; }
.row2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
label { display:block; color:var(--muted); font-size:12px; margin:13px 0 5px; font-weight:600; }
.flex { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.right { margin-left:auto; }
@media (max-width:900px){ .cards{grid-template-columns:repeat(2,1fr);} .sidebar{display:none;} }
"""

SIDEBAR_ITEMS = [
    ("Dashboard", "/dashboard", "reply"),
    ("Leads", "/dashboard", "reply"),
    ("Campaigns", "/dashboard/soon?name=Campaigns", "soon"),
    ("Master Inbox", "/dashboard/soon?name=Master+Inbox", "soon"),
    ("Sender Emails", "/dashboard/soon?name=Sender+Emails", "soon"),
    ("Email Warmup", "/dashboard/soon?name=Email+Warmup", "soon"),
    ("Sending Schedule", "/dashboard/soon?name=Sending+Schedule", "soon"),
    ("Integrations", "/dashboard/soon?name=Integrations", "soon"),
    ("Settings", "/dashboard/settings", "settings"),
    ("Help & Support", "/dashboard/soon?name=Help+%26+Support", "soon"),
]

DRAWER_JS = """
var LEAD_IDS = window.__LEAD_IDS__ || [];
var curIdx = -1;
function openDrawer(id){
  curIdx = LEAD_IDS.indexOf(parseInt(id));
  fetch('/dashboard/leads/'+id+'/panel').then(function(r){return r.text();}).then(function(h){
    document.getElementById('drawer-content').innerHTML = h;
    document.getElementById('overlay').classList.add('show');
    document.getElementById('drawer').classList.add('show');
  });
}
function closeDrawer(){
  document.getElementById('overlay').classList.remove('show');
  document.getElementById('drawer').classList.remove('show');
}
function drawerNav(step){
  if(!LEAD_IDS.length) return;
  var i = curIdx + step;
  if(i<0||i>=LEAD_IDS.length) return;
  openDrawer(LEAD_IDS[i]);
}
function leadAction(id, action){
  var ta = document.getElementById('fup-edit');
  var fd = new FormData();
  fd.append('action', action);
  if(ta) fd.append('followup', ta.value);
  return fetch('/dashboard/leads/'+id+'/action', {method:'POST', body:fd})
    .then(function(r){return r.json();})
    .then(function(d){ if(d && d.ok){ if(action==='stop'||action==='mark_done'){ location.reload(); } else { openDrawer(id); } } });
}
function wsFilter(q){q=q.toLowerCase();document.querySelectorAll('.ws-item').forEach(function(el){el.style.display=el.textContent.toLowerCase().indexOf(q)>-1?'block':'none';});}
function toggleWs(ev){var m=document.getElementById('wsmenu');m.classList.toggle('open');ev.stopPropagation();}
document.addEventListener('click',function(){var m=document.getElementById('wsmenu');if(m)m.classList.remove('open');});
// auto-apply filters
function autoApply(){ var f=document.getElementById('filterform'); if(f) f.submit(); }
var _t;
function autoApplySearch(){ clearTimeout(_t); _t=setTimeout(autoApply, 450); }
// bulk
function rowChecked(){
  var ids=[].slice.call(document.querySelectorAll('.rowchk:checked')).map(function(c){return c.value;});
  var bar=document.getElementById('bulkbar');
  if(ids.length){ bar.classList.add('show'); document.getElementById('bulkcount').textContent=ids.length+' selected'; }
  else { bar.classList.remove('show'); }
  return ids;
}
function toggleAll(c){ document.querySelectorAll('.rowchk').forEach(function(x){x.checked=c.checked;}); rowChecked(); }
function bulkDo(action){
  var ids=rowChecked(); if(!ids.length) return;
  if(action==='export'){ window.location='/dashboard/export?ids='+ids.join(','); return; }
  var fd=new FormData(); fd.append('action',action); ids.forEach(function(i){fd.append('ids',i);});
  fetch('/dashboard/bulk',{method:'POST',body:fd}).then(function(){location.reload();});
}
document.addEventListener('keydown',function(ev){ if(ev.key==='Escape') closeDrawer(); });
"""


def workspace_switcher(current_ws):
    try:
        workspaces = db.list_workspaces()
    except Exception:
        workspaces = []
    label = current_ws or "All workspaces"
    items = f'<a class="ws-item {"sel" if not current_ws else ""}" href="/dashboard/switch?workspace=">All workspaces</a>'
    for w in workspaces:
        sel = "sel" if w.name == current_ws else ""
        items += f'<a class="ws-item {sel}" href="/dashboard/switch?workspace={quote(w.name)}">{e(w.name)}</a>'
    return f"""<div class="ws-switch">
      <button class="ws-btn" type="button" onclick="toggleWs(event)">{e(label)} &#9662;</button>
      <div class="ws-menu" id="wsmenu" onclick="event.stopPropagation()">
        <input class="ws-search" placeholder="Search workspaces..." oninput="wsFilter(this.value)">
        <div class="ws-list">{items}</div>
      </div>
    </div>"""


def layout(title, active, body, current_ws="", with_drawer=False):
    user = os.getenv("DASHBOARD_USER", "admin")
    initials = (user[:2] or "AD").upper()

    side = ""
    for label, href, key in SIDEBAR_ITEMS:
        cls = "active" if key == active and label in ("Dashboard",) else ""
        side += f'<a class="{cls}" href="{href}">{e(label)}</a>'

    tabs = ""
    for label, href, key in [("Reply Manager", "/dashboard", "reply"),
                             ("Workspaces", "/dashboard/workspaces", "workspaces"),
                             ("Settings", "/dashboard/settings", "settings")]:
        cls = "active" if key == active else ""
        tabs += f'<a class="{cls}" href="{href}">{e(label)}</a>'

    drawer = ""
    if with_drawer:
        drawer = """
        <div class="overlay" id="overlay" onclick="closeDrawer()"></div>
        <div class="drawer" id="drawer">
          <div class="dhead">
            <button class="btn sec sm" onclick="drawerNav(-1)">&larr; Prev</button>
            <button class="btn sec sm" onclick="drawerNav(1)">Next &rarr;</button>
            <button class="btn sec sm right" onclick="closeDrawer()">Close</button>
          </div>
          <div class="dbody" id="drawer-content"></div>
        </div>"""

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{PAGE_CSS}</style></head><body>
<div class="app">
  <div class="sidebar">
    <div class="logo">⚡ Reply Manager</div>
    <div class="navi">{side}</div>
    <div class="spacer"></div>
    <a href="/dashboard/logout" style="color:#aab2cd;padding:9px 12px;font-size:13px;font-weight:600">Logout</a>
    <div class="me">
      <div class="ava">{e(initials)}</div>
      <div><div class="nm">{e(user)}</div><div class="ws">{e(current_ws or "All workspaces")}</div></div>
    </div>
  </div>
  <div class="main">
    <div class="topbar">
      <div class="tabs">{tabs}</div>
      {workspace_switcher(current_ws)}
    </div>
    <div class="content">{body}</div>
  </div>
</div>
{drawer}
<script>{DRAWER_JS}</script>
</body></html>"""


# -------------------------
# Console (main reply manager)
# -------------------------

@router.get("/dashboard/switch")
def switch_workspace(workspace: str = "", _: str = Depends(require_login)):
    target = "/dashboard" + (f"?workspace={quote(workspace)}" if workspace else "")
    resp = RedirectResponse(target, status_code=303)
    if workspace:
        resp.set_cookie("ws", workspace, max_age=60 * 60 * 24 * 365, samesite="lax")
    else:
        resp.delete_cookie("ws")
    return resp


@router.get("/dashboard/logout")
def logout():
    return Response("Logged out. Close this tab or log in again.", status_code=401,
                    headers={"WWW-Authenticate": "Basic"})


@router.get("/dashboard/soon", response_class=HTMLResponse)
def soon(request: Request, name: str = "This section", _: str = Depends(require_login)):
    body = f"""<h1>{e(name)}</h1>
    <p class="sub">This section is coming soon.</p>
    <div class="empty">🚧 <b>{e(name)}</b> isn't wired up yet.<br>It will live here as the platform grows.</div>"""
    return HTMLResponse(layout(name, "reply", body, current_ws=request.cookies.get("ws", "")))


@router.get("/dashboard", response_class=HTMLResponse)
def console(request: Request, workspace: str = "", status: str = "", intent: str = "",
            action: str = "", q: str = "", conf: str = "", date_from: str = "",
            date_to: str = "", page: int = 1, _: str = Depends(require_login)):
    if not workspace:
        workspace = request.cookies.get("ws", "")

    page = max(1, page)
    dto = (date_to + " 23:59:59") if date_to else ""
    rows, total = db.list_leads(
        workspace=workspace or None, status=status or None, intent=intent or None,
        action=action or None, search=q or None, conf_min=conf or None,
        date_from=date_from or None, date_to=dto or None,
        offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE,
    )
    counts = db.lead_counts(workspace=workspace or None)
    workspaces = db.list_workspaces()
    intents = db.distinct_intents()

    # ---- filter controls ----
    ws_opts = '<option value="">All workspaces</option>' + "".join(
        f'<option value="{e(w.name)}"{" selected" if w.name==workspace else ""}>{e(w.name)}</option>' for w in workspaces)
    status_defs = [("", "All statuses"), ("replied", "Replied"), ("needs_review", "Needs review"),
                   ("enriched", "Follow-up added"), ("not_enriched", "Not enriched"),
                   ("stopped", "Stopped"), ("reviewed", "Reviewed")]
    status_opts = "".join(f'<option value="{v}"{" selected" if v==status else ""}>{l}</option>' for v, l in status_defs)
    intent_opts = '<option value="">All intents</option>' + "".join(
        f'<option value="{e(i)}"{" selected" if i==intent else ""}>{e(i)}</option>' for i in intents)
    action_defs = [("", "All actions"), ("send", "Send"), ("skip_enrich", "Enrich"), ("stop", "Stop"), ("error", "Error")]
    action_opts = "".join(f'<option value="{v}"{" selected" if v==action else ""}>{l}</option>' for v, l in action_defs)
    conf_defs = [("", "Any confidence"), ("0.5", "≥ 0.50"), ("0.7", "≥ 0.70"), ("0.9", "≥ 0.90")]
    conf_opts = "".join(f'<option value="{v}"{" selected" if v==conf else ""}>{l}</option>' for v, l in conf_defs)

    # ---- rows ----
    body_rows = ""
    for ld in rows:
        when = ld.created_at.strftime("%b %d, %H:%M") if ld.created_at else ""
        body_rows += f"""<tr>
          <td><input type="checkbox" class="rowchk" value="{ld.id}" onchange="rowChecked()"></td>
          <td class="small muted">{e(when)}</td>
          <td>{e(ld.workspace_name)}</td>
          <td>{e(ld.name) or '<span class="muted">-</span>'}</td>
          <td class="small">{e(ld.email)}</td>
          <td class="small">{e(ld.company) or '<span class="muted">-</span>'}</td>
          <td class="small">{e(ld.campaign) or '<span class="muted">-</span>'}</td>
          <td>{e(ld.intent) or '<span class="muted">-</span>'}</td>
          <td>{e(ld.confidence)}</td>
          <td>{action_badge(ld.action)}</td>
          <td>{reply_badge(ld)}</td>
          <td>{fup_badge(ld)}</td>
          <td>{review_badge(ld)}</td>
          <td><button class="linkbtn" onclick="openDrawer({ld.id})">View</button></td>
        </tr>"""

    if not rows:
        body_rows = ('<tr><td colspan="14"><div class="empty">No replies found.<br>'
                     'Try changing filters or wait for new replies to sync.</div></td></tr>')

    # ---- pagination ----
    pages = max(1, math.ceil(total / PAGE_SIZE))
    base_params = {k: v for k, v in {
        "workspace": workspace, "status": status, "intent": intent, "action": action,
        "q": q, "conf": conf, "date_from": date_from, "date_to": date_to}.items() if v}

    def page_url(p):
        params = dict(base_params); params["page"] = p
        return "/dashboard?" + urlencode(params)

    pager = ""
    if pages > 1:
        pager = '<div class="pager">'
        pager += f'<a href="{page_url(page-1)}">Prev</a>' if page > 1 else '<span class="muted">Prev</span>'
        for p in range(max(1, page - 2), min(pages, page + 2) + 1):
            pager += f'<span class="cur">{p}</span>' if p == page else f'<a href="{page_url(p)}">{p}</a>'
        pager += f'<a href="{page_url(page+1)}">Next</a>' if page < pages else '<span class="muted">Next</span>'
        pager += '</div>'

    lead_ids = [ld.id for ld in rows]

    def card(n, label, st=""):
        href = page_url_filter(base_params, status=st)
        return f'<div class="stat click" onclick="location.href=\'{href}\'"><div class="n">{n}</div><div class="l">{label}</div></div>'

    body = f"""
    <h1>Reply Manager</h1>
    <p class="sub">Process replies, review AI suggestions, and push follow-ups — all in one place.</p>

    <div class="cards">
      {card(counts['total'], 'Total replies')}
      {card(counts['replied'], 'Replied', 'replied')}
      {card(counts['enriched'], 'Follow-ups added', 'enriched')}
      {card(counts['needs_review'], 'Needs review', 'needs_review')}
      {card(counts['stopped'], 'Stopped', 'stopped')}
      {card(counts['sent'], 'Sent', 'replied')}
    </div>

    <form class="filterbar" id="filterform" method="get" action="/dashboard">
      <input class="grow" type="text" name="q" value="{e(q)}" placeholder="Search name, email or company..." oninput="autoApplySearch()">
      <select name="workspace" onchange="autoApply()">{ws_opts}</select>
      <select name="status" onchange="autoApply()">{status_opts}</select>
      <select name="intent" onchange="autoApply()">{intent_opts}</select>
      <select name="action" onchange="autoApply()">{action_opts}</select>
      <select name="conf" onchange="autoApply()">{conf_opts}</select>
      <input type="date" name="date_from" value="{e(date_from)}" onchange="autoApply()">
      <input type="date" name="date_to" value="{e(date_to)}" onchange="autoApply()">
      <a class="btn sec" href="/dashboard">Reset</a>
    </form>

    <div class="bulkbar" id="bulkbar">
      <b id="bulkcount">0 selected</b>
      <button class="btn ok sm" onclick="bulkDo('mark_reviewed')">Mark reviewed</button>
      <button class="btn danger sm" onclick="bulkDo('stop')">Stop selected</button>
      <button class="btn sec sm" onclick="bulkDo('export')">Export CSV</button>
    </div>

    <div class="tablewrap">
      <table>
        <thead><tr>
          <th><input type="checkbox" onchange="toggleAll(this)"></th>
          <th>When</th><th>Workspace</th><th>Lead</th><th>Email</th><th>Company</th><th>Campaign</th>
          <th>Intent</th><th>Conf</th><th>Action</th><th>Reply</th><th>FUP</th><th>Review</th><th></th>
        </tr></thead>
        <tbody>{body_rows}</tbody>
      </table>
    </div>
    {pager}
    <div class="flex" style="margin-top:12px">
      <span class="muted small">{total} result(s)</span>
      <a class="btn sec sm right" href="/dashboard/export?{urlencode(base_params)}">Export all (CSV)</a>
    </div>
    <script>window.__LEAD_IDS__ = {json.dumps(lead_ids)};</script>
    """
    return HTMLResponse(layout("Reply Manager", "reply", body, current_ws=workspace, with_drawer=True))


def page_url_filter(base_params, status=""):
    params = dict(base_params)
    if status:
        params["status"] = status
    else:
        params.pop("status", None)
    params.pop("page", None)
    return "/dashboard?" + urlencode(params)


# -------------------------
# Lead detail panel (drawer fragment) + full page fallback
# -------------------------

def build_panel(ld):
    try:
        followups = json.loads(ld.followups) if ld.followups else []
    except Exception:
        followups = []

    convo = ""
    if ld.reply_text:
        convo += f'<div class="msg in"><div class="who">Prospect — {e(ld.name)} &lt;{e(ld.email)}&gt;</div><div class="body">{nl2br(ld.reply_text)}</div></div>'
    if ld.main_reply:
        lbl = "Sent reply" if ld.replied else "Drafted reply (not sent)"
        convo += f'<div class="msg"><div class="who">{e(lbl)}</div><div class="body">{nl2br(ld.main_reply)}</div></div>'
    if not convo:
        convo = '<p class="muted">No conversation text captured.</p>'

    fups = ""
    real = [(i + 1, f) for i, f in enumerate(followups) if (f or "").strip()]
    for step, t in real:
        fups += f'<div class="fup"><div class="step">Follow-up {step}</div><div class="body">{nl2br(t)}</div></div>'
    if not fups:
        fups = '<p class="muted">No follow-up drafts.</p>'

    editable = followups[0] if followups else (ld.main_reply or "")

    return f"""
    <div class="kv">
      <span class="tag">{e(ld.email)}</span>
      <span class="tag">{e(ld.workspace_name)}</span>
      <span class="tag">{e(ld.platform)}</span>
      {f'<span class="tag">{e(ld.campaign)}</span>' if ld.campaign else ''}
    </div>
    <h2 style="margin:2px 0 10px">{e(ld.name) or e(ld.email) or 'Lead'}</h2>
    <div class="flex" style="margin-bottom:14px">
      <span>Intent: <b>{e(ld.intent) or '-'}</b></span>
      <span class="muted">conf {e(ld.confidence)}</span>
      {action_badge(ld.action)} {reply_badge(ld)} {review_badge(ld)}
    </div>

    <div class="card"><h3>Conversation</h3>{convo}</div>
    <div class="card"><h3>Follow-up sequence (AI-drafted)</h3>{fups}</div>
    <div class="card">
      <h3>Edit recommended follow-up</h3>
      <textarea id="fup-edit">{e(editable)}</textarea>
      <div class="flex" style="margin-top:10px">
        <button class="btn sm" onclick="leadAction({ld.id},'save_followup')">Save draft</button>
        <button class="btn ok sm" onclick="leadAction({ld.id},'mark_reviewed')">Mark reviewed</button>
        <button class="btn danger sm" onclick="leadAction({ld.id},'stop')">Stop lead</button>
        <button class="btn sec sm" onclick="leadAction({ld.id},'mark_done')">Mark done</button>
      </div>
      <p class="muted small" style="margin-top:8px">Saving updates the stored draft. (Live re-send from the dashboard is a planned next step.)</p>
    </div>
    """


@router.get("/dashboard/leads/{lead_id}/panel", response_class=HTMLResponse)
def lead_panel(lead_id: int, _: str = Depends(require_login)):
    ld = db.get_lead(lead_id)
    if ld is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return HTMLResponse(build_panel(ld))


@router.get("/dashboard/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request, _: str = Depends(require_login)):
    ld = db.get_lead(lead_id)
    if ld is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    body = f'<a href="/dashboard" class="muted small">&larr; Back</a>{build_panel(ld)}'
    return HTMLResponse(layout(ld.name or "Lead", "reply", body, current_ws=request.cookies.get("ws", "")))


@router.post("/dashboard/leads/{lead_id}/action")
async def lead_action(lead_id: int, request: Request, _: str = Depends(require_login)):
    form = await request.form()
    action = form.get("action", "")
    followup = form.get("followup", "")

    ld = db.get_lead(lead_id)
    if ld is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    if action == "save_followup":
        try:
            fups = json.loads(ld.followups) if ld.followups else []
        except Exception:
            fups = []
        if fups:
            fups[0] = followup
        else:
            fups = [followup]
        db.update_lead_fields(lead_id, followups=fups)
    elif action == "mark_reviewed":
        db.update_lead_fields(lead_id, reviewed=True)
    elif action == "stop":
        db.update_lead_fields(lead_id, action="stop", reviewed=True)
    elif action == "mark_done":
        db.update_lead_fields(lead_id, reviewed=True)

    return JSONResponse({"ok": True})


@router.post("/dashboard/bulk")
async def bulk(request: Request, _: str = Depends(require_login)):
    form = await request.form()
    action = form.get("action", "")
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    if ids:
        if action == "mark_reviewed":
            db.bulk_update_leads(ids, reviewed=True)
        elif action == "stop":
            db.bulk_update_leads(ids, action="stop", reviewed=True)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard/export")
def export_csv(request: Request, workspace: str = "", status: str = "", intent: str = "",
               action: str = "", q: str = "", conf: str = "", date_from: str = "",
               date_to: str = "", ids: str = "", _: str = Depends(require_login)):
    id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()] if ids else None
    rows = db.export_leads(workspace=workspace or None, status=status or None, intent=intent or None,
                           action=action or None, search=q or None, conf_min=conf or None,
                           date_from=date_from or None, date_to=date_to or None, ids=id_list)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Created", "Workspace", "Platform", "Name", "Email", "Company", "Campaign",
                "Intent", "Confidence", "Action", "Replied", "FUP added", "Reviewed", "Reply text"])
    for ld in rows:
        w.writerow([ld.created_at, ld.workspace_name, ld.platform, ld.name, ld.email, ld.company,
                    ld.campaign, ld.intent, ld.confidence, ld.action, ld.replied, ld.fup_added,
                    ld.reviewed, (ld.reply_text or "").replace("\n", " ")])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=replies.csv"})


# -------------------------
# Workspaces
# -------------------------

@router.get("/dashboard/workspaces", response_class=HTMLResponse)
def workspaces_page(request: Request, _: str = Depends(require_login)):
    workspaces = db.list_workspaces()
    rows = ""
    if not workspaces:
        rows = '<tr><td colspan="5" class="muted">No workspaces yet.</td></tr>'
    for w in workspaces:
        key_state = pill("Set", "ok") if w.api_key else pill("Missing", "no")
        rows += f"""<tr>
          <td><a href="/dashboard/workspaces/{w.id}">{e(w.name)}</a></td>
          <td>{e(w.platform)}</td>
          <td>{key_state}</td>
          <td>{e(w.reply_followup_campaign_id)}</td>
          <td>{pill("Active","ok") if w.active else pill("Off","muted")}</td>
        </tr>"""
    body = f"""
    <div class="flex"><h1>Workspaces</h1><a class="btn right" href="/dashboard/workspaces/new">+ Add workspace</a></div>
    <div class="tablewrap"><table>
      <thead><tr><th>Name</th><th>Platform</th><th>API key</th><th>Follow-up campaign</th><th>Status</th></tr></thead>
      <tbody>{rows}</tbody></table></div>
    """
    return HTMLResponse(layout("Workspaces", "workspaces", body, current_ws=request.cookies.get("ws", "")))


def _workspace_form(ws=None, error="", current_ws=""):
    is_new = ws is None
    g = (lambda a, d="": d if is_new else (getattr(ws, a) or d))
    name = "" if is_new else ws.name
    platform = "bison" if is_new else (ws.platform or "bison")
    active_checked = "checked" if (is_new or ws.active) else ""
    profile = "{}" if is_new else json.dumps(ws.client_profile or {}, indent=2)
    rformat = "{}" if is_new else json.dumps(ws.reply_format or {}, indent=2)
    act = "/dashboard/workspaces" if is_new else f"/dashboard/workspaces/{ws.id}"
    title = "Add workspace" if is_new else f"Edit: {name}"
    bsel = " selected" if platform == "bison" else ""
    isel = " selected" if platform == "instantly" else ""
    err = f'<div class="card" style="border-color:var(--no);color:var(--no)">{e(error)}</div>' if error else ""
    delbtn = "" if is_new else f"""<form method="post" action="/dashboard/workspaces/{ws.id}/delete" onsubmit="return confirm('Delete this workspace?')" style="display:inline"><button class="btn danger" type="submit">Delete</button></form>"""
    body = f"""
    <a href="/dashboard/workspaces" class="muted small">&larr; Back to workspaces</a>
    <h1>{e(title)}</h1>{err}
    <form method="post" action="{act}">
      <div class="card">
        <div class="row2">
          <div><label>Workspace name</label><input type="text" name="name" value="{e(name)}" required style="width:100%"></div>
          <div><label>Platform</label><select name="platform" style="width:100%"><option value="bison"{bsel}>bison</option><option value="instantly"{isel}>instantly</option></select></div>
        </div>
        <label>API key</label><input type="text" name="api_key" value="{e(g('api_key'))}" style="width:100%">
        <div class="row2">
          <div><label>Base URL (Bison only)</label><input type="text" name="base_url" value="{e(g('base_url'))}" placeholder="https://your-bison-instance.com" style="width:100%"></div>
          <div><label>Follow-up campaign ID</label><input type="text" name="reply_followup_campaign_id" value="{e(g('reply_followup_campaign_id'))}" style="width:100%"></div>
        </div>
        <div class="row2">
          <div><label>Website (signature)</label><input type="text" name="website" value="{e(g('website'))}" style="width:100%"></div>
          <div><label>Default sender name</label><input type="text" name="sender_name" value="{e(g('sender_name'))}" style="width:100%"></div>
        </div>
        <label>Default sender email</label><input type="text" name="default_sender_email" value="{e(g('default_sender_email'))}" style="width:100%">
        <label class="flex"><input type="checkbox" name="active" value="1" {active_checked} style="margin-right:8px"> Active</label>
      </div>
      <div class="card"><h3>Client profile (JSON)</h3><textarea name="client_profile">{e(profile)}</textarea></div>
      <div class="card"><h3>Reply format (JSON)</h3><textarea name="reply_format">{e(rformat)}</textarea></div>
      <div class="flex"><button class="btn" type="submit">Save</button>{delbtn}</div>
    </form>
    """
    return layout(title, "workspaces", body, current_ws=current_ws)


@router.get("/dashboard/workspaces/new", response_class=HTMLResponse)
def workspace_new(request: Request, _: str = Depends(require_login)):
    return HTMLResponse(_workspace_form(None, current_ws=request.cookies.get("ws", "")))


@router.get("/dashboard/workspaces/{ws_id}", response_class=HTMLResponse)
def workspace_edit(ws_id: int, request: Request, _: str = Depends(require_login)):
    ws = db.get_workspace(ws_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return HTMLResponse(_workspace_form(ws, current_ws=request.cookies.get("ws", "")))


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
    return {
        "name": form.get("name", ""), "platform": form.get("platform", "bison"),
        "api_key": form.get("api_key", ""), "base_url": form.get("base_url", ""),
        "reply_followup_campaign_id": form.get("reply_followup_campaign_id", ""),
        "website": form.get("website", ""), "sender_name": form.get("sender_name", ""),
        "default_sender_email": form.get("default_sender_email", ""),
        "active": form.get("active") == "1",
        "client_profile": _parse_json_field(form.get("client_profile"), "Client profile"),
        "reply_format": _parse_json_field(form.get("reply_format"), "Reply format"),
    }


@router.post("/dashboard/workspaces")
async def workspace_create(request: Request, _: str = Depends(require_login)):
    try:
        data = await _read_workspace_form(request)
    except ValueError as ex:
        return HTMLResponse(_workspace_form(None, error=str(ex), current_ws=request.cookies.get("ws", "")))
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
        return HTMLResponse(_workspace_form(ws, error=str(ex), current_ws=request.cookies.get("ws", "")))
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
def settings_page(request: Request, saved: str = "", _: str = Depends(require_login)):
    s = db.all_settings()
    saved_html = '<div class="card" style="border-color:var(--ok);color:var(--ok)">Saved.</div>' if saved else ""
    body = f"""
    <h1>Settings</h1>{saved_html}
    <form method="post" action="/dashboard/settings">
      <div class="card">
        <label>OpenAI API key</label><input type="text" name="openai_api_key" value="{e(s.get('openai_api_key',''))}" style="width:100%">
        <label>Human review webhook URL</label><input type="text" name="human_review_webhook_url" value="{e(s.get('human_review_webhook_url',''))}" style="width:100%">
        <div class="row2">
          <div><label>Default EmailBison base URL</label><input type="text" name="emailbison_base_url" value="{e(s.get('emailbison_base_url',''))}" style="width:100%"></div>
          <div><label>Reply delay (seconds)</label><input type="text" name="reply_delay_seconds" value="{e(s.get('reply_delay_seconds','420'))}" style="width:100%"></div>
        </div>
      </div>
      <button class="btn" type="submit">Save settings</button>
    </form>
    <p class="muted small" style="margin-top:18px">The dashboard password is set with the <b>DASHBOARD_PASSWORD</b> environment variable on Railway.</p>
    """
    return HTMLResponse(layout("Settings", "settings", body, current_ws=request.cookies.get("ws", "")))


@router.post("/dashboard/settings")
async def settings_save(request: Request, _: str = Depends(require_login)):
    form = await request.form()
    for key in ["openai_api_key", "human_review_webhook_url", "emailbison_base_url", "reply_delay_seconds"]:
        db.set_setting(key, form.get(key, ""))
    return RedirectResponse("/dashboard/settings?saved=1", status_code=303)
