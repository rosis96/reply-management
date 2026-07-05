#!/usr/bin/env python3
"""
CRM section — a separate mini-HubSpot inside Reply Manager.

Kanban pipeline of opportunities with customizable stages + tags, deal values,
and a card detail panel. Completely separate from the reply engine: its own
tables (crm_stages, crm_tags, opportunities) and its own /crm routes.
"""

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import db
from dashboard import layout, require_login, e

router = APIRouter()


# -------------------------
# helpers
# -------------------------

def _money(v):
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        v = 0
    return "$" + format(int(round(v)), ",")


def _tag_map():
    return {t.id: t for t in db.list_tags()}


def _opp_tag_ids(opp):
    try:
        ids = json.loads(opp.tag_ids or "[]")
        return [int(i) for i in ids]
    except Exception:
        return []


def _tag_pills(opp, tagmap):
    out = ""
    for tid in _opp_tag_ids(opp):
        t = tagmap.get(tid)
        if t:
            out += (f'<span class="opp-tag" style="background:{e(t.color)}22;color:{e(t.color)};'
                    f'border:1px solid {e(t.color)}55">{e(t.name)}</span>')
    return out


CRM_CSS = """
<style>
.crm-top { display:flex; align-items:center; gap:12px; margin-bottom:16px; flex-wrap:wrap; }
.crm-top h1 { margin:0; }
.crm-board { display:flex; gap:14px; overflow-x:auto; padding-bottom:16px; align-items:flex-start; }
.opp-col { flex:0 0 288px; background:#f3f4f8; border:1px solid var(--line); border-radius:14px; padding:10px; }
.opp-colhead { display:flex; align-items:center; gap:8px; padding:4px 6px 10px; font-weight:700; font-size:13px; }
.opp-colhead .dot { width:10px; height:10px; border-radius:50%; flex:0 0 10px; }
.opp-colhead .ct { color:var(--muted); font-weight:600; font-size:12px; margin-left:auto; }
.opp-colhead .addc { border:none; background:transparent; color:var(--muted); cursor:pointer; font-size:18px; line-height:1; padding:0 2px; }
.opp-colhead .addc:hover { color:var(--primary); }
.opp-cards { display:flex; flex-direction:column; gap:9px; min-height:24px; }
.opp-card { background:#fff; border:1px solid var(--line); border-radius:11px; padding:11px 12px; cursor:pointer;
            box-shadow:0 1px 2px rgba(25,28,29,.05); }
.opp-card:hover { border-color:var(--primary); }
.opp-co { font-weight:700; font-size:13.5px; color:var(--txt); }
.opp-sub { font-size:11.5px; color:var(--muted); margin-top:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.opp-tags { display:flex; flex-wrap:wrap; gap:4px; margin-top:8px; }
.opp-tag { font-size:10px; font-weight:700; border-radius:6px; padding:1px 6px; }
.opp-foot { display:flex; align-items:center; justify-content:space-between; margin-top:9px; }
.opp-val { font-weight:800; font-size:13px; color:var(--txt); }
.opp-intent { font-size:10px; color:var(--muted); background:var(--surf); border-radius:6px; padding:1px 7px; }
.opp-empty { color:#aab; font-size:12px; text-align:center; padding:14px 0; }
</style>
"""

