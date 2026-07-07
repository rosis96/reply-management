#!/usr/bin/env python3
"""
Ascendly Studio — client workspaces, blueprints, agreements, Studio AI.

Studio (this app) is the internal cockpit. The ascendly-client-portals
service is the document engine behind blueprint./agreement.ascendly.one.
They stay separate deployables, connected via the portals Service API.

Env:
  PORTALS_API_URL   e.g. https://ascendly-client-portals-production.up.railway.app
  PORTALS_API_KEY   must equal SERVICE_API_KEY on the portals service
  OPENAI_API_KEY    Studio AI (optional) · OPENAI_MODEL default gpt-4o-mini
"""

import os
import re
import json

import requests
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from dashboard import layout, require_login, pill, e

router = APIRouter()

PORTALS_URL = (os.getenv("PORTALS_API_URL") or "http://localhost:3001").rstrip("/")
PORTALS_KEY = os.getenv("PORTALS_API_KEY", "")


# ============================================================
# Service client
# ============================================================

def svc(method, path, body=None, timeout=150):
    if not PORTALS_KEY:
        return {"ok": False, "error": "PORTALS_API_KEY is not set — add it (and PORTALS_API_URL) to Studio's environment."}
    try:
        r = requests.request(method, PORTALS_URL + path, json=body,
                             headers={"X-Service-Key": PORTALS_KEY}, timeout=timeout)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "error": f"Portals service returned {r.status_code}: {r.text[:200]}"}
    except Exception as ex:
        return {"ok": False, "error": f"Portals service unreachable: {ex}"}


STATUS_PILL = {
    "draft": ("Draft", "muted"),
    "published": ("Live", "blue"),
    "client_signed": ("Client signed", "warn"),
    "executed": ("Executed", "ok"),
    "active": ("Active", "ok"),
}

EVENT_LABELS = {
    "created": "Client workspace created",
    "updated": "Details updated",
    "patched": "Fields updated",
    "ai_regenerated": "Blueprint personalization regenerated",
    "client_signed": "Client signed the agreement",
    "countersigned": "Countersigned — agreement executed",
    "pdf_generated": "Executed PDF generated & emailed",
    "blueprint_viewed": "Blueprint viewed by client",
    "agreement_viewed": "Agreement viewed by client",
}

STAGES = ["Draft", "Published", "Client signed", "Executed", "Active"]


def stage_idx(c):
    if c.get("active"):
        return 4
    if c.get("executedAt") or (c.get("signature") or {}).get("executedAt"):
        return 3
    if c.get("clientSignedAt") or (c.get("signature") or {}).get("clientSignedAt"):
        return 2
    if c.get("published", True):
        return 1
    return 0


def status_pill(status):
    label, kind = STATUS_PILL.get(status, (status or "—", "muted"))
    return pill(label, kind)


def ev_label(ev):
    return EVENT_LABELS.get(ev, ev.replace("_", " ").capitalize())


def _fmt_at(at):
    return e((at or "")[:16].replace("T", " "))


# ============================================================
# Shared CSS
# ============================================================

REV_CSS = """
<style>
.rv-top{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.rv-top h1{margin:0}
.rv-cols{display:grid;grid-template-columns:1fr 360px;gap:16px;align-items:start}
.rv-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.rv-card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:18px;margin-bottom:14px}
.rv-card h3{margin:0 0 10px;font-size:13.5px}
.rv-card .muted{color:#6b7280}
.rv-links a{display:block;font-size:12.5px;word-break:break-all;margin-bottom:4px}
.rv-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.rv-kv{font-size:13px;line-height:1.9}
.rv-kv b{display:inline-block;min-width:140px;color:#6b7280;font-weight:500}
.rv-tl{list-style:none;padding:0;margin:0;font-size:12.5px}
.rv-tl li{padding:7px 0 7px 18px;border-left:2px solid #e5e7eb;position:relative;margin-left:6px}
.rv-tl li::before{content:"";position:absolute;left:-5px;top:12px;width:8px;height:8px;border-radius:99px;background:#7c6cf6}
.rv-tl li.view::before{background:#38bdf8}
.rv-tl .t{color:#9ca3af;font-size:11px}
.rv-err{background:#fef2f2;border:1px solid #fecaca;color:#b91c1c;border-radius:10px;padding:12px 16px;margin-bottom:14px;font-size:13px}
.rv-ok{background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;border-radius:10px;padding:12px 16px;margin-bottom:14px;font-size:13px}
.rv-form label{display:block;font-size:11px;font-weight:600;color:#6b7280;margin:12px 0 4px;text-transform:uppercase;letter-spacing:.04em}
.rv-form input,.rv-form textarea{width:100%;border:1px solid #e5e7eb;border-radius:8px;padding:9px 11px;font:inherit;font-size:13.5px}
.rv-form textarea{min-height:80px}
.rv-form .cols{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
/* stage strip */
.rv-stages{display:flex;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px 20px;margin-bottom:14px;align-items:center}
.rv-st{flex:1;text-align:center;position:relative;font-size:11px;color:#9ca3af}
.rv-st .dot{width:26px;height:26px;border-radius:99px;margin:0 auto 6px;display:flex;align-items:center;justify-content:center;font-size:11px;background:#f3f4f6;border:1.5px solid #e5e7eb;position:relative;z-index:2}
.rv-st::before{content:"";position:absolute;top:13px;left:calc(-50% + 14px);right:calc(50% + 14px);height:2px;background:#e5e7eb}
.rv-st:first-child::before{display:none}
.rv-st.done{color:#374151}.rv-st.done .dot{background:#7c6cf6;border-color:#7c6cf6;color:#fff}
.rv-st.done::before{background:#7c6cf6}
.rv-st.cur{color:#7c6cf6;font-weight:600}.rv-st.cur .dot{border-color:#7c6cf6;color:#7c6cf6;background:#f5f3ff}
/* next action bar */
.rv-next{display:flex;align-items:center;gap:12px;background:#f5f3ff;border:1px solid #ddd6fe;border-radius:12px;padding:14px 18px;margin-bottom:14px;font-size:13.5px}
.rv-next b{color:#5b21b6}
.rv-next .sp{margin-left:auto;display:flex;gap:8px}
/* tabs */
.rv-tabs{display:flex;gap:2px;border-bottom:1px solid #e5e7eb;margin-bottom:14px}
.rv-tab{padding:9px 16px;font-size:13px;color:#6b7280;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
.rv-tab.on{color:#7c6cf6;border-bottom-color:#7c6cf6;font-weight:600}
.rv-pane{display:none}.rv-pane.on{display:block}
/* AI panel */
.rv-ai{background:#fff;border:1px solid #e5e7eb;border-radius:12px;display:flex;flex-direction:column;position:sticky;top:14px;height:calc(100vh - 120px);overflow:hidden}
.rv-ai-h{padding:12px 16px;border-bottom:1px solid #e5e7eb;font-size:12.5px;font-weight:600;display:flex;gap:8px;align-items:center}
.rv-ai-h .dot2{width:8px;height:8px;border-radius:99px;background:#34d399}
.rv-msgs{flex:1;overflow-y:auto;padding:14px}
.rv-msg{max-width:88%;padding:9px 12px;border-radius:11px;margin-bottom:8px;font-size:12.5px;white-space:pre-wrap;line-height:1.55}
.rv-msg.user{background:#7c6cf6;color:#fff;margin-left:auto;border-bottom-right-radius:4px}
.rv-msg.ai{background:#f3f4f6;border-bottom-left-radius:4px}
.rv-msg.act{background:#ecfdf5;border:1px solid #a7f3d0;font-size:11px;color:#065f46;max-width:100%}
.rv-inp{display:flex;gap:8px;padding:10px;border-top:1px solid #e5e7eb}
.rv-inp input{flex:1;border:1px solid #e5e7eb;border-radius:9px;padding:9px 12px;font:inherit;font-size:13px}
/* home */
.rv-home{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.rv-item{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid #f3f4f6;font-size:13px}
.rv-item:last-child{border:none}
.rv-item .sp{margin-left:auto}
.rv-count{font-size:20px;font-weight:700;color:#7c6cf6}
@media(max-width:1100px){.rv-cols{grid-template-columns:1fr}.rv-ai{position:static;height:480px}}
@media(max-width:900px){.rv-grid,.rv-home,.rv-form .cols{grid-template-columns:1fr}}
</style>
"""


