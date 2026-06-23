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
    return pill("Added", "violet") if ld.fup_added else pill("No", "muted")


# Inline lucide icons (MIT) for the icon-only sidebar.
_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round">{}</svg>')
ICONS = {
    "dashboard": '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    "booked": '<path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/><path d="m9 16 2 2 4-4"/>',
    "inbox": '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "support": '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="4"/><line x1="4.93" y1="4.93" x2="9.17" y2="9.17"/><line x1="14.83" y1="14.83" x2="19.07" y2="19.07"/><line x1="14.83" y1="9.17" x2="19.07" y2="4.93"/><line x1="9.17" y1="14.83" x2="4.93" y2="19.07"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
}


def icon(name):
    return _SVG.format(ICONS.get(name, ""))


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
/* lead-data rows */
.ld-row { display:grid; grid-template-columns:150px 1fr; gap:12px; padding:9px 0; border-bottom:1px solid var(--line); }
.ld-row:last-child { border-bottom:none; }
.ld-k { color:var(--muted); font-size:12px; font-weight:600; }
.ld-v { font-size:13px; line-height:1.5; white-space:pre-wrap; word-break:break-word; }
.ld-v.clamp { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; cursor:pointer; }
.ld-v.clamp.open { -webkit-line-clamp:unset; }
.ld-v a { word-break:break-all; }
/* collapsible block */
.clip { position:relative; max-height:140px; overflow:hidden; transition:max-height .2s ease; }
.clip.open { max-height:4000px; }
.clip-toggle { display:inline-block; margin-top:8px; color:var(--accent); font-weight:700; cursor:pointer; font-size:12px; }
.clip-fade { position:absolute; bottom:0; left:0; right:0; height:38px; background:linear-gradient(transparent,#fff); pointer-events:none; }
.clip.open .clip-fade { display:none; }
.drawer .dfoot { margin-top:auto; padding:13px 18px; border-top:1px solid var(--line); display:flex; gap:8px; flex-wrap:wrap; }
.row2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
label { display:block; color:var(--muted); font-size:12px; margin:13px 0 5px; font-weight:600; }
.flex { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.right { margin-left:auto; }
@media (max-width:900px){ .cards{grid-template-columns:repeat(2,1fr);} .sidebar{display:none;} }

/* ============ PREMIUM OVERRIDES ============ */
:root {
  --bg:#f8fafc; --panel:#ffffff; --line:#e5e7eb; --txt:#0f172a; --muted:#64748b;
  --muted-soft:#f1f5f9; --accent:#2563eb; --accent-soft:#eff6ff;
  --ok:#166534; --ok-soft:#ecfdf5; --blue:#1d4ed8; --blue-soft:#eff6ff;
  --violet:#6d28d9; --violet-soft:#f5f3ff; --warn:#b45309; --warn-soft:#fff7ed;
  --no:#b91c1c; --no-soft:#fef2f2; --sidebar:#0f172a;
}
body { font-family:'Inter',-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
       font-size:13px; -webkit-font-smoothing:antialiased; }

/* icon sidebar */
.sidebar { width:64px; flex:0 0 64px; background:var(--sidebar); color:#cbd5e1;
           padding:14px 0; align-items:center; }
.sidebar .logo { font-size:20px; color:#fff; padding:0 0 16px; margin:0; }
.sidebar .navi { width:100%; align-items:center; gap:6px; }
.sidebar .navi a { width:42px; height:42px; padding:0; justify-content:center; border-radius:11px;
                   color:#cbd5e1; position:relative; }
.sidebar .navi a:hover { background:rgba(255,255,255,.06); color:#fff; }
.sidebar .navi a.active { background:rgba(255,255,255,.10); color:#fff; }
.sidebar .navi a svg { width:20px; height:20px; }
.tip { position:absolute; left:52px; top:50%; transform:translateY(-50%); background:#0f172a;
       color:#fff; font-size:12px; line-height:1.3; padding:6px 9px; border-radius:8px; white-space:nowrap;
       opacity:0; pointer-events:none; transition:opacity .12s; box-shadow:0 6px 18px rgba(0,0,0,.28); z-index:200; }
.sidebar .navi a:hover .tip, .mewrap:hover .tip { opacity:1; }
.tip b { display:block; } .tip span { color:#94a3b8; font-size:11px; }
.mewrap { margin-top:auto; position:relative; display:flex; justify-content:center; padding-top:10px; }
.ava-dot { width:32px; height:32px; border-radius:50%; background:var(--accent); color:#fff;
           display:flex; align-items:center; justify-content:center; font-weight:700; font-size:12px; cursor:default; }
.sidebar .logoutlink { color:#94a3b8; width:42px; height:42px; display:flex; align-items:center; justify-content:center; border-radius:11px; }
.sidebar .logoutlink:hover { background:rgba(255,255,255,.06); color:#fff; }

/* top bar */
.topbar { height:56px; padding:0 22px; gap:22px; }
.topbar .tabs { gap:20px; height:100%; align-items:stretch; }
.topbar .tabs a { display:flex; align-items:center; font-size:14px; font-weight:600; color:var(--muted);
                  border-bottom:2px solid transparent; }
.topbar .tabs a.active { color:var(--txt); border-bottom:2px solid var(--accent); }
.ws-btn { height:36px; font-size:13px; }

/* content */
.content { padding:24px 26px 64px; }
h1 { font-size:19px; }
.cards { gap:12px; }
.stat { height:84px; display:flex; flex-direction:column; justify-content:center; border-radius:14px;
        box-shadow:0 1px 2px rgba(15,23,42,.04); }
.stat .n { font-size:32px; font-weight:700; line-height:1; }
.stat .l { font-size:11px; font-weight:500; text-transform:uppercase; letter-spacing:.04em; margin-top:6px; }

/* filters */
.filterbar { padding:10px 12px; gap:8px; border-radius:12px; }
.filterbar input, .filterbar select { height:36px; font-size:13px; border:1px solid var(--line); }

/* table density */
thead th { font-size:11px; letter-spacing:.04em; text-transform:uppercase; padding:10px 14px; background:#fff; }
tbody td { height:56px; font-size:13px; }
.tablewrap { border-radius:14px; box-shadow:0 1px 2px rgba(15,23,42,.04); }

/* softer pills */
.pill { padding:4px 10px; font-weight:600; }
.pill.violet { background:var(--violet-soft); color:var(--violet); }

/* buttons */
.btn { border-radius:9px; }
.btn.ok { background:var(--ok-soft); color:var(--ok); }

/* ============ STITCH INDIGO THEME ============ */
:root {
  --primary:#4f46e5; --bg:#faf8ff; --panel:#ffffff; --surf-low:#f2f3ff; --surf:#eaedff;
  --surf-high:#e2e7ff; --line:#e2e8f0; --txt:#1e293b; --muted:#505f76;
  --accent:#4f46e5; --accent-soft:#eef2ff; --emerald:#10b981;
  --ok:#166534; --ok-soft:#e7f7f0; --no:#ba1a1a; --no-soft:#ffdad6;
  --blue:#4f46e5; --blue-soft:#e0e7ff; --violet:#6d28d9; --violet-soft:#f5f3ff;
  --warn:#a8730a; --warn-soft:#fdf2dd; --muted-soft:#eef0f5;
}
body { background:var(--bg); font-family:'Inter',sans-serif; color:var(--txt); }
.material-symbols-outlined { font-variation-settings:'FILL' 0,'wght' 400,'GRAD' 0,'opsz' 24;
                             vertical-align:middle; font-size:20px; }

/* top nav (no sidebar) */
.app { display:block; }
.topnav { position:sticky; top:0; z-index:40; display:flex; align-items:center; gap:22px;
          height:64px; padding:0 26px; background:#fff; border-bottom:1px solid var(--line); }
.topnav .brand { display:flex; align-items:center; gap:10px; }
.topnav .brand .mark { color:var(--primary); font-size:20px; }
.topnav .brand h1 { font-size:17px; font-weight:600; margin:0; color:var(--txt); }
.topnav .brand p { font-size:11px; color:var(--muted); margin:0; }
.topnav .tabs { display:flex; gap:4px; margin-left:8px; }
.topnav .tabs a { font-size:13px; font-weight:600; color:var(--muted); padding:8px 12px; border-radius:8px; border:none; }
.topnav .tabs a:hover { background:var(--surf-low); color:var(--txt); }
.topnav .tabs a.active { color:var(--primary); background:var(--accent-soft); }
.topnav .rt { margin-left:auto; display:flex; align-items:center; gap:16px; }
.bell { position:relative; color:var(--muted); cursor:pointer; }
.bell .bdot { position:absolute; top:-1px; right:-1px; width:8px; height:8px; background:var(--no); border-radius:50%; border:2px solid #fff; }
.avatar { width:32px; height:32px; border-radius:50%; background:var(--primary); color:#fff;
          display:flex; align-items:center; justify-content:center; font-weight:700; font-size:12px; }
.wrap2 { max-width:1280px; margin:0 auto; padding:26px 28px 80px; }
.secthead { display:flex; align-items:center; justify-content:space-between; margin:0 0 14px; }
.secthead h2 { font-size:17px; font-weight:600; margin:0; }
.secthead .sub2 { font-size:12px; color:var(--muted); }

/* stat cards */
.cards { display:grid; grid-template-columns:repeat(6,1fr); gap:14px; margin-bottom:8px; }
.stat { background:#fff; border:2px solid var(--line); border-radius:14px; padding:16px; height:auto;
        cursor:pointer; transition:border-color .15s; box-shadow:none; display:block; }
.stat:hover { border-color:var(--primary); }
.stat .l { font-size:12px; font-weight:500; color:var(--muted); margin:0 0 4px; text-transform:none; letter-spacing:0; }
.stat .n { font-size:26px; font-weight:700; color:var(--txt); line-height:1; }
.stat .bar { margin-top:14px; height:4px; background:var(--surf-low); border-radius:99px; overflow:hidden; }
.stat .bar i { display:block; height:100%; background:var(--primary); border-radius:99px; }
.stat.is-booked { border-color:var(--primary); }
.stat.is-booked .l { color:var(--primary); font-weight:700; }
.stat.is-review { border-color:var(--no); }
.stat.is-review .l { color:var(--no); font-weight:700; }
.stat.is-review .bar i { background:var(--no); }
.stat.is-muted { opacity:.65; }

/* filters */
.filterbar { background:#fff; border:1px solid var(--line); border-radius:12px; }

/* table */
.tablewrap { background:#fff; border:1px solid var(--line); border-radius:14px; box-shadow:none; }
thead th { background:var(--surf-low); color:var(--muted); font-size:12px; font-weight:600;
           text-transform:none; letter-spacing:0; padding:12px 18px; }
tbody td { padding:13px 18px; height:auto; }
.lead-cell { display:flex; align-items:center; gap:12px; }
.lead-ava { width:38px; height:38px; border-radius:50%; background:var(--blue-soft); color:var(--primary);
            display:flex; align-items:center; justify-content:center; font-weight:700; font-size:13px; flex:0 0 38px; }
.lead-nm { font-weight:600; font-size:14px; color:var(--txt); }
.lead-sub { font-size:12px; color:var(--muted); }
.intent-cell { display:flex; align-items:center; gap:7px; font-size:13px; }
.idot { width:8px; height:8px; border-radius:50%; background:#c7c4d8; flex:0 0 8px; }
.idot.pos { background:var(--emerald); }
.eye { width:36px; height:36px; border:1px solid var(--line); background:#fff; border-radius:50%;
       display:inline-flex; align-items:center; justify-content:center; color:var(--primary); cursor:pointer; }
.eye:hover { box-shadow:0 2px 10px rgba(79,70,229,.2); }

/* pills */
.pill { border-radius:99px; font-size:11px; font-weight:700; padding:4px 11px; }
.pill.ok { background:var(--ok-soft); color:#166534; }
.pill.blue { background:var(--surf-high); color:var(--primary); }
.pill.violet { background:var(--violet-soft); color:var(--violet); }
.pill.no { background:var(--no-soft); color:var(--no); text-transform:uppercase; letter-spacing:.04em; }
.pill.muted { background:var(--surf-high); color:var(--muted); }
.pill.warn { background:var(--warn-soft); color:var(--warn); }

/* AI banner */
.banner { margin-top:22px; background:var(--primary); color:#fff; border-radius:14px; padding:22px 24px;
          position:relative; overflow:hidden; box-shadow:0 10px 28px rgba(79,70,229,.25); }
.banner h4 { margin:0 0 4px; font-size:17px; font-weight:700; }
.banner p { margin:0 0 14px; color:rgba(255,255,255,.85); max-width:560px; font-size:13px; }
.banner .bbtn { background:#fff; color:var(--primary); border:none; border-radius:8px; padding:9px 18px;
                font-weight:700; font-size:12px; cursor:pointer; }
.banner .bgico { position:absolute; right:14px; top:-14px; font-size:140px; opacity:.12; }

/* misc */
.btn { background:var(--primary); }
.btn.sec { background:#fff; color:var(--txt); border:1px solid var(--line); }
.btn.ok { background:var(--ok-soft); color:#166534; }
.ws-btn { background:#fff; border:1px solid var(--line); }
.pager .cur { background:var(--primary); border-color:var(--primary); }
.linkbtn { color:var(--primary); }
@media (max-width:1000px){ .cards{grid-template-columns:repeat(2,1fr);} }

/* ============ SABROSSO LIGHT SIDEBAR ============ */
.app { display:flex; min-height:100vh; }
.lsidebar { width:252px; flex:0 0 252px; background:#fff; border-right:1px solid var(--line);
            height:100vh; position:sticky; top:0; display:flex; flex-direction:column; padding:18px 14px; }
.lbrand { display:flex; align-items:center; gap:9px; font-weight:700; font-size:16px; color:var(--txt); padding:4px 8px 14px; }
.lbrand .mark { color:var(--primary); }
.lsec { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.06em; color:#94a3b8; padding:14px 10px 6px; }
.lnav { display:flex; flex-direction:column; gap:2px; }
.lnav a { display:flex; align-items:center; gap:11px; padding:9px 10px; border-radius:9px; color:#475569;
          font-size:13.5px; font-weight:500; }
.lnav a .material-symbols-outlined { font-size:20px; color:#64748b; }
.lnav a:hover { background:#f6f7fb; color:var(--txt); }
.lnav a.active { background:var(--accent-soft); color:var(--primary); font-weight:600; }
.lnav a.active .material-symbols-outlined { color:var(--primary); }
.lnav .nlbl { flex:1; }
.nbadge { background:#eef0f5; color:#64748b; font-size:11px; font-weight:700; border-radius:99px; padding:1px 8px; min-width:20px; text-align:center; }
.nbadge.danger { background:var(--no-soft); color:var(--no); }
.lspacer { flex:1; }
.lnav-logout { display:flex; align-items:center; gap:11px; padding:9px 10px; border-radius:9px; color:#64748b; font-size:13.5px; font-weight:500; }
.lnav-logout:hover { background:#f6f7fb; color:var(--txt); }
.lnav-logout .material-symbols-outlined { font-size:20px; }
.luser { display:flex; align-items:center; gap:10px; padding:12px 8px 4px; margin-top:6px; border-top:1px solid var(--line); }
.luser .lu-nm { font-weight:600; font-size:13px; }
.luser .lu-ws { font-size:11px; color:var(--muted); }
.lmain { flex:1; min-width:0; display:flex; flex-direction:column; }
.ltop { height:64px; border-bottom:1px solid var(--line); background:#fff; display:flex; align-items:center;
        gap:16px; padding:0 24px; position:sticky; top:0; z-index:30; }
.lsearch { flex:1; max-width:440px; display:flex; align-items:center; gap:8px; background:#f6f7fb;
           border:1px solid var(--line); border-radius:10px; padding:0 12px; height:38px; }
.lsearch .material-symbols-outlined { color:#94a3b8; font-size:20px; }
.lsearch input { border:none; background:transparent; height:36px; width:100%; font-size:13px; padding:0; }
.lsearch input:focus { outline:none; }
.ltop-rt { margin-left:auto; display:flex; align-items:center; gap:14px; }
.lcontent { padding:24px 28px 80px; max-width:1280px; }
@media (max-width:1000px){ .lsidebar{display:none;} }

/* ============ ASCENDLY · STITCH (Manrope / Purple) THEME ============ */
:root {
  --primary:#5f3add; --primary-c:#7857f8; --primary-deep:#4918c8;
  --bg:#f8f9fa; --panel:#ffffff; --line:#d8d4e6;
  --surf-low:#f3f4f5; --surf:#edeeef; --surf-high:#e7e8e9;
  --txt:#191c1d; --muted:#56536a;
  --accent:#5f3add; --accent-soft:#efe9ff; --emerald:#137333;
  --ok:#137333; --ok-soft:#e6f4ea; --no:#ba1a1a; --no-soft:#ffdad6;
  --blue:#5f3add; --blue-soft:#ece7ff; --violet:#5f3add; --violet-soft:#efe9ff;
  --warn:#8a5a00; --warn-soft:#fdf2dd; --muted-soft:#edeeef;
}
body { font-family:'Manrope',-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
a { color:var(--primary); }

/* brand chip */
.lbrand { gap:10px; font-weight:800; letter-spacing:-.01em; }
.lbrand .mark { display:none; }
.lbrand .chip { width:30px; height:30px; border-radius:9px; background:var(--primary); color:#fff;
                display:flex; align-items:center; justify-content:center; font-weight:800; font-size:15px; }

/* grouped sidebar */
.lsidebar { background:var(--surf-low); }
.lsec { color:#7d7990; font-weight:700; letter-spacing:.08em; }
.lnav a { color:#484555; font-weight:600; border-radius:10px; }
.lnav a .material-symbols-outlined { color:#6f6b82; }
.lnav a:hover { background:#e6e1f4; color:var(--txt); }
.lnav a.active { background:var(--primary-c); color:#fff; font-weight:700;
                 box-shadow:inset 3px 0 0 var(--primary-deep); }
.lnav a.active .material-symbols-outlined { color:#fff; }
.nbadge { background:#e3e1ef; color:#56536a; }
.lnav a.active .nbadge { background:rgba(255,255,255,.28); color:#fff; }
.lnav-logout:hover { background:#e6e1f4; }
.luser .avatar, .avatar { background:var(--primary); }

/* top bar */
.ltop { background:#fff; }
.lsearch { background:#f1f0f6; border-color:var(--line); border-radius:11px; }
.ws-btn:hover { border-color:var(--primary); }

/* stat cards */
.stat { border:1px solid var(--line); border-radius:16px; }
.stat:hover { border-color:var(--primary); }
.stat .bar i { background:var(--primary); }
.stat.is-booked { border-color:var(--primary); } .stat.is-booked .l { color:var(--primary); }

/* table */
.tablewrap { border:1px solid var(--line); border-radius:16px; box-shadow:0 1px 2px rgba(25,28,29,.04); }
thead th { background:#faf9fe; color:#56536a; }
.lead-ava { background:#efe9ff; color:var(--primary); }
.idot.pos { background:var(--emerald); }
.eye { color:var(--primary); border-color:var(--line); }
.eye:hover { box-shadow:0 2px 10px rgba(95,58,221,.22); }

/* pills */
.pill.ok { background:#e6f4ea; color:#137333; }
.pill.blue { background:#ece7ff; color:var(--primary); }
.pill.violet { background:#efe9ff; color:var(--primary); }
.pill.muted { background:#edeeef; color:#56536a; }

/* buttons + accents */
.btn { background:var(--primary); border-radius:10px; }
.btn:hover { background:#5331c9; filter:none; }
.btn.sec { background:#fff; color:var(--txt); border:1px solid var(--line); }
.btn.ok { background:#e6f4ea; color:#137333; }
.linkbtn { color:var(--primary); }
.pager .cur { background:var(--primary); border-color:var(--primary); }
.banner { background:linear-gradient(135deg,#5f3add,#7857f8); box-shadow:0 10px 28px rgba(95,58,221,.28); }
.banner .bbtn { color:var(--primary); }
.fup .step { color:var(--primary); }
"""

# (icon_key, tooltip, href, active_key)
SIDEBAR_ITEMS = [
    ("dashboard", "Leads", "/dashboard", "reply"),
    ("booked", "Meetings", "/dashboard?stage=booked", "booked"),
    ("inbox", "Inbox", "/dashboard/soon?name=Master+Inbox", "inbox"),
    ("settings", "Settings", "/dashboard/settings", "settings"),
    ("support", "Support", "/dashboard/soon?name=Help+%26+Support", "support"),
]

STAGE_DEFS = [
    ("new", "New"),
    ("replied", "Replied"),
    ("booked", "Meeting Booked"),
    ("won", "Won"),
    ("lost", "Lost"),
    ("stopped", "Stopped"),
]
STAGE_LABELS = dict(STAGE_DEFS)


def stage_badge(stage):
    kind = {"booked": "ok", "won": "ok", "replied": "blue",
            "new": "muted", "lost": "no", "stopped": "muted"}.get(stage, "muted")
    return pill(STAGE_LABELS.get(stage, stage or "—"), kind)

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
  var ta = document.getElementById('reply-edit');
  var ss = document.getElementById('stage-sel');
  var fd = new FormData();
  fd.append('action', action);
  if(ta) fd.append('reply', ta.value);
  if(ss) fd.append('stage', ss.value);
  if(action==='delete' && !confirm('Delete this lead? This cannot be undone.')) return;
  var reloaders = ['stop','mark_done','set_stage','mark_booked','delete'];
  return fetch('/dashboard/leads/'+id+'/action', {method:'POST', body:fd})
    .then(function(r){return r.json();})
    .then(function(d){ if(d && d.ok){ if(reloaders.indexOf(action)>-1){ location.reload(); } else { openDrawer(id); } } });
}
function sendReply(id){
  var ta = document.getElementById('reply-edit');
  var st = document.getElementById('send-status');
  if(!confirm('Send this reply to the prospect now?')) return;
  if(st){ st.textContent = 'Sending...'; }
  var fd = new FormData(); fd.append('reply', ta ? ta.value : '');
  fetch('/dashboard/leads/'+id+'/send', {method:'POST', body:fd})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d && d.ok){ if(st){ st.textContent = 'Sent ✓'; } setTimeout(function(){ location.reload(); }, 700); }
      else { if(st){ st.textContent = 'Send failed: ' + ((d && d.error) || 'unknown'); } }
    }).catch(function(){ if(st){ st.textContent = 'Send failed (network).'; } });
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
  if(action==='delete' && !confirm('Delete '+ids.length+' lead(s)? This cannot be undone.')) return;
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


def _nav_badge(count, danger=False):
    if not count:
        return ""
    cls = "nbadge danger" if danger else "nbadge"
    return f'<span class="{cls}">{count}</span>'


def layout(title, active, body, current_ws="", with_drawer=False):
    user = os.getenv("DASHBOARD_USER", "admin")
    initials = (user[:2] or "AD").upper()

    try:
        counts = db.lead_counts(current_ws or None)
    except Exception:
        counts = {"total": 0, "needs_review": 0, "replied": 0, "booked": 0, "stopped": 0}

    # (key, label, icon, href, badge_count, danger)
    replies_nav = [
        ("all", "All Replies", "dashboard", "/dashboard", counts.get("total", 0), False),
        ("needs_review", "Needs Review", "flag", "/dashboard?status=needs_review", counts.get("needs_review", 0), True),
        ("replied", "Replied", "reply", "/dashboard?status=replied", counts.get("replied", 0), False),
        ("booked", "Meeting Booked", "event_available", "/dashboard?stage=booked", counts.get("booked", 0), False),
        ("stopped", "Stopped", "block", "/dashboard?status=stopped", counts.get("stopped", 0), False),
    ]
    manage_nav = [
        ("workspaces", "Workspaces", "corporate_fare", "/dashboard/workspaces", None, False),
        ("settings", "Settings", "settings", "/dashboard/settings", None, False),
        ("support", "Support", "help", "/dashboard/soon?name=Help+%26+Support", None, False),
    ]

    def nav_links(items):
        out = ""
        for key, label, ic, href, badge, danger in items:
            cls = "active" if key == active else ""
            out += (f'<a class="{cls}" href="{href}">'
                    f'<span class="material-symbols-outlined">{ic}</span>'
                    f'<span class="nlbl">{e(label)}</span>{_nav_badge(badge, danger)}</a>')
        return out

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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0&display=swap" rel="stylesheet">
<title>{e(title)}</title><style>{PAGE_CSS}</style></head><body>
<div class="app">
  <aside class="lsidebar">
    <div class="lbrand"><span class="chip">A</span><span>Ascendly</span></div>
    <div class="lsec">Replies</div>
    <nav class="lnav">{nav_links(replies_nav)}</nav>
    <div class="lsec">Manage</div>
    <nav class="lnav">{nav_links(manage_nav)}</nav>
    <div class="lspacer"></div>
    <a class="lnav-logout" href="/dashboard/logout"><span class="material-symbols-outlined">logout</span><span class="nlbl">Logout</span></a>
    <div class="luser"><div class="avatar">{e(initials)}</div><div><div class="lu-nm">{e(user)}</div><div class="lu-ws">{e(current_ws or "All workspaces")}</div></div></div>
  </aside>
  <div class="lmain">
    <div class="ltop">
      <form method="get" action="/dashboard" class="lsearch">
        <span class="material-symbols-outlined">search</span>
        <input type="text" name="q" placeholder="Search name, email or company...">
      </form>
      <div class="ltop-rt">{workspace_switcher(current_ws)}
        <span class="bell"><span class="material-symbols-outlined">notifications</span><span class="bdot"></span></span>
      </div>
    </div>
    <div class="lcontent">{body}</div>
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
    return HTMLResponse(layout(name, "support", body, current_ws=request.cookies.get("ws", "")))


def avatar_initials(name, email):
    s = (name or email or "?").strip()
    parts = s.split()
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    return s[:2].upper()


def intent_dot(intent):
    i = (intent or "").lower()
    pos = any(k in i for k in ["positive", "interest", "share", "meeting", "yes", "book"])
    return f'<span class="idot {"pos" if pos else ""}"></span>'


@router.get("/dashboard", response_class=HTMLResponse)
def console(request: Request, workspace: str = "", status: str = "", intent: str = "",
            action: str = "", q: str = "", conf: str = "", date_from: str = "",
            date_to: str = "", stage: str = "", page: int = 1, _: str = Depends(require_login)):
    if not workspace:
        workspace = request.cookies.get("ws", "")

    page = max(1, page)
    dto = (date_to + " 23:59:59") if date_to else ""
    rows, total = db.list_leads(
        workspace=workspace or None, status=status or None, intent=intent or None,
        action=action or None, search=q or None, conf_min=conf or None,
        date_from=date_from or None, date_to=dto or None, stage=stage or None,
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
    stage_opts = '<option value="">All stages</option>' + "".join(
        f'<option value="{v}"{" selected" if v==stage else ""}>{l}</option>' for v, l in STAGE_DEFS)

    # ---- rows ----
    body_rows = ""
    for ld in rows:
        when = ld.created_at.strftime("%b %d, %H:%M") if ld.created_at else ""
        sub2 = " • ".join([x for x in [ld.company, ld.campaign] if x])
        sub2_html = f'<div class="lead-sub">{e(sub2)}</div>' if sub2 else ""
        body_rows += f"""<tr>
          <td style="width:34px"><input type="checkbox" class="rowchk" value="{ld.id}" onchange="rowChecked()"></td>
          <td>
            <div class="lead-cell">
              <div class="lead-ava">{e(avatar_initials(ld.name, ld.email))}</div>
              <div>
                <div class="lead-nm">{e(ld.name) or e(ld.email) or 'Lead'}</div>
                <div class="lead-sub">{e(ld.email)}</div>
                {sub2_html}
              </div>
            </div>
          </td>
          <td><div class="intent-cell">{intent_dot(ld.intent)}<span>{e(ld.intent) or '-'}</span></div></td>
          <td>{stage_badge(ld.stage)}</td>
          <td>{reply_badge(ld)}</td>
          <td>{review_badge(ld)}</td>
          <td class="small muted">{e(when)}</td>
          <td style="text-align:right"><button class="eye" onclick="openDrawer({ld.id})" title="View"><span class="material-symbols-outlined">visibility</span></button></td>
        </tr>"""

    if not rows:
        body_rows = ('<tr><td colspan="8"><div class="empty">No replies found.<br>'
                     'Try changing filters or wait for new replies to sync.</div></td></tr>')

    # ---- pagination ----
    pages = max(1, math.ceil(total / PAGE_SIZE))
    base_params = {k: v for k, v in {
        "workspace": workspace, "status": status, "intent": intent, "action": action,
        "q": q, "conf": conf, "date_from": date_from, "date_to": date_to, "stage": stage}.items() if v}

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
    tot = counts['total'] or 1

    def card(n, label, kind="", **override):
        params = {k: v for k, v in base_params.items() if k not in ("status", "stage", "page")}
        params.update({k: v for k, v in override.items() if v})
        href = "/dashboard?" + urlencode(params)
        cls = {"booked": "is-booked", "review": "is-review", "stopped": "is-muted"}.get(kind, "")
        pct = min(100, int(n / tot * 100))
        return (f'<div class="stat {cls}" onclick="location.href=\'{href}\'">'
                f'<div class="l">{label}</div><div class="n">{n}</div>'
                f'<div class="bar"><i style="width:{pct}%"></i></div></div>')

    # AI smart-sorting banner
    banner = ""
    if counts['needs_review'] and stage != "booked":
        nr = "/dashboard?" + urlencode({**{k: v for k, v in base_params.items()
                                            if k not in ("status", "stage", "page")}, "status": "needs_review"})
        banner = f"""
        <div class="banner">
          <span class="material-symbols-outlined bgico">insights</span>
          <h4>AI Smart Sorting</h4>
          <p>Your dashboard is prioritizing {counts['needs_review']} lead(s) that need review so you can follow up fast.</p>
          <a class="bbtn" href="{nr}">View priorities</a>
        </div>"""

    title = "Meeting Booked" if stage == "booked" else "Reply Manager"
    body = f"""
    <div class="secthead">
      <h2>Performance Overview</h2>
    </div>
    <div class="cards">
      {card(counts['total'], 'Total Replies')}
      {card(counts['replied'], 'Replied', status='replied')}
      {card(counts['booked'], 'Meeting Booked', 'booked', stage='booked')}
      {card(counts['needs_review'], 'Needs Review', 'review', status='needs_review')}
      {card(counts['enriched'], 'Follow-ups', status='enriched')}
      {card(counts['stopped'], 'Stopped', 'stopped', status='stopped')}
    </div>

    <div class="secthead" style="margin-top:26px">
      <h2>{e(title) if stage=='booked' else 'Recent Leads Activity'}</h2>
      <a class="btn sm" href="/dashboard/export?{urlencode(base_params)}">Export CSV</a>
    </div>

    <form class="filterbar" id="filterform" method="get" action="/dashboard">
      <input class="grow" type="text" name="q" value="{e(q)}" placeholder="Search name, email or company..." oninput="autoApplySearch()">
      <select name="workspace" onchange="autoApply()">{ws_opts}</select>
      <select name="stage" onchange="autoApply()">{stage_opts}</select>
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
      <button class="btn ok sm" onclick="bulkDo('mark_booked')">Mark booked</button>
      <button class="btn sec sm" onclick="bulkDo('mark_reviewed')">Mark reviewed</button>
      <button class="btn danger sm" onclick="bulkDo('stop')">Stop selected</button>
      <button class="btn sec sm" onclick="bulkDo('export')">Export CSV</button>
      <button class="btn danger sm" onclick="bulkDo('delete')">Delete</button>
    </div>

    <div class="tablewrap">
      <table>
        <thead><tr>
          <th><input type="checkbox" onchange="toggleAll(this)"></th>
          <th>Lead &amp; Company</th><th>Intent</th><th>Stage</th><th>Reply</th>
          <th>Review</th><th>When</th><th style="text-align:right">Actions</th>
        </tr></thead>
        <tbody>{body_rows}</tbody>
      </table>
    </div>
    {pager}
    <div class="flex" style="margin-top:12px">
      <span class="muted small">{total} result(s)</span>
    </div>
    {banner}
    <script>window.__LEAD_IDS__ = {json.dumps(lead_ids)};</script>
    """
    if stage == "booked":
        active_key = "booked"
    elif status in ("needs_review", "replied", "stopped"):
        active_key = status
    else:
        active_key = "all"
    return HTMLResponse(layout(title, active_key, body, current_ws=workspace, with_drawer=True))


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

PRETTY_LABELS = {
    "firstName": "First name", "lastName": "Last name", "jobTitle": "Job title",
    "linkedIn": "LinkedIn", "lead_email": "Email", "email": "Email",
    "companyName": "Company", "company_category": "Company category",
    "ideal_customers": "Ideal customers", "campaign_name": "Campaign",
    "Company Linkedin Url": "Company LinkedIn",
}


def prettify_key(k):
    if k in PRETTY_LABELS:
        return PRETTY_LABELS[k]
    return k.replace("_", " ").strip().capitalize()


# Show these lead-data fields first, in this order, when present.
LEAD_DATA_PRIORITY = [
    "firstName", "lastName", "jobTitle", "email", "lead_email", "Sending email",
    "linkedIn", "website", "Industry", "company_category", "# Employees",
    "Annual Revenue", "Company Address", "ideal_customers", "Company Linkedin Url",
]


def render_lead_data(data):
    if not data:
        return '<p class="muted">No lead data captured for this lead.</p>'

    ordered = [k for k in LEAD_DATA_PRIORITY if k in data and data[k] not in (None, "")]
    rest = [k for k in data if k not in LEAD_DATA_PRIORITY and data[k] not in (None, "")]
    rows = ""
    for k in ordered + rest:
        v = str(data[k])
        if k in ("linkedIn", "website", "Company Linkedin Url") and v.startswith("http"):
            vhtml = f'<a href="{e(v)}" target="_blank" rel="noopener">{e(v)}</a>'
            rows += f'<div class="ld-row"><div class="ld-k">{e(prettify_key(k))}</div><div class="ld-v">{vhtml}</div></div>'
        else:
            rows += (f'<div class="ld-row"><div class="ld-k">{e(prettify_key(k))}</div>'
                     f'<div class="ld-v clamp" onclick="this.classList.toggle(\'open\')">{e(v)}</div></div>')
    return rows


def build_panel(ld):
    try:
        followups = json.loads(ld.followups) if ld.followups else []
    except Exception:
        followups = []
    try:
        data = json.loads(ld.lead_data) if ld.lead_data else {}
    except Exception:
        data = {}

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

    editable = ld.main_reply or (followups[0] if followups else "")

    stage_sel = '<select id="stage-sel">' + "".join(
        f'<option value="{v}"{" selected" if ld.stage==v else ""}>{l}</option>' for v, l in STAGE_DEFS) + "</select>"

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
      {stage_badge(ld.stage)} {action_badge(ld.action)} {reply_badge(ld)} {review_badge(ld)}
    </div>

    <div class="card">
      <h3>Stage</h3>
      <div class="flex">
        {stage_sel}
        <button class="btn sm" onclick="leadAction({ld.id},'set_stage')">Save stage</button>
        <button class="btn ok sm" onclick="leadAction({ld.id},'mark_booked')">Mark booked</button>
      </div>
    </div>

    <div class="card"><h3>Lead details</h3>{render_lead_data(data)}</div>

    <div class="card">
      <h3>Conversation</h3>
      <div class="clip" id="convo-clip">{convo}<div class="clip-fade"></div></div>
      <span class="clip-toggle" onclick="var c=document.getElementById('convo-clip');c.classList.toggle('open');this.textContent=c.classList.contains('open')?'Show less':'Show full conversation';">Show full conversation</span>
    </div>

    <div class="card">
      <h3>Follow-up sequence (AI-drafted)</h3>
      <div class="clip" id="fup-clip">{fups}<div class="clip-fade"></div></div>
      <span class="clip-toggle" onclick="var c=document.getElementById('fup-clip');c.classList.toggle('open');this.textContent=c.classList.contains('open')?'Show less':'Show all follow-ups';">Show all follow-ups</span>
    </div>

    <div class="card">
      <h3>Reply to send</h3>
      <textarea id="reply-edit">{e(editable)}</textarea>
      <div class="flex" style="margin-top:10px">
        <button class="btn sm" onclick="sendReply({ld.id})">Approve &amp; Send</button>
        <button class="btn sec sm" onclick="leadAction({ld.id},'save_reply')">Save draft</button>
        <button class="btn ok sm" onclick="leadAction({ld.id},'mark_reviewed')">Mark reviewed</button>
        <button class="btn sec sm" onclick="leadAction({ld.id},'mark_done')">Done</button>
        <button class="btn danger sm" onclick="leadAction({ld.id},'stop')">Stop</button>
        <button class="btn danger sm" onclick="leadAction({ld.id},'delete')">Delete</button>
      </div>
      <p class="muted small" id="send-status" style="margin-top:8px">Approve &amp; Send delivers this reply through {e(ld.platform)} from your connected account.</p>
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
    reply = form.get("reply", "")

    ld = db.get_lead(lead_id)
    if ld is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    if action == "save_reply":
        db.update_lead_fields(lead_id, main_reply=reply)
    elif action == "mark_reviewed":
        db.update_lead_fields(lead_id, reviewed=True)
    elif action == "stop":
        db.update_lead_fields(lead_id, action="stop", stage="stopped", reviewed=True)
    elif action == "mark_done":
        db.update_lead_fields(lead_id, reviewed=True)
    elif action == "mark_booked":
        db.update_lead_fields(lead_id, stage="booked", reviewed=True)
    elif action == "set_stage":
        st = form.get("stage", "")
        if st:
            db.update_lead_fields(lead_id, stage=st)
    elif action == "delete":
        db.delete_leads([lead_id])

    return JSONResponse({"ok": True})


@router.post("/dashboard/leads/{lead_id}/send")
async def send_reply(lead_id: int, request: Request, _: str = Depends(require_login)):
    """Actually send the (edited) reply to the prospect via Bison/Instantly."""
    import main  # lazy import to avoid circular import at module load

    form = await request.form()
    text = (form.get("reply", "") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "empty reply"})

    ld = db.get_lead(lead_id)
    if ld is None:
        return JSONResponse({"ok": False, "error": "lead not found"})

    ws = db.get_workspace_config(ld.workspace_name)
    if not ws or not ws.get("api_key"):
        return JSONResponse({"ok": False, "error": "workspace API key missing"})

    try:
        meta = json.loads(ld.send_meta) if ld.send_meta else {}
    except Exception:
        meta = {}

    try:
        if ld.platform == "instantly":
            uuid = main.get_latest_instantly_email_id(
                meta.get("email") or ld.email, meta.get("campaign_id", ""), ws["api_key"])
            if not uuid:
                return JSONResponse({"ok": False, "error": "could not locate original email to thread"})
            eaccount = meta.get("eaccount", "")
            if not eaccount:
                return JSONResponse({"ok": False, "error": "no sending account on file"})
            main.send_instantly_reply(uuid, text, eaccount, meta.get("subject", "Re:"), ws["api_key"])
            ok = True
        else:  # bison
            base = (ws.get("base_url") or db.get_setting("emailbison_base_url", "")).rstrip("/")
            if not base:
                return JSONResponse({"ok": False, "error": "no Bison base URL"})
            res = main.send_reply_to_bison(
                reply_id=meta.get("reply_id") or ld.reply_id,
                message=main.format_email_for_bison(text),
                sender_email_id=meta.get("sender_email_id"),
                to_name=meta.get("to_name") or ld.name,
                to_email=meta.get("to_email") or ld.email,
                api_key=ws["api_key"],
                base_url=base,
            )
            ok = bool(res)
    except Exception as ex:
        return JSONResponse({"ok": False, "error": str(ex)[:200]})

    if ok:
        db.update_lead_fields(lead_id, replied=True, reviewed=True, main_reply=text)
    return JSONResponse({"ok": ok, "error": None if ok else "send failed at provider"})


@router.post("/dashboard/bulk")
async def bulk(request: Request, _: str = Depends(require_login)):
    form = await request.form()
    action = form.get("action", "")
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    if ids:
        if action == "mark_reviewed":
            db.bulk_update_leads(ids, reviewed=True)
        elif action == "stop":
            db.bulk_update_leads(ids, action="stop", stage="stopped", reviewed=True)
        elif action == "mark_booked":
            db.bulk_update_leads(ids, stage="booked", reviewed=True)
        elif action == "delete":
            db.delete_leads(ids)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/dashboard/export")
def export_csv(request: Request, workspace: str = "", status: str = "", intent: str = "",
               action: str = "", q: str = "", conf: str = "", date_from: str = "",
               date_to: str = "", stage: str = "", ids: str = "", _: str = Depends(require_login)):
    id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()] if ids else None
    rows = db.export_leads(workspace=workspace or None, status=status or None, intent=intent or None,
                           action=action or None, search=q or None, conf_min=conf or None,
                           date_from=date_from or None, date_to=date_to or None,
                           stage=stage or None, ids=id_list)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Created", "Workspace", "Platform", "Name", "Email", "Company", "Campaign",
                "Intent", "Confidence", "Stage", "Action", "Replied", "FUP added", "Reviewed", "Reply text"])
    for ld in rows:
        w.writerow([ld.created_at, ld.workspace_name, ld.platform, ld.name, ld.email, ld.company,
                    ld.campaign, ld.intent, ld.confidence, ld.stage, ld.action, ld.replied, ld.fup_added,
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
    mode = "reply" if is_new else (ws.mode or "reply")
    mrep = " selected" if mode == "reply" else ""
    mfup = " selected" if mode == "followup" else ""
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
        <label>Mode</label>
        <select name="mode" style="width:100%"><option value="reply"{mrep}>reply (normal)</option><option value="followup"{mfup}>follow-up (thread-aware, FUP1-FUP8)</option></select>
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
        <div class="row2">
          <div><label>Calendly Personal Access Token</label><input type="text" name="calendly_token" value="{e(g('calendly_token'))}" placeholder="optional — enables real availability" style="width:100%"></div>
          <div><label>Calendly scheduling link</label><input type="text" name="calendly_scheduling_url" value="{e(g('calendly_scheduling_url'))}" placeholder="https://calendly.com/rosis/new-meeting" style="width:100%"></div>
        </div>
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
        "mode": form.get("mode", "reply"),
        "api_key": form.get("api_key", ""), "base_url": form.get("base_url", ""),
        "reply_followup_campaign_id": form.get("reply_followup_campaign_id", ""),
        "website": form.get("website", ""), "sender_name": form.get("sender_name", ""),
        "default_sender_email": form.get("default_sender_email", ""),
        "calendly_token": form.get("calendly_token", ""),
        "calendly_scheduling_url": form.get("calendly_scheduling_url", ""),
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
        <div class="row2">
          <div><label>Follow-up trigger tag (Bison)</label><input type="text" name="followup_trigger_tag" value="{e(s.get('followup_trigger_tag','Begin follow-up'))}" style="width:100%"></div>
          <div><label>Reply trigger tag (Bison)</label><input type="text" name="reply_trigger_tag" value="{e(s.get('reply_trigger_tag','Interested'))}" style="width:100%"></div>
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
    for key in ["openai_api_key", "human_review_webhook_url", "emailbison_base_url",
                "reply_delay_seconds", "followup_trigger_tag", "reply_trigger_tag"]:
        db.set_setting(key, form.get(key, ""))
    return RedirectResponse("/dashboard/settings?saved=1", status_code=303)