CRM_JS = """
<script>
var _dragId=null;
function cardDrag(ev,id){ _dragId=String(id); try{ev.dataTransfer.setData('text/plain',String(id));}catch(e){} }
function colOver(ev){ ev.preventDefault(); }
function colDrop(ev,stageId){
  ev.preventDefault();
  var id=_dragId||ev.dataTransfer.getData('text/plain'); _dragId=null;
  if(!id) return;
  var fd=new FormData(); fd.append('stage_id',stageId);
  fetch('/crm/opp/'+id+'/move',{method:'POST',body:fd}).then(function(){ location.reload(); });
}
function openForm(id,stageId){
  var url='/crm/opp-form?opp_id='+(id||'')+'&stage_id='+(stageId||'');
  fetch(url).then(function(r){return r.text();}).then(function(h){
    document.getElementById('crm-panel-body').innerHTML=h;
    document.getElementById('crm-overlay').classList.add('show');
    document.getElementById('crm-drawer').classList.add('show');
  });
}
function openOpp(id){ openForm(id,''); }
function newOpp(sid){ openForm('',sid); }
function closeOpp(){
  document.getElementById('crm-overlay').classList.remove('show');
  document.getElementById('crm-drawer').classList.remove('show');
}
function saveOpp(oppId){
  var form=document.getElementById('opp-form');
  var fd=new FormData(form);
  fetch('/crm/opp-save?opp_id='+(oppId||''),{method:'POST',body:fd})
    .then(function(r){return r.json();})
    .then(function(d){ if(d&&d.ok){ location.reload(); } else { alert('Save failed'); } });
}
function deleteOpp(id){
  if(!confirm('Delete this opportunity?')) return;
  fetch('/crm/opp/'+id+'/delete',{method:'POST'}).then(function(){ location.reload(); });
}
document.addEventListener('keydown',function(ev){ if(ev.key==='Escape') closeOpp(); });
</script>
"""


def _crm_drawer():
    return """
    <div class="overlay" id="crm-overlay" onclick="closeOpp()"></div>
    <div class="drawer" id="crm-drawer">
      <div class="dhead"><b style="flex:1">Opportunity</b>
        <button class="btn sec sm" onclick="closeOpp()">Close</button></div>
      <div class="dbody" id="crm-panel-body"></div>
    </div>"""


def _client_filter(current):
    try:
        wss = db.list_workspaces()
    except Exception:
        wss = []
    opts = f'<option value="">All clients</option>'
    for w in wss:
        sel = " selected" if w.name == current else ""
        opts += f'<option value="{e(w.name)}"{sel}>{e(w.name)}</option>'
    return (f'<select onchange="location=\'/crm?workspace=\'+encodeURIComponent(this.value)" '
            f'style="min-width:180px">{opts}</select>')


# -------------------------
# Board
# -------------------------

@router.get("/crm", response_class=HTMLResponse)
def crm_board(request: Request, workspace: str = "", _: str = Depends(require_login)):
    db.seed_crm_if_empty()
    stages = db.list_stages()
    opps = db.list_opportunities(workspace=workspace or None)
    totals = db.opportunity_stage_totals(workspace=workspace or None)
    tagmap = _tag_map()

    by_stage = {}
    for o in opps:
        by_stage.setdefault(o.stage_id, []).append(o)

    cols = ""
    for st in stages:
        cards = ""
        for o in by_stage.get(st.id, []):
            intent = f'<span class="opp-intent">{e(o.lead_intent)}</span>' if o.lead_intent else ""
            cards += f"""
            <div class="opp-card" draggable="true" ondragstart="cardDrag(event,{o.id})" onclick="openOpp({o.id})">
              <div class="opp-co">{e(o.company or o.deal_name or o.contact_name or "Untitled")}</div>
              <div class="opp-sub">{e(o.contact_name or "")}{" · " + e(o.email) if o.email else ""}</div>
              <div class="opp-tags">{_tag_pills(o, tagmap)}</div>
              <div class="opp-foot"><span class="opp-val">{_money(o.value)}</span>{intent}</div>
            </div>"""
        if not cards:
            cards = '<div class="opp-empty">Drop cards here</div>'
        t = totals.get(st.id, {"count": 0, "value": 0})
        cols += f"""
        <div class="opp-col" ondragover="colOver(event)" ondrop="colDrop(event,{st.id})">
          <div class="opp-colhead">
            <span class="dot" style="background:{e(st.color)}"></span>{e(st.name)}
            <span class="ct">{t['count']} · {_money(t['value'])}</span>
            <button class="addc" title="Add here" onclick="newOpp({st.id})">+</button>
          </div>
          <div class="opp-cards">{cards}</div>
        </div>"""

    grand = sum(t["value"] for t in totals.values())
    body = f"""
    {CRM_CSS}
    <div class="crm-top">
      <h1>Pipeline</h1>
      <span class="muted small">{len(opps)} opportunities · {_money(grand)} total</span>
      <div style="margin-left:auto; display:flex; gap:10px; align-items:center">
        {_client_filter(workspace)}
        <button class="btn" onclick="newOpp('')">+ Add opportunity</button>
      </div>
    </div>
    <div class="crm-board">{cols}</div>
    {_crm_drawer()}
    {CRM_JS}
    """
    return HTMLResponse(layout("CRM Pipeline", "", body,
                               current_ws=request.cookies.get("ws", ""), crm_active="board"))