def _err(msg):
    return f'<div class="rv-err">{e(msg)}</div>'


# ============================================================
# HOME — operational command center
# ============================================================

@router.get("/home", response_class=HTMLResponse)
def home(request: Request, _: str = Depends(require_login)):
    """Studio Home — the revenue command center. No inbox, no reply queues."""
    data = svc("GET", "/service/clients")
    clients = data.get("clients", []) if data.get("ok") else []
    svc_err = "" if data.get("ok") else _err(data.get("error", ""))

    by = {"draft": [], "published": [], "client_signed": [], "executed": [], "active": []}
    for c in clients:
        by.setdefault(c.get("status", "draft"), []).append(c)

    def money_num(v):
        digits = "".join(ch for ch in str(v or "") if ch.isdigit())
        return int(digits) if digits else 0

    mrr = sum(money_num(c.get("investment")) for c in by["active"])
    pipeline_value = sum(money_num(c.get("total")) for c in by["draft"] + by["published"] + by["client_signed"])

    def chip(n, label, href="/clients"):
        return (f'<a href="{href}" style="text-decoration:none;color:inherit;flex:1">'
                f'<div class="rv-card" style="text-align:center;margin:0"><div class="rv-count">{n}</div>'
                f'<div class="muted" style="font-size:12px">{e(label)}</div></div></a>')

    chips = ('<div style="display:flex;gap:12px;margin-bottom:14px">'
             + chip(len(by["draft"]), "Draft blueprints")
             + chip(len(by["published"]), "Live — awaiting signature")
             + chip(len(by["client_signed"]), "Awaiting countersign")
             + chip(len(by["executed"]), "Ready to onboard")
             + chip(len(by["active"]), "Active clients")
             + f'<div class="rv-card" style="text-align:center;margin:0;flex:1;background:#f5f3ff;border-color:#ddd6fe">'
               f'<div class="rv-count">${mrr:,}</div><div class="muted" style="font-size:12px">MRR (active)</div></div>'
             + '</div>')

    def rows(items, action_html):
        return "".join(
            f'<div class="rv-item"><a href="/clients/{e(c["slug"])}" style="text-decoration:none;color:inherit"><b>{e(c["companyName"])}</b></a>'
            f'<span class="muted small">{e(c.get("clientName", ""))}</span>'
            f'<span class="sp">{action_html(c)}</span></div>'
            for c in items[:5])

    cs_html = rows(by["client_signed"], lambda c:
        f'<a class="btn sm" href="{e(c["links"]["countersignUrl"])}" target="_blank">Countersign now</a>')         or '<div class="muted" style="font-size:13px">Nothing waiting on your signature.</div>'

    onboard_html = rows(by["executed"], lambda c:
        f'<a class="btn sm" href="/clients/{e(c["slug"])}">Start onboarding</a>')         or '<div class="muted" style="font-size:13px">No executed agreements pending kickoff.</div>'

    draft_html = rows(by["draft"], lambda c:
        f'<a class="btn sec sm" href="{e(c["links"]["previewBlueprintUrl"])}" target="_blank">Review</a>')         or '<div class="muted" style="font-size:13px">Nothing in draft.</div>'

    live_html = rows(by["published"], lambda c:
        f'<a class="btn sec sm" href="/clients/{e(c["slug"])}">Open</a>')         or '<div class="muted" style="font-size:13px">No blueprints out with prospects right now.</div>'

    act = svc("GET", "/service/activity?limit=12")
    act_html = "".join(
        f'<li class="{"view" if a["event"].endswith("_viewed") else ""}">'
        f'<div class="t">{_fmt_at(a.get("at"))}</div>'
        f'<a href="/clients/{e(a.get("slug", ""))}"><b>{e(a.get("companyName", ""))}</b></a> — {e(ev_label(a.get("event", "")))}</li>'
        for a in act.get("events", [])) or '<li class="muted">No activity yet.</li>'

    body = f"""{REV_CSS}{svc_err}
    <div class="rv-top"><h1>Home</h1>
      <span class="muted" style="font-size:13px">Revenue pipeline · ${pipeline_value:,} in play</span>
      <div style="margin-left:auto;display:flex;gap:8px">
        <a class="btn sec" href="/assistant">Ask Studio AI</a>
        <a class="btn" href="/clients/new">+ New Client</a>
      </div>
    </div>
    {chips}
    <div class="rv-home">
      <div class="rv-card"><h3>✍️ Awaiting your countersign</h3>{cs_html}</div>
      <div class="rv-card"><h3>🚀 Ready to onboard</h3>{onboard_html}</div>
      <div class="rv-card"><h3>📝 Blueprints in draft</h3>{draft_html}</div>
      <div class="rv-card"><h3>👀 With prospects — awaiting signature</h3>{live_html}</div>
    </div>
    <div class="rv-card"><h3>Recent activity</h3><ul class="rv-tl">{act_html}</ul></div>
    """
    return layout("Home", "home", body)


# ============================================================
# CLIENTS — unified list
# ============================================================