# -------------------------
# List view
# -------------------------

@router.get("/crm/list", response_class=HTMLResponse)
def crm_list(request: Request, workspace: str = "", q: str = "", _: str = Depends(require_login)):
    db.seed_crm_if_empty()
    stages = {s.id: s for s in db.list_stages()}
    opps = db.list_opportunities(workspace=workspace or None, search=q or None)
    tagmap = _tag_map()
    rows = ""
    for o in opps:
        st = stages.get(o.stage_id)
        stage_pill = (f'<span class="opp-tag" style="background:{e(st.color)}22;color:{e(st.color)}">'
                      f'{e(st.name)}</span>') if st else "—"
        rows += f"""<tr onclick="openOpp({o.id})" style="cursor:pointer">
          <td><b>{e(o.company or o.deal_name or "Untitled")}</b><div class="muted small">{e(o.contact_name or "")}</div></td>
          <td>{stage_pill}</td>
          <td>{_money(o.value)}</td>
          <td>{e(o.email or "")}</td>
          <td>{e(o.workspace_name or "")}</td>
          <td>{_tag_pills(o, tagmap) or "—"}</td>
        </tr>"""
    if not rows:
        rows = '<tr><td colspan="6" class="empty">No opportunities yet.</td></tr>'
    body = f"""
    {CRM_CSS}
    <div class="crm-top">
      <h1>Opportunities</h1>
      <div style="margin-left:auto; display:flex; gap:10px; align-items:center">
        <form method="get" action="/crm/list"><input type="text" name="q" value="{e(q)}" placeholder="Search company, contact, email"></form>
        {_client_filter(workspace)}
        <button class="btn" onclick="newOpp('')">+ Add</button>
      </div>
    </div>
    <div class="tablewrap"><table>
      <thead><tr><th>Company</th><th>Stage</th><th>Value</th><th>Email</th><th>Client</th><th>Tags</th></tr></thead>
      <tbody>{rows}</tbody></table></div>
    {_crm_drawer()}
    {CRM_JS}
    """
    return HTMLResponse(layout("CRM Opportunities", "", body,
                               current_ws=request.cookies.get("ws", ""), crm_active="list"))


# -------------------------
# Opportunity form (panel) + save
# -------------------------

@router.get("/crm/opp-form", response_class=HTMLResponse)
def opp_form(opp_id: str = "", stage_id: str = "", _: str = Depends(require_login)):
    stages = db.list_stages()
    tags = db.list_tags()
    opp = db.get_opportunity(opp_id) if opp_id else None

    def val(attr, d=""):
        return getattr(opp, attr) if opp else d

    cur_stage = (opp.stage_id if opp else None) or (int(stage_id) if stage_id else (stages[0].id if stages else None))
    stage_opts = ""
    for s in stages:
        sel = " selected" if s.id == cur_stage else ""
        stage_opts += f'<option value="{s.id}"{sel}>{e(s.name)}</option>'

    try:
        wss = db.list_workspaces()
    except Exception:
        wss = []
    ws_opts = '<option value="">—</option>'
    for w in wss:
        sel = " selected" if opp and w.name == opp.workspace_name else ""
        ws_opts += f'<option value="{e(w.name)}"{sel}>{e(w.name)}</option>'

    cur_tags = set(_opp_tag_ids(opp)) if opp else set()
    tag_boxes = ""
    for t in tags:
        chk = "checked" if t.id in cur_tags else ""
        tag_boxes += (f'<label class="flex" style="gap:6px;margin:0 10px 6px 0;display:inline-flex">'
                      f'<input type="checkbox" name="tags" value="{t.id}" {chk}>'
                      f'<span class="opp-tag" style="background:{e(t.color)}22;color:{e(t.color)}">{e(t.name)}</span></label>')
    if not tags:
        tag_boxes = '<span class="muted small">No tags yet — add them in Stages & tags.</span>'

    val_num = ("" if not opp or not opp.value else int(opp.value) if float(opp.value).is_integer() else opp.value)
    del_btn = (f'<button type="button" class="btn danger sm" onclick="deleteOpp({opp.id})">Delete</button>'
               if opp else "")
    title = "Edit opportunity" if opp else "New opportunity"
    return HTMLResponse(f"""
    <form id="opp-form" onsubmit="return false">
      <h3 style="margin-top:0">{title}</h3>
      <div class="row2">
        <div><label>Company</label><input type="text" name="company" value="{e(val('company'))}" style="width:100%"></div>
        <div><label>Deal name</label><input type="text" name="deal_name" value="{e(val('deal_name'))}" style="width:100%"></div>
      </div>
      <div class="row2">
        <div><label>Contact name</label><input type="text" name="contact_name" value="{e(val('contact_name'))}" style="width:100%"></div>
        <div><label>Email</label><input type="text" name="email" value="{e(val('email'))}" style="width:100%"></div>
      </div>
      <div class="row2">
        <div><label>Deal value ($)</label><input type="number" name="value" value="{e(val_num)}" style="width:100%"></div>
        <div><label>Stage</label><select name="stage_id" style="width:100%">{stage_opts}</select></div>
      </div>
      <div class="row2">
        <div><label>Client / workspace</label><select name="workspace_name" style="width:100%">{ws_opts}</select></div>
        <div><label>Status</label><input type="text" name="status" value="{e(val('status'))}" placeholder="e.g. proposal sent" style="width:100%"></div>
      </div>
      <div class="row2">
        <div><label>Owner</label><input type="text" name="owner" value="{e(val('owner'))}" style="width:100%"></div>
        <div><label>Next action date</label><input type="date" name="next_action_date" value="{e(val('next_action_date'))}" style="width:100%"></div>
      </div>
      <label>Website</label><input type="text" name="website" value="{e(val('website'))}" style="width:100%">
      <label>Lead intent</label><input type="text" name="lead_intent" value="{e(val('lead_intent'))}" style="width:100%">
      <label>Tags</label><div style="margin-bottom:6px">{tag_boxes}</div>
      <label>Description / notes</label><textarea name="description" style="width:100%;min-height:100px">{e(val('description'))}</textarea>
      <div class="flex" style="margin-top:14px">
        <button type="button" class="btn" onclick="saveOpp('{opp.id if opp else ''}')">Save</button>
        {del_btn}
      </div>
    </form>
    """)


@router.post("/crm/opp-save")
async def opp_save(request: Request, opp_id: str = "", _: str = Depends(require_login)):
    form = await request.form()
    tag_ids = form.getlist("tags")
    data = {
        "company": form.get("company", ""), "deal_name": form.get("deal_name", ""),
        "contact_name": form.get("contact_name", ""), "email": form.get("email", ""),
        "website": form.get("website", ""), "value": form.get("value", "0"),
        "stage_id": form.get("stage_id", ""), "workspace_name": form.get("workspace_name", ""),
        "status": form.get("status", ""), "owner": form.get("owner", ""),
        "next_action_date": form.get("next_action_date", ""),
        "lead_intent": form.get("lead_intent", ""), "description": form.get("description", ""),
        "tag_ids": json.dumps([int(t) for t in tag_ids if str(t).isdigit()]),
    }
    new_id = db.save_opportunity(opp_id or None, data)
    return JSONResponse({"ok": True, "id": new_id})


@router.post("/crm/opp/{opp_id}/move")
async def opp_move(opp_id: int, request: Request, _: str = Depends(require_login)):
    form = await request.form()
    db.move_opportunity(opp_id, form.get("stage_id"))
    return JSONResponse({"ok": True})


@router.post("/crm/opp/{opp_id}/delete")
def opp_delete(opp_id: int, _: str = Depends(require_login)):
    db.delete_opportunity(opp_id)
    return JSONResponse({"ok": True})