@router.get("/clients", response_class=HTMLResponse)
def clients_list(request: Request, _: str = Depends(require_login)):
    data = svc("GET", "/service/clients")
    banner = "" if data.get("ok") else _err(data.get("error", "Unknown error"))
    rows = ""
    for c in data.get("clients", []):
        rows += f"""<tr onclick="location.href='/clients/{e(c["slug"])}'" style="cursor:pointer">
          <td><b>{e(c.get("companyName", ""))}</b><div class="muted small">{e(c.get("clientName", ""))} · {e(c.get("clientEmail", ""))}</div></td>
          <td>{status_pill(c.get("status"))}</td>
          <td class="small">{e(c.get("agreementId", ""))}</td>
          <td class="small">{e(c.get("investment", ""))}</td>
          <td class="small">{e((c.get("createdAt") or "")[:10])}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="5" class="empty">No clients yet — create one, or ask Studio AI.</td></tr>'
    body = f"""{REV_CSS}{banner}
    <div class="rv-top"><h1>Clients</h1>
      <div style="margin-left:auto"><a class="btn" href="/clients/new">+ New Client</a></div>
    </div>
    <div class="tablewrap"><table>
      <thead><tr><th>Company</th><th>Stage</th><th>Agreement ID</th><th>Investment</th><th>Created</th></tr></thead>
      <tbody>{rows}</tbody></table></div>
    """
    return layout("Clients", "clients", body)


# ============================================================
# TEMPLATES
# ============================================================

@router.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, _: str = Depends(require_login)):
    cards = [
        ("Revenue Blueprint", "The personalized strategy presentation every prospect receives. Studio AI tailors the bottlenecks, focus and summary to each client from your discovery call.", "Active", "ok"),
        ("Agreement", "Your standard Revenue Growth System agreement — e-signature, countersign and executed PDF included. Commercial terms are set per client.", "Active", "ok"),
        ("Client emails", "The signing notification you receive and the executed-agreement email your client receives.", "Active", "ok"),
        ("AI personalization", "The rules Studio AI follows when writing client-specific blueprint content.", "Active", "ok"),
        ("Engagement pricing", "Default terms for new clients: $4,000/month · 3 months · $12,000 total. Adjust per client at creation, in the workspace, or by asking Studio AI.", "Default", "blue"),
    ]
    html = "".join(
        f'<div class="rv-card"><h3>{e(t)}</h3><p class="muted" style="font-size:13px;margin:0 0 10px">{e(d)}</p>{pill(st, kind)}</div>'
        for t, d, st, kind in cards)
    body = f"""{REV_CSS}
    <div class="rv-top"><h1>Templates</h1>
      <span class="muted" style="font-size:13px">The building blocks every client engagement is created from. In-Studio editing is coming.</span></div>
    <div class="rv-grid">{html}</div>
    """
    return layout("Templates", "templates", body)


# ============================================================
# STUDIO SETTINGS (Studio-scoped only — outbound config lives in Reply Settings)
# ============================================================

@router.get("/studio/settings", response_class=HTMLResponse)
def studio_settings(request: Request, _: str = Depends(require_login)):
    # connection health
    data = svc("GET", "/service/clients")
    if data.get("ok"):
        n = len(data.get("clients", []))
        conn = f'{pill("Connected", "ok")} <span class="muted" style="font-size:13px">Document service is reachable · {n} client workspace{"s" if n != 1 else ""}</span>'
        links = (data["clients"][0].get("links", {}) if data.get("clients") else {})
    else:
        conn = f'{pill("Not connected", "no")} <span class="muted" style="font-size:13px">{e(data.get("error", ""))}</span>'
        links = {}

    def host(u):
        try:
            return u.split("//", 1)[1].split("/", 1)[0]
        except Exception:
            return "—"

    bp_host = host(links.get("blueprintUrl", "https://blueprint.ascendly.one/x"))
    ag_host = host(links.get("agreementUrl", "https://agreement.ascendly.one/x"))
    ai_on = bool(os.getenv("OPENAI_API_KEY"))
    ai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    info = svc("GET", "/service/info")
    gen_model = info.get("generationModel", "unknown") if info.get("ok") else "unknown"
    gen_ver = info.get("version", "unknown") if info.get("ok") else "unknown"
    gen_on = bool(info.get("aiEnabled")) if info.get("ok") else False

    body = f"""{REV_CSS}
    <div class="rv-top"><h1>Studio Settings</h1>
      <span class="muted" style="font-size:13px">Outbound & reply configuration lives under Manage → Reply Settings.</span></div>
    <div class="rv-grid">
      <div class="rv-card"><h3>Document service</h3>
        <div style="margin-bottom:8px">{conn}</div>
        <div class="rv-kv"><b>Client blueprints</b> {e(bp_host)}<br><b>Client agreements</b> {e(ag_host)}</div>
        <p class="muted" style="font-size:12px;margin:10px 0 0">Clients only ever see these two domains — Studio stays internal.</p>
      </div>
      <div class="rv-card"><h3>Document generation (Blueprints &amp; Agreements)</h3>
        <div style="margin-bottom:8px">{pill("Enabled", "ok") if gen_on else pill("Disabled", "no")} {pill(gen_ver, "blue")}</div>
        <div class="rv-kv"><b>Model</b> {e(gen_model)}</div>
        <p class="muted" style="font-size:12px;margin:10px 0 0">Writes the client-facing content from your call transcripts. Transcript decisions always take priority — defaults below apply only when the call never covered terms.</p>
      </div>
      <div class="rv-card"><h3>Studio AI (chat assistant)</h3>
        <div style="margin-bottom:8px">{pill("Enabled", "ok") if ai_on else pill("Disabled", "no")}</div>
        <div class="rv-kv"><b>Model</b> {e(ai_model)}</div>
        <p class="muted" style="font-size:12px;margin:10px 0 0">{"Available in every client workspace and on the AI Assistant page." if ai_on else "Add an OpenAI key to Studio's environment to enable the assistant."}</p>
      </div>
      <div class="rv-card"><h3>Default engagement</h3>
        <div class="rv-kv"><b>Investment</b> $4,000/month<br><b>Initial term</b> 3 months<br><b>Total</b> $12,000</div>
        <p class="muted" style="font-size:12px;margin:10px 0 0">Applied to new clients; adjustable per client at any time before signing.</p>
      </div>
      <div class="rv-card"><h3>Notifications</h3>
        <div class="rv-kv"><b>Internal email</b> rosis_s@ascendly.one</div>
        <p class="muted" style="font-size:12px;margin:10px 0 0">Receives signing alerts and countersign requests. Override per client when creating or editing.</p>
      </div>
    </div>
    """
    return layout("Studio Settings", "studio_settings", body)


# ============================================================
# CLIENT WORKSPACE
# ============================================================

def _stage_strip(idx):
    out = ""
    for i, name in enumerate(STAGES):
        cls = "done" if i < idx else ("cur" if i == idx else "")
        mark = "✓" if i < idx else str(i + 1)
        out += f'<div class="rv-st {cls}"><div class="dot">{mark}</div>{e(name)}</div>'
    return f'<div class="rv-stages">{out}</div>'


def _next_action(c, slug, links):
    idx = stage_idx(c)
    prev = e(links.get("previewBlueprintUrl", ""))
    bp = e(links.get("blueprintUrl", ""))
    cs = e(links.get("countersignUrl", ""))
    dl = e(links.get("downloadUrl", ""))
    if idx == 0:
        return (f'<div class="rv-next"><b>Next:</b> review the draft, then publish.'
                f'<span class="sp"><a class="btn sec sm" target="_blank" href="{prev}">Preview draft</a>'
                f'<button class="btn sm" onclick="rvPost(\'/clients/{e(slug)}/publish\',{{published:true}},this)">Publish</button></span></div>')
    if idx == 1:
        return (f'<div class="rv-next"><b>Next:</b> send the blueprint link to the client.'
                f'<span class="sp"><button class="btn sm" onclick="rvCopy(\'{bp}\',this)">Copy blueprint link</button>'
                f'<a class="btn sec sm" target="_blank" href="{bp}">Open</a></span></div>')
    if idx == 2:
        return (f'<div class="rv-next"><b>Next:</b> the client signed — countersign to execute.'
                f'<span class="sp"><a class="btn sm" target="_blank" href="{cs}">Countersign now</a></span></div>')
    if idx == 3:
        return (f'<div class="rv-next"><b>Next:</b> executed & emailed. Kick off delivery.'
                f'<span class="sp"><a class="btn sec sm" href="{dl}">Download PDF</a>'
                f'<button class="btn sm" onclick="rvPost(\'/clients/{e(slug)}/patchjs\',{{active:true}},this)">Mark Active</button></span></div>')
    return (f'<div class="rv-next" style="background:#ecfdf5;border-color:#a7f3d0"><b style="color:#065f46">Engagement active.</b>'
            f'<span class="sp"><a class="btn sec sm" href="{dl}">Executed PDF</a></span></div>')


@router.get("/clients/new", response_class=HTMLResponse)
@router.get("/clients/{slug}/edit", response_class=HTMLResponse)
def client_form(request: Request, slug: str = "", _: str = Depends(require_login)):
    c, intake = {}, {}
    editing = bool(slug)
    if editing:
        data = svc("GET", f"/service/clients/{slug}")
        if data.get("ok"):
            c = data["client"]
            intake = c.get("intake") or {}
    else:
        # handoff prefill: /clients/new?companyName=...&clientName=...&clientEmail=...
        qp = request.query_params
        for k in ("companyName", "clientName", "clientEmail", "clientTitle", "website", "industry"):
            if qp.get(k):
                c[k] = qp.get(k)
    defaults = {"ascendlyEmail": "rosis_s@ascendly.one", "investment": "$4,000/month", "termMonths": "3", "total": "$12,000"}

    FIELDS = [
        ("companyName", "Company name *"), ("slug", "Slug (auto)"), ("website", "Website"), ("industry", "Industry"),
        ("clientName", "Contact name *"), ("clientTitle", "Contact title"),
        ("clientEmail", "Client email *"), ("ascendlyEmail", "Internal Ascendly email"),
        ("companyAddress", "Company address"), ("clientLogo", "Client logo URL"),
        ("agreementId", "Agreement ID (auto)"), ("investment", "Investment"),
        ("termMonths", "Term (months)"), ("total", "Total"),
        ("startDate", "Start date"), ("preparedDate", "Prepared date"),
    ]
    AREAS = [
        ("transcript", "Fathom transcript (powers AI personalization)"),
        ("currentBottlenecks", "Current bottlenecks (AI fills if empty)"),
        ("recommendedFocus", "Recommended focus (AI fills if empty)"),
        ("salesCallNotes", "Sales call notes"), ("promisedScope", "Promised scope"),
        ("pricingNotes", "Pricing notes"), ("risks", "Risks / sensitive items"),
        ("blueprintInstructions", "Custom blueprint instructions"),
    ]
    inputs = ""
    for name, label in FIELDS:
        val = str(c.get(name) if c.get(name) is not None else defaults.get(name, ""))
        inputs += f'<div><label>{e(label)}</label><input name="{name}" value="{e(val)}"></div>'
    areas = ""
    for name, label in AREAS:
        key = "mainBottleneck" if name == "currentBottlenecks" else name
        val = str(c.get(key) or intake.get(name) or "")
        areas += f'<label>{e(label)}</label><textarea name="{name}">{e(val)}</textarea>'

    body = f"""{REV_CSS}
    <div class="rv-top"><h1>{'Edit ' + e(c.get("companyName", "")) if editing else 'New Client'}</h1></div>
    <form class="rv-card rv-form" method="post" action="/clients/save">
      <input type="hidden" name="overwrite" value="{'1' if editing else ''}">
      <label>Paste JSON (optional — fills everything)</label>
      <textarea id="rvjson" placeholder='{{"companyName":"...","clientName":"...","clientEmail":"..."}}'></textarea>
      <button type="button" class="btn sec sm" style="margin-top:6px" onclick="rvFill()">Fill from JSON</button>
      <div class="cols" style="margin-top:10px">{inputs}</div>
      {areas}
      <div style="margin-top:18px"><button class="btn" type="submit">{'Save Changes' if editing else 'Create Client Workspace'}</button></div>
    </form>
    <script>
    function rvFill(){{
      try{{
        var o=JSON.parse(document.getElementById('rvjson').value);
        var f=document.forms[0];var n=0;
        function set(k,v){{if(f.elements[k]&&v!=null&&typeof v!=='object'){{f.elements[k].value=v;n++}}}}
        for(var k in o)set(k,o[k]);
        if(o.intake)for(var k2 in o.intake)set(k2,o.intake[k2]);
        if(o.mainBottleneck)set('currentBottlenecks',o.mainBottleneck);
        alert('Filled '+n+' fields');
      }}catch(err){{alert('Invalid JSON: '+err.message)}}
    }}
    </script>
    """
    return layout("New Client" if not editing else "Edit Client", "clients", body)


@router.post("/clients/save")
async def client_save(request: Request, _: str = Depends(require_login)):
    form = dict(await request.form())
    form["overwrite"] = bool(form.get("overwrite"))
    r = svc("POST", "/service/clients", form)
    if not r.get("ok"):
        return HTMLResponse(layout("Error", "clients", REV_CSS + _err(r.get("error", "Failed")) +
                                   '<a class="btn sec" href="javascript:history.back()">Back</a>'))
    return RedirectResponse(f"/clients/{r['slug']}?msg=Saved.", status_code=303)


@router.get("/clients/{slug}", response_class=HTMLResponse)
def client_workspace(slug: str, request: Request, msg: str = "", _: str = Depends(require_login)):
    data = svc("GET", f"/service/clients/{slug}")
    if not data.get("ok"):
        return layout("Client", "clients", REV_CSS + _err(data.get("error", "Not found")))
    c = data["client"]
    links = c.get("links", {})
    sig = c.get("signature", {})
    audit = svc("GET", f"/service/clients/{slug}/audit").get("audit", [])
    idx = stage_idx(c)
    notice = f'<div class="rv-ok">{e(msg)}</div>' if msg else ""

    # timeline — the client relationship history (Studio-scoped events only)
    tl_items = [(a.get("at") or "", ev_label(a.get("event", "")), a.get("detail", ""), a.get("event", "")) for a in audit]
    tl_items.sort(key=lambda x: x[0], reverse=True)
    tl = "".join(
        f'<li class="{"view" if ev.endswith("_viewed") else ""}"><div class="t">{_fmt_at(at)}</div>'
        f'<b>{e(label)}</b>{(" — " + e(detail)) if detail else ""}</li>'
        for at, label, detail, ev in tl_items[:40]) or '<li class="muted">No events yet.</li>'

    intake = c.get("intake") or {}
    intake_rows = "".join(f"<div><b>{e(k)}</b> {e(str(v)[:400])}</div>" for k, v in intake.items() if v) \
        or '<div class="muted">No intake data stored.</div>'
    aisum = c.get("aiSummary") or {}
    mismatch = ""
    if "MISMATCH" in str(aisum.get("notesForAscendly", "")).upper():
        mismatch = _err("⚠ Studio AI flagged a mismatch: the transcript appears to be about a different company than this workspace. Check the AI notes in the Blueprint tab before publishing.")
    ai_block = ""
    if aisum:
        com = aisum.get("commercial") or {}
        com_line = (f'{e(str(com.get("investment", "")))} · {e(str(com.get("termMonths", "")))} months · {e(str(com.get("total", "")))}'
                    if com else "none detected in the call — defaults in use")
        ai_block = (f'<div class="rv-kv" style="margin-top:8px"><b>AI bottleneck</b> {e(aisum.get("mainBottleneck", ""))}<br>'
                    f'<b>AI focus</b> {e(aisum.get("recommendedFocus", ""))}<br>'
                    f'<b>Call pricing</b> {com_line}<br>'
                    f'<b>Notes for us</b> {e(aisum.get("notesForAscendly", ""))}</div>')

    has_transcript = bool(str(intake.get("transcript") or "").strip())
    if not has_transcript:
        transcript_card = f"""
        <div class="rv-card" style="border-color:#ddd6fe;background:#fbfaff">
          <h3>📋 Add the discovery-call transcript</h3>
          <p class="muted" style="font-size:13px;margin:0 0 10px">Paste the Fathom transcript or your meeting notes.
          Studio AI will generate the Blueprint personalization and Agreement variables from it — the blueprint stays in draft until you publish.</p>
          <form method="post" action="/clients/{e(slug)}/transcript" class="rv-form">
            <textarea name="transcript" style="min-height:150px" placeholder="Paste the full call transcript here…"></textarea>
            <div class="rv-actions"><button class="btn" type="submit">Save &amp; Generate Blueprint + Agreement</button></div>
          </form>
        </div>"""
    else:
        tlen = len(str(intake.get("transcript") or ""))
        transcript_card = f"""
        <details class="rv-card" style="margin-bottom:14px">
          <summary style="cursor:pointer;font-size:13px"><b>📋 Transcript on file</b> <span class="muted">({tlen:,} characters — click to replace &amp; regenerate)</span></summary>
          <form method="post" action="/clients/{e(slug)}/transcript" class="rv-form" style="margin-top:10px">
            <textarea name="transcript" style="min-height:150px">{e(str(intake.get("transcript") or ""))}</textarea>
            <div class="rv-actions"><button class="btn sm" type="submit">Replace &amp; Regenerate</button></div>
          </form>
        </details>"""

    # ── internal sales intelligence (never client-visible) ──
    intel = aisum.get("internal") or {}
    ops = intel.get("opportunityScore") or {}
    coach = intel.get("salesCoach") or {}
    missing = [m for m in (intel.get("missingInfo") or []) if m]

    def score_bar(label, v):
        try:
            n = max(0, min(10, float(v)))
        except Exception:
            n = 0
        color = "#34d399" if n >= 7 else ("#fbbf24" if n >= 4 else "#f87171")
        return (f'<div style="margin-bottom:10px"><div style="display:flex;justify-content:space-between;font-size:12px">'
                f'<span style="color:#6b7280">{e(label)}</span><b>{n:g}/10</b></div>'
                f'<div style="height:6px;border-radius:99px;background:#f3f4f6;margin-top:3px">'
                f'<div style="height:6px;border-radius:99px;width:{n * 10}%;background:{color}"></div></div></div>')

    if intel:
        bars = "".join(score_bar(l, ops.get(k)) for k, l in
                       [("overall", "Overall opportunity"), ("budget", "Budget"), ("authority", "Authority"),
                        ("urgency", "Urgency"), ("fit", "Business fit")])
        klist = lambda items: "".join(f'<li style="margin-bottom:5px">{e(str(x))}</li>' for x in (items or []))
        intel_pane = f"""
          <div class="rv-grid">
            <div class="rv-card"><h3>🎯 Opportunity Score <span class="muted" style="font-weight:400">(internal)</span></h3>
              {bars}
              <div class="rv-kv" style="margin-top:10px">
                <b>Close probability</b> {e(str(ops.get("closeProbability") or "—"))}<br>
                <b>Revenue potential</b> {e(str(ops.get("revenuePotential") or "—"))}<br>
                <b>Risk</b> {e(str(ops.get("risk") or "—"))}<br>
                <b>AI confidence</b> {e(str(ops.get("confidence") or "—"))}
              </div>
            </div>
            <div class="rv-card"><h3>🧠 Sales Coach — next call</h3>
              <div class="rv-kv">
                <b>Biggest objection</b> {e(str(coach.get("biggestObjection") or "—"))}<br>
                <b>How to answer</b> {e(str(coach.get("objectionResponse") or "—"))}<br>
                <b>Pricing stance</b> {e(str(coach.get("pricingConfidence") or "—"))}<br>
                <b>Next step</b> {e(str(coach.get("recommendedNextStep") or "—"))}
              </div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px;font-size:12.5px">
                <div><b style="color:#065f46">Emphasize</b><ul style="padding-left:16px;margin:6px 0">{klist(coach.get("emphasize"))}</ul></div>
                <div><b style="color:#b91c1c">Avoid</b><ul style="padding-left:16px;margin:6px 0">{klist(coach.get("avoid"))}</ul></div>
              </div>
              <div style="margin-top:8px;font-size:12.5px"><b>Questions to ask</b><ul style="padding-left:16px;margin:6px 0">{klist(coach.get("questionsToAsk"))}</ul></div>
            </div>
            <div class="rv-card" style="grid-column:1/-1"><h3>❓ Missing information ({len(missing)})</h3>
              {('<ul style="padding-left:16px;font-size:13px;margin:0">' + klist(missing) + '</ul>') if missing else '<span class="muted" style="font-size:13px">Nothing critical missing — the call covered the essentials.</span>'}
            </div>
          </div>"""
    else:
        intel_pane = '<div class="rv-card"><p class="muted" style="font-size:13px;margin:0">No sales intelligence yet — add a transcript and generate. The AI will score the opportunity, prep you for objections, and flag missing information. Internal only; the client never sees this.</p></div>'

    # ── publish readiness (deterministic checks) ──
    checks = [
        ("Transcript on file", has_transcript),
        ("AI personalization generated", bool(aisum)),
        ("Pricing sourced from the call", bool(aisum.get("commercial"))),
        ("No company mismatch flagged", not bool(mismatch)),
        ("Client logo set", bool(str(c.get("clientLogo") or "").strip())),
    ]
    ready_n = sum(1 for _, ok in checks if ok)
    readiness = ('<div class="rv-card"><h3>Publish readiness · ' + f'{ready_n}/{len(checks)}' + '</h3>'
                 + "".join(f'<div class="rv-item" style="padding:6px 0">{"✅" if ok else "▫️"} <span style="font-size:13px">{e(lbl)}</span></div>' for lbl, ok in checks)
                 + ('<p class="muted" style="font-size:12px;margin:8px 0 0">Missing checks are advisory — you can still publish.</p>' if ready_n < len(checks) else
                    '<p style="font-size:12px;margin:8px 0 0;color:#065f46">Ready to present and publish.</p>')
                 + '</div>')

    published = c.get("published", True)
    pub_btn = (f'<button class="btn sec sm" onclick="rvPost(\'/clients/{e(slug)}/publish\',{{published:false}},this)">Unpublish</button>'
               if published else
               f'<button class="btn ok sm" onclick="rvPost(\'/clients/{e(slug)}/publish\',{{published:true}},this)">Publish</button>')

    locked = bool(sig.get("locked"))
    lock_note = '<div class="muted" style="font-size:12px;margin-top:6px">Locked — agreement is executed.</div>' if locked else ""
    dis = "disabled" if locked else ""
    exec_html = ""
    if sig.get("executedAt"):
        exec_html = (f'<a class="btn ok sm" href="{e(links.get("downloadUrl", ""))}">Download PDF</a>'
                     f'<button class="btn sec sm" onclick="rvPost(\'/clients/{e(slug)}/resend\',{{}},this)">Resend email</button>')

    body = f"""{REV_CSS}{notice}{mismatch}
    <div class="rv-top">
      <h1>{e(c.get("companyName", ""))}</h1>
      {status_pill(c.get("status"))}
      <div style="margin-left:auto;display:flex;gap:8px">
        <a class="btn sec sm" href="/clients/{e(slug)}/edit">Edit</a>
        <button class="btn danger sm" onclick="rvDelete('{e(slug)}')">Delete</button>
      </div>
    </div>
    {_stage_strip(idx)}
    {transcript_card}
    {_next_action(c, slug, links)}

    <div class="rv-cols">
      <div>
        <div class="rv-tabs">
          <div class="rv-tab on" data-p="ovr">Overview</div>
          <div class="rv-tab" data-p="bp">Blueprint</div>
          <div class="rv-tab" data-p="ag">Agreement</div>
          <div class="rv-tab" data-p="si">Sales Intel{f' <span style="background:#fef3c7;color:#92400e;border-radius:99px;padding:0 7px;font-size:10.5px">{len(missing)}</span>' if missing else ''}</div>
          <div class="rv-tab" data-p="tl">Timeline</div>
          <div class="rv-tab" data-p="nt">Notes</div>
        </div>

        <div class="rv-pane on" id="p-ovr">
          <div class="rv-card"><h3>Overview</h3>
            <div class="rv-kv">
              <b>Contact</b> {e(c.get("clientName", ""))} · {e(c.get("clientTitle") or "—")}<br>
              <b>Client email</b> {e(c.get("clientEmail", ""))}<br>
              <b>Internal email</b> {e(c.get("ascendlyEmail", ""))}<br>
              <b>Industry</b> {e(c.get("industry") or "—")}<br>
              <b>Investment</b> {e(c.get("investment", ""))} · {e(str(c.get("termMonths", "")))} mo · {e(c.get("total", ""))}<br>
              <b>Main bottleneck</b> {e(c.get("mainBottleneck") or "—")}<br>
              <b>Recommended focus</b> {e(c.get("recommendedFocus") or "—")}
            </div>
            <details style="margin-top:10px"><summary class="muted" style="cursor:pointer;font-size:12.5px">Intake data</summary>
              <div class="rv-kv" style="margin-top:8px">{intake_rows}</div></details>
          </div>
        </div>

        <div class="rv-pane" id="p-bp">
          <div class="rv-card"><h3>Revenue Blueprint</h3>
            <div class="rv-links"><a target="_blank" href="{e(links.get("blueprintUrl", ""))}">{e(links.get("blueprintUrl", ""))}</a></div>
            <div class="rv-actions">
              <a class="btn sm" target="_blank" href="{e(links.get("previewBlueprintUrl", ""))}">Preview</a>
              <a class="btn sm" target="_blank" href="{e(links.get("previewBlueprintUrl", ""))}&present=1" style="background:#7c6cf6;color:#fff;border-color:#7c6cf6">▶ Present</a>
              <button class="btn sec sm" onclick="rvCopy('{e(links.get("blueprintUrl", ""))}',this)">Copy link</button>
              {pub_btn}
              <button class="btn sec sm" onclick="rvPost('/clients/{e(slug)}/regen',{{}},this)">Regenerate AI</button>
            </div>
            {ai_block}
          </div>
          {readiness}
        </div>

        <div class="rv-pane" id="p-ag">
          <div class="rv-card"><h3>Agreement — {e(c.get("agreementId", ""))}</h3>
            <div class="rv-kv">
              <b>Client signed</b> {e(sig.get("clientSignedAt") or "—")}<br>
              <b>Countersigned</b> {e(sig.get("ascendlySignedAt") or "—")}<br>
              <b>Executed</b> {e(sig.get("executedAt") or "—")}
            </div>
            <div class="rv-actions">
              <a class="btn sm" target="_blank" href="{e(links.get("agreementUrl", ""))}">Open</a>
              <button class="btn sec sm" onclick="rvCopy('{e(links.get("agreementUrl", ""))}',this)">Copy link</button>
              <a class="btn sec sm" target="_blank" href="{e(links.get("countersignUrl", ""))}">Countersign</a>
              {exec_html}
            </div>
          </div>
          <div class="rv-card rv-form"><h3>Commercial terms</h3>
            <div class="cols">
              <div><label>Investment</label><input id="f-investment" value="{e(c.get("investment", ""))}" {dis}></div>
              <div><label>Term (months)</label><input id="f-termMonths" value="{e(str(c.get("termMonths", "")))}" {dis}></div>
              <div><label>Total</label><input id="f-total" value="{e(c.get("total", ""))}" {dis}></div>
              <div><label>Start date</label><input id="f-startDate" value="{e(c.get("startDate", ""))}" {dis}></div>
            </div>
            <div class="rv-actions"><button class="btn sm" onclick="rvSaveTerms()" {dis}>Save terms</button></div>
            {lock_note}
          </div>
        </div>

        <div class="rv-pane" id="p-si">{intel_pane}</div>

        <div class="rv-pane" id="p-tl">
          <div class="rv-card"><h3>Timeline</h3><ul class="rv-tl">{tl}</ul></div>
        </div>

        <div class="rv-pane" id="p-nt">
          <div class="rv-card"><h3>Notes</h3><p class="muted" style="font-size:13px">Notes & files are coming in the next sprint — for now, drop context into "Sales call notes" via Edit.</p></div>
        </div>
      </div>

      <div class="rv-ai">
        <div class="rv-ai-h"><span class="dot2"></span>Studio AI · working on {e(c.get("companyName", ""))}</div>
        <div class="rv-msgs" id="msgs">
          <div class="rv-msg ai">I'm loaded with {e(c.get("companyName", ""))}'s full context. Try: "publish the blueprint", "change investment to $5,000/month", "regenerate the personalization", "what's the countersign link?"</div>
        </div>
        <div class="rv-inp">
          <input id="chatin" placeholder="Tell me what to do…" onkeydown="if(event.key==='Enter')aiSend()">
          <button class="btn sm" id="sendbtn" onclick="aiSend()">Send</button>
        </div>
      </div>
    </div>

    <script>
    document.querySelectorAll('.rv-tab').forEach(function(t){{
      t.onclick=function(){{
        document.querySelectorAll('.rv-tab').forEach(x=>x.classList.toggle('on',x===t));
        document.querySelectorAll('.rv-pane').forEach(x=>x.classList.toggle('on',x.id==='p-'+t.dataset.p));
      }};
    }});
    function rvCopy(t,b){{navigator.clipboard.writeText(t).then(()=>{{var o=b.textContent;b.textContent='Copied ✓';setTimeout(()=>b.textContent=o,1500)}})}}
    function rvPost(u,body,b){{
      if(b){{b.disabled=true}}
      fetch(u,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body||{{}})}})
        .then(r=>r.json()).then(j=>{{
          if(j.ok)location.href='/clients/{e(slug)}?msg='+encodeURIComponent(j.msg||'Done');
          else{{alert(j.error||'Failed');location.reload()}}
        }});
    }}
    function rvSaveTerms(){{
      rvPost('/clients/{e(slug)}/patchjs',{{
        investment:document.getElementById('f-investment').value,
        termMonths:parseInt(document.getElementById('f-termMonths').value)||3,
        total:document.getElementById('f-total').value,
        startDate:document.getElementById('f-startDate').value
      }});
    }}
    function rvDelete(slug){{
      if(!confirm('Delete '+slug+'? Removes the portal, agreement state and executed PDF.'))return;
      fetch('/clients/'+slug+'/delete',{{method:'POST'}}).then(r=>r.json())
        .then(j=>{{if(j.ok)location.href='/clients';else alert(j.error||'Failed')}});
    }}
    /* ── embedded Studio AI (context: this client) ── */
    var HIST=[];
    function aiAdd(cls,text){{
      var d=document.createElement('div');d.className='rv-msg '+cls;d.textContent=text;
      document.getElementById('msgs').appendChild(d);d.scrollIntoView({{behavior:'smooth'}});return d;
    }}
    function aiSend(){{
      var inp=document.getElementById('chatin'),t=inp.value.trim();if(!t)return;
      inp.value='';aiAdd('user',t);HIST.push({{role:'user',content:t}});
      var w=aiAdd('ai','…');document.getElementById('sendbtn').disabled=true;
      fetch('/assistant/chat',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{messages:HIST,slug:'{e(slug)}'}})}})
        .then(r=>r.json()).then(j=>{{
          w.remove();
          (j.actions||[]).forEach(a=>aiAdd('act','⚙ '+a));
          aiAdd('ai',j.reply||j.error||'(no reply)');
          if(j.reply)HIST.push({{role:'assistant',content:j.reply}});
          document.getElementById('sendbtn').disabled=false;
          if((j.actions||[]).length)setTimeout(()=>location.reload(),2500);
        }}).catch(e2=>{{w.textContent='Error: '+e2.message;document.getElementById('sendbtn').disabled=false}});
    }}
    </script>
    """
    return layout(c.get("companyName", "Client"), "clients", body)


# workspace actions
@router.post("/clients/{slug}/publish")
async def ws_publish(slug: str, request: Request, _: str = Depends(require_login)):
    b = await request.json()
    r = svc("PATCH", f"/service/clients/{slug}", {"published": bool(b.get("published"))})
    r["msg"] = "Published — the client links are live." if b.get("published") else "Unpublished — back to draft."
    return JSONResponse(r)


@router.post("/clients/{slug}/patchjs")
async def ws_patch(slug: str, request: Request, _: str = Depends(require_login)):
    b = await request.json()
    r = svc("PATCH", f"/service/clients/{slug}", b)
    r["msg"] = "Marked active — delivery begins." if b.get("active") else "Saved."
    return JSONResponse(r)


@router.post("/clients/{slug}/regen")
async def ws_regen(slug: str, request: Request, _: str = Depends(require_login)):
    r = svc("POST", f"/service/clients/{slug}/ai", {"force": True})
    if r.get("ok"):
        com = (r.get("aiSummary") or {}).get("commercial")
        inv = (r.get("client") or {}).get("investment", "")
        r["msg"] = (f"Regenerated from the call. Call pricing applied: {inv}." if com
                    else "Regenerated. No specific pricing found in the call — default terms kept.")
    return JSONResponse(r)


@router.post("/clients/{slug}/resend")
async def ws_resend(slug: str, request: Request, _: str = Depends(require_login)):
    r = svc("POST", f"/service/clients/{slug}/resend-executed")
    r["msg"] = "Executed agreement re-sent to both parties."
    return JSONResponse(r)


@router.post("/clients/{slug}/delete")
async def ws_delete(slug: str, _: str = Depends(require_login)):
    return JSONResponse(svc("DELETE", f"/service/clients/{slug}"))


# ============================================================
# CRM → STUDIO PROMOTION (manual, one click per opportunity)
# ============================================================

def _slugify(v):
    out = re.sub(r"[^a-z0-9]+", "-", str(v or "").lower()).strip("-")
    return out[:40]


@router.get("/promote")
def promote_opportunity(request: Request, opp_id: int = 0, _: str = Depends(require_login)):
    """Create a Studio client from a CRM opportunity. CRM stays the source of
    booked meetings; Studio begins here — manually, never automatically."""
    import db
    o = db.get_opportunity(opp_id)
    if not o:
        return HTMLResponse(layout("Promote", "clients", REV_CSS + _err(f"Opportunity {opp_id} not found.")))

    company = (o.company or o.deal_name or "").strip()
    if not company:
        return HTMLResponse(layout("Promote", "clients", REV_CSS + _err("This opportunity has no company name — add one in the CRM first.")))
    slug = _slugify(company)

    # already promoted? go straight to the workspace
    existing = svc("GET", f"/service/clients/{slug}")
    if existing.get("ok"):
        return RedirectResponse(f"/clients/{slug}?msg=Already in Studio — this opportunity was promoted earlier.", status_code=303)

    notes = []
    if getattr(o, "location", ""):
        notes.append(f"Location: {o.location}")
    if getattr(o, "contact_linkedin", ""):
        notes.append(f"Contact LinkedIn: {o.contact_linkedin}")
    if getattr(o, "company_linkedin", ""):
        notes.append(f"Company LinkedIn: {o.company_linkedin}")
    if getattr(o, "lead_intent", ""):
        notes.append(f"Lead intent: {o.lead_intent}")
    if getattr(o, "meeting_outcome", ""):
        notes.append(f"Meeting outcome: {o.meeting_outcome}")
    if getattr(o, "next_step", ""):
        notes.append(f"Agreed next step: {o.next_step}")
    if getattr(o, "close_date", ""):
        notes.append(f"Estimated close: {o.close_date}")
    if getattr(o, "source", ""):
        notes.append(f"Source: {o.source}")
    if getattr(o, "description", ""):
        notes.append(f"Notes: {o.description}")

    body = {
        "companyName": company,
        "clientName": (o.contact_name or "").strip() or company,
        "clientEmail": (o.email or "").strip(),
        "website": (o.website or "").strip(),
        "companyAddress": (getattr(o, "location", "") or "").strip(),
        "salesCallNotes": "\n".join(notes),
        "pricingNotes": (f"CRM deal value: ${int(o.value):,}" if getattr(o, "value", 0) else ""),
        "slug": slug,
    }
    if not body["clientEmail"]:
        return HTMLResponse(layout("Promote", "clients", REV_CSS + _err("This opportunity has no email — add one in the CRM first.")))

    r = svc("POST", "/service/clients", body)
    if not r.get("ok"):
        return HTMLResponse(layout("Promote", "clients", REV_CSS + _err(r.get("error", "Promotion failed."))))
    # promoted clients start as DRAFT — nothing goes live until you publish
    svc("PATCH", f"/service/clients/{r['slug']}", {"published": False})
    return RedirectResponse(
        f"/clients/{r['slug']}?msg=Promoted from CRM. Paste the call transcript below to generate the Blueprint %26 Agreement.",
        status_code=303)


@router.post("/clients/{slug}/transcript")
async def save_transcript(slug: str, request: Request, _: str = Depends(require_login)):
    """Save/replace the transcript, then generate blueprint personalization +
    agreement variables (uses the existing upsert + AI Service API endpoints)."""
    form = dict(await request.form())
    transcript = str(form.get("transcript", "")).strip()
    if len(transcript) < 50:
        return RedirectResponse(f"/clients/{slug}?msg=Transcript looks empty — paste the full call transcript.", status_code=303)

    data = svc("GET", f"/service/clients/{slug}")
    if not data.get("ok"):
        return HTMLResponse(layout("Error", "clients", REV_CSS + _err(data.get("error", "Not found"))))
    c = data["client"]
    it = c.get("intake") or {}

    body = {
        "slug": slug, "overwrite": True, "skipAI": True,
        "companyName": c.get("companyName"), "clientName": c.get("clientName"),
        "clientEmail": c.get("clientEmail"), "ascendlyEmail": c.get("ascendlyEmail"),
        "clientTitle": c.get("clientTitle"), "website": c.get("website"),
        "industry": c.get("industry"), "companyAddress": c.get("companyAddress"),
        "clientLogo": c.get("clientLogo"), "agreementId": c.get("agreementId"),
        "investment": c.get("investment"), "termMonths": c.get("termMonths"),
        "total": c.get("total"), "startDate": c.get("startDate"), "preparedDate": c.get("preparedDate"),
        "currentBottlenecks": c.get("mainBottleneck"), "recommendedFocus": c.get("recommendedFocus"),
        "transcript": transcript,
        "companyDetails": it.get("companyDetails", ""), "whatTheySell": it.get("whatTheySell", ""),
        "targetCustomers": it.get("targetCustomers", ""), "salesCallNotes": it.get("salesCallNotes", ""),
        "promisedScope": it.get("promisedScope", ""), "pricingNotes": it.get("pricingNotes", ""),
        "risks": it.get("risks", ""), "blueprintInstructions": it.get("blueprintInstructions", ""),
    }
    r = svc("POST", "/service/clients", body)
    if not r.get("ok"):
        return HTMLResponse(layout("Error", "clients", REV_CSS + _err(r.get("error", "Save failed"))))

    # generate now (synchronous) so the workspace comes back fully personalized
    ai = svc("POST", f"/service/clients/{slug}/ai", {"force": True})
    if ai.get("ok"):
        com = (ai.get("aiSummary") or {}).get("commercial")
        inv = (ai.get("client") or {}).get("investment", "")
        if com:
            msg = f"Blueprint %26 Agreement generated from the call. Call pricing applied: {inv}."
        else:
            msg = "Blueprint %26 Agreement generated. No specific pricing was found in the call — default terms kept (edit in the Agreement tab if needed)."
    else:
        msg = "Transcript saved. AI generation issue: " + ai.get("error", "unknown")[:120]
    return RedirectResponse(f"/clients/{slug}?msg={msg}", status_code=303)


# ============================================================
# STUDIO AI (embedded in workspaces; global page unlisted)
# ============================================================

AI_TOOLS = [
    {"type": "function", "function": {"name": "list_clients", "description": "List all clients with status and links.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_client", "description": "Get full details, links and signature state for one client.",
        "parameters": {"type": "object", "properties": {"slug": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "create_or_update_client",
        "description": "Create a client workspace (blueprint + agreement) or update one (overwrite=true). Transcript powers AI personalization.",
        "parameters": {"type": "object", "properties": {
            "companyName": {"type": "string"}, "clientName": {"type": "string"}, "clientEmail": {"type": "string"},
            "ascendlyEmail": {"type": "string"}, "clientTitle": {"type": "string"}, "industry": {"type": "string"},
            "website": {"type": "string"}, "investment": {"type": "string"}, "termMonths": {"type": "integer"},
            "total": {"type": "string"}, "startDate": {"type": "string"}, "transcript": {"type": "string"},
            "currentBottlenecks": {"type": "string"}, "recommendedFocus": {"type": "string"},
            "salesCallNotes": {"type": "string"}, "promisedScope": {"type": "string"}, "pricingNotes": {"type": "string"},
            "risks": {"type": "string"}, "blueprintInstructions": {"type": "string"},
            "slug": {"type": "string"}, "overwrite": {"type": "boolean"}},
            "required": ["companyName", "clientName", "clientEmail"]}}},
    {"type": "function", "function": {"name": "update_client_fields",
        "description": "Patch fields (pricing, emails, focus...) on an existing client. Omit slug to use the current workspace.",
        "parameters": {"type": "object", "properties": {
            "slug": {"type": "string"}, "investment": {"type": "string"}, "termMonths": {"type": "integer"},
            "total": {"type": "string"}, "startDate": {"type": "string"}, "industry": {"type": "string"},
            "mainBottleneck": {"type": "string"}, "recommendedFocus": {"type": "string"},
            "clientEmail": {"type": "string"}, "ascendlyEmail": {"type": "string"},
            "clientName": {"type": "string"}, "clientTitle": {"type": "string"}, "active": {"type": "boolean"}},
            "required": []}}},
    {"type": "function", "function": {"name": "set_published",
        "description": "Publish (client links live) or unpublish (draft). Omit slug for current workspace.",
        "parameters": {"type": "object", "properties": {"slug": {"type": "string"}, "published": {"type": "boolean"}},
            "required": ["published"]}}},
    {"type": "function", "function": {"name": "regenerate_ai",
        "description": "Re-run AI personalization from the stored transcript. force=true overwrites existing values.",
        "parameters": {"type": "object", "properties": {"slug": {"type": "string"}, "force": {"type": "boolean"},
            "instructions": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "resend_executed_email",
        "description": "Re-send the executed agreement PDF to both parties (post-execution only).",
        "parameters": {"type": "object", "properties": {"slug": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "delete_client",
        "description": "Permanently delete a client. ONLY after explicit user confirmation in this conversation.",
        "parameters": {"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]}}},
]

AI_SYSTEM = """You are Studio AI inside Ascendly Studio, talking to Rosis (Ascendly's founder).
You orchestrate client Revenue Blueprints and Agreements via tools that call the portals service
(blueprint.ascendly.one / agreement.ascendly.one). Clients never see Studio.

Behavior:
- Brief and operational. Confirm what changed, give the relevant link.
- If a CURRENT WORKSPACE is provided, all tool calls default to that client — don't ask for the slug.
- "Build a blueprint for X": create/update the client, then share preview + public links.
- New clients start published; unpublish for draft work.
- Pricing default: $4,000/month · 3 months · $12,000. Change only when asked.
- Destructive actions (delete): ask for confirmation first.
- Signing/countersigning happen on the agreement pages — share links when relevant.
"""


def _ai_dispatch(name, args, current_slug=""):
    slug = args.get("slug") or current_slug
    if name == "list_clients":
        r = svc("GET", "/service/clients")
        if r.get("ok"):
            return {"clients": [{k: c.get(k) for k in ("slug", "companyName", "clientEmail", "status", "agreementId")} for c in r["clients"]]}
        return r
    if name == "get_client":
        return svc("GET", f"/service/clients/{slug}")
    if name == "create_or_update_client":
        return svc("POST", "/service/clients", args)
    if name == "update_client_fields":
        body = {k: v for k, v in args.items() if k != "slug"}
        return svc("PATCH", f"/service/clients/{slug}", body)
    if name == "set_published":
        return svc("PATCH", f"/service/clients/{slug}", {"published": bool(args.get("published"))})
    if name == "regenerate_ai":
        return svc("POST", f"/service/clients/{slug}/ai", {"force": bool(args.get("force")), "instructions": args.get("instructions", "")})
    if name == "resend_executed_email":
        return svc("POST", f"/service/clients/{slug}/resend-executed")
    if name == "delete_client":
        return svc("DELETE", f"/service/clients/{slug}")
    return {"ok": False, "error": f"unknown tool {name}"}


@router.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request, _: str = Depends(require_login)):
    body = f"""{REV_CSS}
    <div class="rv-top"><h1>Studio AI</h1>
      <span class="muted" style="font-size:12.5px">Cross-client assistant — inside a client workspace it already knows the context.</span>
    </div>
    <div class="rv-ai" style="position:static;height:calc(100vh - 190px)">
      <div class="rv-ai-h"><span class="dot2"></span>Studio AI</div>
      <div class="rv-msgs" id="msgs">
        <div class="rv-msg ai">Ask me anything across clients: "who hasn't signed yet?", "build a blueprint for Northstar" (paste the transcript), "list all executed agreements".</div>
      </div>
      <div class="rv-inp">
        <input id="chatin" placeholder="Type an instruction…" onkeydown="if(event.key==='Enter')aiSend()">
        <button class="btn sm" id="sendbtn" onclick="aiSend()">Send</button>
      </div>
    </div>
    <script>
    var HIST=[];
    function aiAdd(cls,text){{var d=document.createElement('div');d.className='rv-msg '+cls;d.textContent=text;
      document.getElementById('msgs').appendChild(d);d.scrollIntoView({{behavior:'smooth'}});return d}}
    function aiSend(){{
      var inp=document.getElementById('chatin'),t=inp.value.trim();if(!t)return;
      inp.value='';aiAdd('user',t);HIST.push({{role:'user',content:t}});
      var w=aiAdd('ai','…');document.getElementById('sendbtn').disabled=true;
      fetch('/assistant/chat',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{messages:HIST}})}})
        .then(r=>r.json()).then(j=>{{
          w.remove();(j.actions||[]).forEach(a=>aiAdd('act','⚙ '+a));
          aiAdd('ai',j.reply||j.error||'(no reply)');
          if(j.reply)HIST.push({{role:'assistant',content:j.reply}});
          document.getElementById('sendbtn').disabled=false;
        }}).catch(e2=>{{w.textContent='Error: '+e2.message;document.getElementById('sendbtn').disabled=false}});
    }}
    </script>
    """
    return layout("AI Assistant", "rev_ai", body)


@router.post("/assistant/chat")
async def assistant_chat(request: Request, _: str = Depends(require_login)):
    if not os.getenv("OPENAI_API_KEY"):
        return JSONResponse({"reply": "", "error": "OPENAI_API_KEY is not set on Studio."})
    try:
        from openai import OpenAI
    except Exception:
        return JSONResponse({"reply": "", "error": "openai package not installed."})

    body = await request.json()
    current_slug = str(body.get("slug") or "")
    history = [m for m in (body.get("messages") or []) if m.get("role") in ("user", "assistant")][-20:]
    messages = [{"role": "system", "content": AI_SYSTEM}]

    if current_slug:
        cd = svc("GET", f"/service/clients/{current_slug}")
        if cd.get("ok"):
            c = cd["client"]
            ctx = {k: c.get(k) for k in ("slug", "companyName", "clientName", "clientEmail", "status",
                                          "agreementId", "investment", "termMonths", "total",
                                          "mainBottleneck", "recommendedFocus", "published")}
            ctx["links"] = c.get("links", {})
            ctx["signature"] = c.get("signature", {})
            messages.append({"role": "system", "content": "CURRENT WORKSPACE: " + json.dumps(ctx)})
    messages += history

    oc = OpenAI()
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    actions = []
    try:
        for _round in range(6):
            resp = oc.chat.completions.create(model=model, messages=messages, tools=AI_TOOLS, temperature=0.3)
            m = resp.choices[0].message
            if not m.tool_calls:
                return JSONResponse({"reply": m.content or "", "actions": actions})
            messages.append({"role": "assistant", "content": m.content or "",
                             "tool_calls": [tc.model_dump() for tc in m.tool_calls]})
            for tc in m.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                result = _ai_dispatch(tc.function.name, args, current_slug)
                actions.append(f"{tc.function.name}({args.get('slug') or current_slug or args.get('companyName') or ''}) → {'ok' if result.get('ok') else result.get('error', 'failed')}")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)[:8000]})
        return JSONResponse({"reply": "Hit the action limit for one message — say continue.", "actions": actions})
    except Exception as ex:
        return JSONResponse({"reply": "", "error": f"Studio AI error: {ex}", "actions": actions})


# ============================================================
# TEMPORARY connectivity test — remove once verified
# ============================================================

@router.get("/api/revenue/test")
def connectivity_test(_: str = Depends(require_login)):
    """Verifies Studio ↔ Portals Service API connectivity. TEMPORARY."""
    if not PORTALS_KEY:
        return JSONResponse({"ok": False, "portalsUrl": PORTALS_URL,
                             "error": "PORTALS_API_KEY env var is not set on Studio."}, status_code=503)
    try:
        r = requests.get(PORTALS_URL + "/service/clients",
                         headers={"X-Service-Key": PORTALS_KEY}, timeout=20)
    except Exception as ex:
        return JSONResponse({"ok": False, "portalsUrl": PORTALS_URL,
                             "error": f"Request failed: {ex}"}, status_code=502)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:500]}
    auth_ok = r.status_code == 200 and data.get("ok") is True
    clients = data.get("clients", []) if auth_ok else []
    return JSONResponse({
        "ok": auth_ok,
        "portalsUrl": PORTALS_URL,
        "httpStatus": r.status_code,
        "authenticated": auth_ok,
        "clientCount": len(clients),
        "firstClientSlug": clients[0]["slug"] if clients else None,
        "errorBody": None if auth_ok else data,
    })


# ============================================================
# Legacy redirects (old /revenue/* URLs keep working)
# ============================================================

@router.get("/revenue")
def _r1(view: str = ""):
    return RedirectResponse("/clients")


@router.get("/revenue/new")
def _r2():
    return RedirectResponse("/clients/new")


@router.get("/revenue/client/{slug}")
def _r3(slug: str):
    return RedirectResponse(f"/clients/{slug}")


@router.get("/revenue/assistant")
def _r4():
    return RedirectResponse("/assistant")