# -------------------------
# Stages & tags management
# -------------------------

@router.get("/crm/stages", response_class=HTMLResponse)
def stages_page(request: Request, _: str = Depends(require_login)):
    db.seed_crm_if_empty()
    stages = db.list_stages()
    tags = db.list_tags()
    srows = ""
    for s in stages:
        srows += f"""<form method="post" action="/crm/stages/save" class="flex" style="gap:8px;margin-bottom:8px;align-items:center">
          <input type="hidden" name="id" value="{s.id}">
          <input type="color" name="color" value="{e(s.color)}" style="width:44px;height:38px;padding:2px">
          <input type="text" name="name" value="{e(s.name)}" style="flex:1">
          <input type="number" name="sort_order" value="{s.sort_order}" title="Order" style="width:74px">
          <button class="btn sm" type="submit">Save</button>
          <button class="btn sec sm" formaction="/crm/stages/delete" type="submit" onclick="return confirm('Delete this stage? Cards move to the first stage.')">Delete</button>
        </form>"""
    trows = ""
    for t in tags:
        trows += f"""<form method="post" action="/crm/tags/save" class="flex" style="gap:8px;margin-bottom:8px;align-items:center">
          <input type="hidden" name="id" value="{t.id}">
          <input type="color" name="color" value="{e(t.color)}" style="width:44px;height:38px;padding:2px">
          <input type="text" name="name" value="{e(t.name)}" style="flex:1">
          <button class="btn sm" type="submit">Save</button>
          <button class="btn sec sm" formaction="/crm/tags/delete" type="submit" onclick="return confirm('Delete this tag?')">Delete</button>
        </form>"""
    body = f"""
    <h1>Stages &amp; tags</h1>
    <p class="sub">Customize your pipeline columns and the tags you can put on opportunities.</p>
    <div class="card">
      <h3>Pipeline stages</h3>
      {srows or '<p class="muted small">No stages.</p>'}
      <form method="post" action="/crm/stages/save" class="flex" style="gap:8px;margin-top:12px;align-items:center">
        <input type="color" name="color" value="#7857f8" style="width:44px;height:38px;padding:2px">
        <input type="text" name="name" placeholder="New stage name" style="flex:1">
        <button class="btn sm" type="submit">+ Add stage</button>
      </form>
    </div>
    <div class="card">
      <h3>Tags</h3>
      {trows or '<p class="muted small">No tags yet.</p>'}
      <form method="post" action="/crm/tags/save" class="flex" style="gap:8px;margin-top:12px;align-items:center">
        <input type="color" name="color" value="#137333" style="width:44px;height:38px;padding:2px">
        <input type="text" name="name" placeholder="New tag name" style="flex:1">
        <button class="btn sm" type="submit">+ Add tag</button>
      </form>
    </div>
    """
    return HTMLResponse(layout("CRM Stages", "", body,
                               current_ws=request.cookies.get("ws", ""), crm_active="stages"))


@router.post("/crm/stages/save")
async def stages_save(request: Request, _: str = Depends(require_login)):
    form = await request.form()
    db.save_stage(form.get("id") or None, {
        "name": form.get("name", ""), "color": form.get("color", "#64748b"),
        "sort_order": form.get("sort_order", ""),
    })
    return RedirectResponse("/crm/stages", status_code=303)


@router.post("/crm/stages/delete")
async def stages_delete(request: Request, _: str = Depends(require_login)):
    form = await request.form()
    if form.get("id"):
        db.delete_stage(form.get("id"))
    return RedirectResponse("/crm/stages", status_code=303)


@router.post("/crm/tags/save")
async def tags_save(request: Request, _: str = Depends(require_login)):
    form = await request.form()
    db.save_tag(form.get("id") or None, {
        "name": form.get("name", ""), "color": form.get("color", "#7857f8"),
    })
    return RedirectResponse("/crm/stages", status_code=303)


@router.post("/crm/tags/delete")
async def tags_delete(request: Request, _: str = Depends(require_login)):
    form = await request.form()
    if form.get("id"):
        db.delete_tag(form.get("id"))
    return RedirectResponse("/crm/stages", status_code=303)
