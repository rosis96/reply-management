#!/usr/bin/env python3
"""
Database layer for the reply management system.

Stores everything that used to live in scattered files / env vars:
  - workspaces (API keys, base URLs, campaign IDs, client profiles, reply formats)
  - app settings (OpenAI key, human-review webhook, reply delay)
  - leads (one row per processed reply, with Reply / FUP status)

Uses Postgres in production (Railway sets DATABASE_URL) and falls back to a
local SQLite file when DATABASE_URL is not set, so the dashboard can be
previewed locally before deploying.
"""

import os
import json
from datetime import datetime

from sqlalchemy import (
    create_engine,
    inspect,
    text,
    or_,
    Column,
    Integer,
    Float,
    String,
    Text,
    Boolean,
    DateTime,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker


# -------------------------
# Engine / session
# -------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Railway sometimes hands out the legacy "postgres://" scheme which
# SQLAlchemy 2.0 no longer accepts. Normalize it.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if not DATABASE_URL:
    # Local preview fallback.
    DATABASE_URL = "sqlite:///reply_management.db"

_is_sqlite = DATABASE_URL.startswith("sqlite")
IS_SQLITE = _is_sqlite

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# -------------------------
# Models
# -------------------------

class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    platform = Column(String(50), default="bison")          # "bison" or "instantly"
    mode = Column(String(20), default="reply")              # "reply" or "followup" (temporary)
    active = Column(Boolean, default=True)

    api_key = Column(Text, default="")
    base_url = Column(Text, default="")                      # Bison base URL
    reply_followup_campaign_id = Column(String(255), default="")

    website = Column(Text, default="")
    sender_name = Column(Text, default="")
    default_sender_email = Column(Text, default="")
    calendly_token = Column(Text, default="")              # Calendly Personal Access Token
    calendly_scheduling_url = Column(Text, default="")     # client's public Calendly link

    ai_provider = Column(String(20), default="openai")     # "openai" or "gemini" (primary, tried first)
    ai_fallback = Column(Boolean, default=False)           # if primary fails, try the other provider
    openai_key = Column(Text, default="")                  # per-workspace override (else global setting)
    gemini_key = Column(Text, default="")                  # per-workspace override (else global setting)

    client_profile = Column(JSON, default=dict)
    reply_format = Column(JSON, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Setting(Base):
    __tablename__ = "app_settings"

    key = Column(String(255), primary_key=True)
    value = Column(Text, default="")


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_leads_dedupe_key"),)

    id = Column(Integer, primary_key=True)
    dedupe_key = Column(String(512), index=True)

    workspace_name = Column(String(255), default="")
    platform = Column(String(50), default="")
    external_lead_id = Column(String(255), default="")
    reply_id = Column(String(255), default="")

    name = Column(String(255), default="")
    email = Column(String(255), default="")

    interested = Column(Boolean, default=None)
    intent = Column(String(255), default="")
    confidence = Column(String(50), default="")
    conf_num = Column(Float, default=0.0)                   # numeric confidence for filtering
    action = Column(String(50), default="")                 # send / skip_enrich / stop / error

    reply_added = Column(Boolean, default=False)            # a main_reply was generated
    replied = Column(Boolean, default=False)               # reply was actually sent
    fup_added = Column(Boolean, default=False)             # follow-up variables written
    reviewed = Column(Boolean, default=False)              # a human has reviewed it
    stage = Column(String(40), default="")                # workflow stage (new/replied/booked/won/lost/stopped)
    owner = Column(String(255), default="")               # assigned owner

    company = Column(String(255), default="")
    campaign = Column(String(255), default="")
    subject = Column(String(512), default="")
    reply_text = Column(Text, default="")                  # the prospect's incoming reply
    main_reply = Column(Text, default="")                  # our AI reply
    followups = Column(Text, default="")                   # JSON list of follow-up drafts
    thread = Column(Text, default="")                      # JSON list of thread messages
    lead_data = Column(Text, default="")                   # JSON of all lead fields from the platform
    send_meta = Column(Text, default="")                   # JSON: what's needed to re-send (ids/eaccount/etc.)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProposedSlot(Base):
    """One row per real Calendly time we've already proposed to a prospect, so
    we never pitch the same upcoming slot to two different prospects."""
    __tablename__ = "proposed_slots"

    id = Column(Integer, primary_key=True)
    workspace_name = Column(String(255), default="", index=True)
    prospect = Column(String(255), default="")             # email or lead id
    slot_utc = Column(String(40), default="", index=True)  # ISO 8601 UTC, the unique key
    label = Column(Text, default="")                       # human label we showed
    created_at = Column(DateTime, default=datetime.utcnow)


# -------------------------
# CRM models (separate section — does not touch the reply pipeline)
# -------------------------

class CrmStage(Base):
    __tablename__ = "crm_stages"
    id = Column(Integer, primary_key=True)
    name = Column(String(80), default="")
    color = Column(String(20), default="#64748b")
    sort_order = Column(Integer, default=0)
    is_won = Column(Boolean, default=False)
    is_lost = Column(Boolean, default=False)


class CrmTag(Base):
    __tablename__ = "crm_tags"
    id = Column(Integer, primary_key=True)
    name = Column(String(80), default="")
    color = Column(String(20), default="#7857f8")


class Opportunity(Base):
    __tablename__ = "opportunities"
    id = Column(Integer, primary_key=True)
    workspace_name = Column(String(255), default="")
    lead_id = Column(String(255), default="")          # external lead id (Bison/Instantly)
    deal_name = Column(String(255), default="")
    contact_name = Column(String(255), default="")
    email = Column(String(255), default="")
    company = Column(String(255), default="")
    website = Column(String(512), default="")
    stage_id = Column(Integer, default=None)
    lead_intent = Column(String(120), default="")
    status = Column(String(120), default="")
    value = Column(Float, default=0.0)
    owner = Column(String(255), default="")
    description = Column(Text, default="")
    next_action_date = Column(String(40), default="")
    tag_ids = Column(Text, default="")                 # JSON list of CrmTag ids
    # post-meeting flow fields
    close_date = Column(String(40), default="")        # estimated close date
    source = Column(String(120), default="")           # original source
    next_step = Column(Text, default="")               # the agreed next action
    meeting_outcome = Column(String(60), default="")   # outcome of the meeting
    location = Column(String(255), default="")         # from platform lead data
    contact_linkedin = Column(String(512), default="") # personal LinkedIn URL
    company_linkedin = Column(String(512), default="") # company LinkedIn URL
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    stage_changed_at = Column(DateTime, default=datetime.utcnow)


DEFAULT_CRM_STAGES = [
    ("Opportunity", "#64748b", False, False),
    ("Meeting Booked", "#2563eb", False, False),
    ("Meeting Completed", "#7857f8", False, False),
    ("No Show", "#d97706", False, False),
    ("Follow-up", "#ea580c", False, False),
    ("Won", "#137333", True, False),
    ("Lost", "#ba1a1a", False, True),
]


# -------------------------
# Init + seed
# -------------------------

def init_db():
    Base.metadata.create_all(bind=engine)


def migrate():
    """Add any columns that don't exist yet on an already-created table.
    Safe to run on every startup (works for both Postgres and SQLite)."""
    # workspaces table additions
    try:
        ws_cols = {c["name"] for c in inspect(engine).get_columns("workspaces")}
        ws_adds = {
            "mode": "VARCHAR(20)",
            "calendly_token": "TEXT",
            "calendly_scheduling_url": "TEXT",
            "ai_provider": "VARCHAR(20)",
            "ai_fallback": "BOOLEAN",
            "openai_key": "TEXT",
            "gemini_key": "TEXT",
        }
        with engine.begin() as conn:
            for col, coltype in ws_adds.items():
                if col not in ws_cols:
                    try:
                        conn.execute(text(f"ALTER TABLE workspaces ADD COLUMN {col} {coltype}"))
                    except Exception:
                        pass
    except Exception:
        pass

    # opportunities table additions (CRM post-meeting fields)
    try:
        if inspect(engine).has_table("opportunities"):
            opp_cols = {c["name"] for c in inspect(engine).get_columns("opportunities")}
            opp_adds = {"close_date": "VARCHAR(40)", "source": "VARCHAR(120)",
                        "next_step": "TEXT", "meeting_outcome": "VARCHAR(60)",
                        "location": "VARCHAR(255)", "contact_linkedin": "VARCHAR(512)",
                        "company_linkedin": "VARCHAR(512)"}
            with engine.begin() as conn:
                for col, coltype in opp_adds.items():
                    if col not in opp_cols:
                        try:
                            conn.execute(text(f"ALTER TABLE opportunities ADD COLUMN {col} {coltype}"))
                        except Exception:
                            pass
    except Exception:
        pass

    try:
        insp = inspect(engine)
        existing = {c["name"] for c in insp.get_columns("leads")}
    except Exception:
        return

    new_columns = {
        "campaign": "VARCHAR(255)",
        "company": "VARCHAR(255)",
        "owner": "VARCHAR(255)",
        "subject": "VARCHAR(512)",
        "reply_text": "TEXT",
        "followups": "TEXT",
        "thread": "TEXT",
        "lead_data": "TEXT",
        "send_meta": "TEXT",
        "conf_num": "FLOAT",
        "reviewed": "BOOLEAN",
        "stage": "VARCHAR(40)",
    }

    with engine.begin() as conn:
        for col, coltype in new_columns.items():
            if col not in existing:
                try:
                    conn.execute(text(f"ALTER TABLE leads ADD COLUMN {col} {coltype}"))
                except Exception:
                    pass


def _load_file_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def seed_if_empty():
    """One-time migration: pull existing config.json / profiles / formats /
    env vars into the database the first time the app runs."""
    session = SessionLocal()
    try:
        # ----- settings -----
        defaults = {
            "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
            "human_review_webhook_url": os.getenv("HUMAN_REVIEW_WEBHOOK_URL", ""),
            "emailbison_base_url": os.getenv("EMAILBISON_BASE_URL", "").rstrip("/"),
            "reply_delay_seconds": os.getenv("REPLY_DELAY_SECONDS", "420"),
            "followup_trigger_tag": "Begin follow-up",
            "reply_trigger_tag": "Interested",
        }
        for key, value in defaults.items():
            existing = session.get(Setting, key)
            if existing is None:
                session.add(Setting(key=key, value=value or ""))

        # ----- workspaces -----
        already = session.query(Workspace).count()
        if already == 0:
            config = _load_file_json("config.json")
            base_url_default = os.getenv("EMAILBISON_BASE_URL", "").rstrip("/")

            for name, ws in (config.get("workspaces") or {}).items():
                platform = ws.get("platform", "bison")
                api_key = os.getenv(ws.get("api_key_env", ""), "") if ws.get("api_key_env") else ""

                profile = {}
                if ws.get("client_profile"):
                    profile = _load_file_json(f"client_profiles/{ws['client_profile']}")

                rformat = {}
                if ws.get("reply_format"):
                    rformat = _load_file_json(f"reply_formats/{ws['reply_format']}")

                session.add(Workspace(
                    name=name,
                    platform=platform,
                    active=True,
                    api_key=api_key,
                    base_url="" if platform == "instantly" else base_url_default,
                    reply_followup_campaign_id=str(ws.get("reply_followup_campaign_id", "")),
                    website=ws.get("website", "") or profile.get("website", ""),
                    sender_name=ws.get("sender_name", ""),
                    default_sender_email=ws.get("default_sender_email", ""),
                    client_profile=profile,
                    reply_format=rformat,
                ))

        session.commit()
    finally:
        session.close()


# -------------------------
# Settings helpers
# -------------------------

def get_setting(key, default=""):
    session = SessionLocal()
    try:
        row = session.get(Setting, key)
        if row is None or row.value is None or row.value == "":
            return default
        return row.value
    finally:
        session.close()


def set_setting(key, value):
    session = SessionLocal()
    try:
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value or ""))
        else:
            row.value = value or ""
        session.commit()
    finally:
        session.close()


def all_settings():
    session = SessionLocal()
    try:
        return {row.key: row.value for row in session.query(Setting).all()}
    finally:
        session.close()


# -------------------------
# Workspace helpers
# -------------------------

def get_workspace_config(name):
    """Return a plain dict the webhook code can use, or None if not found.

    Falls back to the old config.json/files/env if the DB has no entry, so the
    system keeps working even before/while seeding."""
    session = SessionLocal()
    try:
        ws = session.query(Workspace).filter(Workspace.name == name).first()
        if ws is not None:
            return {
                "name": ws.name,
                "platform": ws.platform or "bison",
                "mode": ws.mode or "reply",
                "active": bool(ws.active),
                "api_key": ws.api_key or "",
                "base_url": (ws.base_url or "").rstrip("/"),
                "reply_followup_campaign_id": ws.reply_followup_campaign_id or "",
                "website": ws.website or "",
                "sender_name": ws.sender_name or "",
                "default_sender_email": ws.default_sender_email or "",
                "calendly_token": ws.calendly_token or "",
                "calendly_scheduling_url": ws.calendly_scheduling_url or "",
                "ai_provider": ws.ai_provider or "openai",
                "ai_fallback": bool(ws.ai_fallback),
                "openai_key": ws.openai_key or "",
                "gemini_key": ws.gemini_key or "",
                "client_profile": ws.client_profile or {},
                "reply_format": ws.reply_format or {},
            }
    finally:
        session.close()

    # ---- fallback to files/env (legacy behavior) ----
    config = _load_file_json("config.json")
    ws = (config.get("workspaces") or {}).get(name)
    if not ws:
        return None

    platform = ws.get("platform", "bison")
    profile = _load_file_json(f"client_profiles/{ws['client_profile']}") if ws.get("client_profile") else {}
    rformat = _load_file_json(f"reply_formats/{ws['reply_format']}") if ws.get("reply_format") else {}
    return {
        "name": name,
        "platform": platform,
        "mode": "reply",
        "active": True,
        "api_key": os.getenv(ws.get("api_key_env", ""), "") if ws.get("api_key_env") else "",
        "base_url": os.getenv("EMAILBISON_BASE_URL", "").rstrip("/"),
        "reply_followup_campaign_id": str(ws.get("reply_followup_campaign_id", "")),
        "website": ws.get("website", "") or profile.get("website", ""),
        "sender_name": ws.get("sender_name", ""),
        "default_sender_email": ws.get("default_sender_email", ""),
        "client_profile": profile,
        "reply_format": rformat,
    }


def list_workspaces():
    session = SessionLocal()
    try:
        return session.query(Workspace).order_by(Workspace.name).all()
    finally:
        session.close()


def get_workspace(ws_id):
    session = SessionLocal()
    try:
        return session.get(Workspace, int(ws_id))
    finally:
        session.close()


def save_workspace(ws_id, data):
    """Create (ws_id falsy) or update a workspace from a dict of fields."""
    session = SessionLocal()
    try:
        if ws_id:
            ws = session.get(Workspace, int(ws_id))
            if ws is None:
                return None
        else:
            ws = Workspace(name=data.get("name", "").strip())
            session.add(ws)

        ws.name = data.get("name", ws.name).strip()
        ws.platform = data.get("platform", "bison")
        ws.mode = data.get("mode", "reply")
        ws.active = bool(data.get("active", True))
        ws.api_key = data.get("api_key", "")
        ws.base_url = data.get("base_url", "")
        ws.reply_followup_campaign_id = str(data.get("reply_followup_campaign_id", ""))
        ws.website = data.get("website", "")
        ws.sender_name = data.get("sender_name", "")
        ws.default_sender_email = data.get("default_sender_email", "")
        ws.calendly_token = data.get("calendly_token", "")
        ws.calendly_scheduling_url = data.get("calendly_scheduling_url", "")
        ws.ai_provider = data.get("ai_provider", "openai")
        ws.ai_fallback = bool(data.get("ai_fallback", False))
        ws.openai_key = data.get("openai_key", "")
        ws.gemini_key = data.get("gemini_key", "")
        ws.client_profile = data.get("client_profile", {})
        ws.reply_format = data.get("reply_format", {})

        session.commit()
        return ws.id
    finally:
        session.close()


def delete_workspace(ws_id):
    session = SessionLocal()
    try:
        ws = session.get(Workspace, int(ws_id))
        if ws is not None:
            session.delete(ws)
            session.commit()
    finally:
        session.close()


# -------------------------
# Proposed-slot reservation (avoid pitching the same time twice)
# -------------------------

def get_reserved_slot_keys(workspace_name, now_iso):
    """Return the set of slot_utc keys already proposed for this workspace that
    are still in the future (past slots are irrelevant). Also prunes old rows."""
    session = SessionLocal()
    try:
        # prune rows whose slot time has already passed
        try:
            session.query(ProposedSlot).filter(
                ProposedSlot.workspace_name == workspace_name,
                ProposedSlot.slot_utc < now_iso,
            ).delete(synchronize_session=False)
            session.commit()
        except Exception:
            session.rollback()

        rows = session.query(ProposedSlot.slot_utc).filter(
            ProposedSlot.workspace_name == workspace_name,
            ProposedSlot.slot_utc >= now_iso,
        ).all()
        return {r[0] for r in rows if r[0]}
    finally:
        session.close()


def reserve_slots(workspace_name, prospect, items):
    """Record that we proposed these slots. `items` is a list of
    {"utc": iso, "label": str}. Skips any slot already reserved (idempotent)."""
    if not items:
        return
    session = SessionLocal()
    try:
        existing = {
            r[0] for r in session.query(ProposedSlot.slot_utc).filter(
                ProposedSlot.workspace_name == workspace_name,
            ).all()
        }
        for it in items:
            utc = (it or {}).get("utc", "")
            if not utc or utc in existing:
                continue
            session.add(ProposedSlot(
                workspace_name=workspace_name,
                prospect=str(prospect or "")[:255],
                slot_utc=utc,
                label=(it or {}).get("label", ""),
            ))
            existing.add(utc)
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


# -------------------------
# Lead helpers
# -------------------------

def upsert_lead(
    platform,
    external_lead_id,
    reply_id="",
    workspace_name="",
    name="",
    email="",
    interested=None,
    intent="",
    confidence="",
    action="",
    reply_added=False,
    replied=False,
    fup_added=False,
    main_reply="",
    reply_text="",
    subject="",
    campaign="",
    company="",
    followups=None,
    thread=None,
    lead_data=None,
    send_meta=None,
):
    external_lead_id = str(external_lead_id or "")
    reply_id = str(reply_id or "")
    dedupe_key = f"{platform}:{external_lead_id}:{reply_id or email}"

    try:
        conf_num = float(confidence)
    except (TypeError, ValueError):
        conf_num = 0.0

    session = SessionLocal()
    try:
        lead = session.query(Lead).filter(Lead.dedupe_key == dedupe_key).first()
        created = lead is None
        if created:
            lead = Lead(dedupe_key=dedupe_key)
            session.add(lead)

        lead.workspace_name = workspace_name
        lead.platform = platform
        lead.external_lead_id = external_lead_id
        lead.reply_id = reply_id
        lead.name = name or ""
        lead.email = email or ""
        lead.interested = interested
        lead.intent = intent or ""
        lead.confidence = str(confidence or "")
        lead.conf_num = conf_num
        lead.action = action or ""
        lead.reply_added = bool(reply_added)
        lead.replied = bool(replied)
        lead.fup_added = bool(fup_added)
        lead.main_reply = main_reply or ""
        lead.reply_text = reply_text or ""
        lead.subject = subject or ""
        lead.campaign = campaign or ""
        lead.company = company or ""
        lead.followups = json.dumps(followups or [])
        lead.thread = json.dumps(thread or [])
        if lead_data is not None:
            lead.lead_data = json.dumps(lead_data or {})
        if send_meta is not None:
            lead.send_meta = json.dumps(send_meta or {})

        # Set a sensible default stage only on creation; never overwrite a
        # stage a human has already set (e.g. "booked") when re-processing.
        if created or not (lead.stage or ""):
            if (action or "") == "stop":
                lead.stage = "stopped"
            elif (action or "") == "error":
                lead.stage = "new"
            else:
                lead.stage = "replied"

        session.commit()
        return lead.id
    finally:
        session.close()


def get_lead(lead_id):
    session = SessionLocal()
    try:
        return session.get(Lead, int(lead_id))
    finally:
        session.close()


def lead_already_handled(workspace_name, external_lead_id):
    """True if we've ALREADY processed this lead in this workspace (sent a reply,
    enriched follow-ups, or drafted a reply). Used to avoid re-replying when a
    lead that is already in the follow-up campaign replies again."""
    ext = str(external_lead_id or "")
    if not ext:
        return False
    session = SessionLocal()
    try:
        return session.query(Lead.id).filter(
            Lead.workspace_name == workspace_name,
            Lead.external_lead_id == ext,
            or_(Lead.replied.is_(True),
                Lead.fup_added.is_(True),
                Lead.reply_added.is_(True)),
        ).first() is not None
    finally:
        session.close()


def _apply_lead_filters(q, workspace=None, status=None, intent=None, action=None,
                        search=None, conf_min=None, date_from=None, date_to=None, stage=None):
    if workspace:
        q = q.filter(Lead.workspace_name == workspace)
    if stage:
        q = q.filter(Lead.stage == stage)
    if intent:
        q = q.filter(Lead.intent == intent)
    if action:
        q = q.filter(Lead.action == action)
    if search:
        like = f"%{search.strip()}%"
        q = q.filter(or_(
            Lead.name.ilike(like),
            Lead.email.ilike(like),
            Lead.company.ilike(like),
        ))
    if conf_min:
        try:
            q = q.filter(Lead.conf_num >= float(conf_min))
        except (TypeError, ValueError):
            pass
    if date_from:
        q = q.filter(Lead.created_at >= date_from)
    if date_to:
        q = q.filter(Lead.created_at <= date_to)

    if status == "replied" or status == "sent":
        q = q.filter(Lead.replied.is_(True))
    elif status == "enriched":
        q = q.filter(Lead.fup_added.is_(True))
    elif status == "not_enriched":
        q = q.filter(Lead.fup_added.is_(False))
    elif status == "stopped":
        q = q.filter(Lead.action == "stop")
    elif status == "needs_review":
        q = q.filter(Lead.action.in_(["skip_enrich", "error"]),
                     (Lead.reviewed.is_(False)) | (Lead.reviewed.is_(None)))
    elif status == "reviewed":
        q = q.filter(Lead.reviewed.is_(True))
    return q


def list_leads(workspace=None, status=None, intent=None, action=None, search=None,
               conf_min=None, date_from=None, date_to=None, stage=None, offset=0, limit=25):
    session = SessionLocal()
    try:
        q = _apply_lead_filters(session.query(Lead), workspace, status, intent,
                                action, search, conf_min, date_from, date_to, stage)
        total = q.count()
        rows = q.order_by(Lead.created_at.desc()).offset(offset).limit(limit).all()
        return rows, total
    finally:
        session.close()


def export_leads(workspace=None, status=None, intent=None, action=None, search=None,
                 conf_min=None, date_from=None, date_to=None, stage=None, ids=None):
    session = SessionLocal()
    try:
        q = session.query(Lead)
        if ids:
            q = q.filter(Lead.id.in_(ids))
        else:
            q = _apply_lead_filters(q, workspace, status, intent, action,
                                    search, conf_min, date_from, date_to, stage)
        return q.order_by(Lead.created_at.desc()).all()
    finally:
        session.close()


def distinct_intents():
    session = SessionLocal()
    try:
        rows = session.query(Lead.intent).distinct().all()
        return sorted({r[0] for r in rows if r[0]})
    finally:
        session.close()


def lead_counts(workspace=None):
    session = SessionLocal()
    try:
        base = session.query(Lead)
        if workspace:
            base = base.filter(Lead.workspace_name == workspace)

        def c(query):
            return query.count()

        total = c(base)
        replied = c(base.filter(Lead.replied.is_(True)))
        enriched = c(base.filter(Lead.fup_added.is_(True)))
        stopped = c(base.filter(Lead.action == "stop"))
        needs_review = c(base.filter(
            Lead.action.in_(["skip_enrich", "error"]),
            (Lead.reviewed.is_(False)) | (Lead.reviewed.is_(None)),
        ))
        booked = c(base.filter(Lead.stage == "booked"))
        won = c(base.filter(Lead.stage == "won"))
        return {
            "total": total,
            "replied": replied,
            "sent": replied,
            "enriched": enriched,
            "stopped": stopped,
            "needs_review": needs_review,
            "booked": booked,
            "won": won,
        }
    finally:
        session.close()


def update_lead_fields(lead_id, **fields):
    """Set arbitrary columns on a lead. followups accepts a list (json-encoded)."""
    session = SessionLocal()
    try:
        lead = session.get(Lead, int(lead_id))
        if lead is None:
            return False
        for key, value in fields.items():
            if key == "followups" and isinstance(value, list):
                value = json.dumps(value)
            if hasattr(lead, key):
                setattr(lead, key, value)
        session.commit()
        return True
    finally:
        session.close()


def delete_leads(ids):
    session = SessionLocal()
    try:
        count = 0
        for lead_id in ids:
            lead = session.get(Lead, int(lead_id))
            if lead is not None:
                session.delete(lead)
                count += 1
        session.commit()
        return count
    finally:
        session.close()


def bulk_update_leads(ids, **fields):
    session = SessionLocal()
    try:
        count = 0
        for lead_id in ids:
            lead = session.get(Lead, int(lead_id))
            if lead is None:
                continue
            for key, value in fields.items():
                if hasattr(lead, key):
                    setattr(lead, key, value)
            count += 1
        session.commit()
        return count
    finally:
        session.close()


# -------------------------
# CRM helpers
# -------------------------

def seed_crm_if_empty():
    session = SessionLocal()
    try:
        if session.query(CrmStage).count() == 0:
            for i, (name, color, won, lost) in enumerate(DEFAULT_CRM_STAGES):
                session.add(CrmStage(name=name, color=color, sort_order=i,
                                     is_won=won, is_lost=lost))
            session.commit()
    finally:
        session.close()


def list_stages():
    session = SessionLocal()
    try:
        return session.query(CrmStage).order_by(CrmStage.sort_order, CrmStage.id).all()
    finally:
        session.close()


def get_stage(stage_id):
    session = SessionLocal()
    try:
        return session.get(CrmStage, int(stage_id))
    finally:
        session.close()


def save_stage(stage_id, data):
    session = SessionLocal()
    try:
        st = session.get(CrmStage, int(stage_id)) if stage_id else None
        if st is None:
            st = CrmStage()
            session.add(st)
            # place new stage at the end
            mx = session.query(CrmStage).count()
            st.sort_order = mx
        if "name" in data:
            st.name = (data.get("name") or "").strip()
        if "color" in data:
            st.color = (data.get("color") or "#64748b").strip()
        if "sort_order" in data:
            try:
                st.sort_order = int(data["sort_order"])
            except (TypeError, ValueError):
                pass
        if "is_won" in data:
            st.is_won = bool(data["is_won"])
        if "is_lost" in data:
            st.is_lost = bool(data["is_lost"])
        session.commit()
        return st.id
    finally:
        session.close()


def delete_stage(stage_id):
    """Delete a stage. Opportunities in it are moved to the first remaining stage."""
    session = SessionLocal()
    try:
        st = session.get(CrmStage, int(stage_id))
        if st is None:
            return
        fallback = (session.query(CrmStage).filter(CrmStage.id != st.id)
                    .order_by(CrmStage.sort_order, CrmStage.id).first())
        fb_id = fallback.id if fallback else None
        session.query(Opportunity).filter(Opportunity.stage_id == st.id)\
            .update({Opportunity.stage_id: fb_id}, synchronize_session=False)
        session.delete(st)
        session.commit()
    finally:
        session.close()


def list_tags():
    session = SessionLocal()
    try:
        return session.query(CrmTag).order_by(CrmTag.name).all()
    finally:
        session.close()


def save_tag(tag_id, data):
    session = SessionLocal()
    try:
        t = session.get(CrmTag, int(tag_id)) if tag_id else None
        if t is None:
            t = CrmTag()
            session.add(t)
        t.name = (data.get("name") or "").strip()
        t.color = (data.get("color") or "#7857f8").strip()
        session.commit()
        return t.id
    finally:
        session.close()


def delete_tag(tag_id):
    session = SessionLocal()
    try:
        t = session.get(CrmTag, int(tag_id))
        if t is not None:
            session.delete(t)
            session.commit()
    finally:
        session.close()


def list_opportunities(workspace=None, search=None):
    session = SessionLocal()
    try:
        q = session.query(Opportunity)
        if workspace:
            q = q.filter(Opportunity.workspace_name == workspace)
        if search:
            like = f"%{search.strip()}%"
            q = q.filter(or_(Opportunity.company.ilike(like),
                             Opportunity.contact_name.ilike(like),
                             Opportunity.email.ilike(like),
                             Opportunity.deal_name.ilike(like)))
        return q.order_by(Opportunity.updated_at.desc()).all()
    finally:
        session.close()


def get_opportunity(opp_id):
    session = SessionLocal()
    try:
        return session.get(Opportunity, int(opp_id))
    finally:
        session.close()


_OPP_FIELDS = ("workspace_name", "lead_id", "deal_name", "contact_name", "email",
               "company", "website", "lead_intent", "status", "owner",
               "description", "next_action_date", "tag_ids",
               "close_date", "source", "next_step", "meeting_outcome")


def save_opportunity(opp_id, data):
    session = SessionLocal()
    try:
        opp = session.get(Opportunity, int(opp_id)) if opp_id else None
        if opp is None:
            opp = Opportunity()
            session.add(opp)
            # default stage = first
            first = session.query(CrmStage).order_by(CrmStage.sort_order, CrmStage.id).first()
            opp.stage_id = first.id if first else None
        for f in _OPP_FIELDS:
            if f in data:
                setattr(opp, f, data[f])
        if "value" in data:
            try:
                opp.value = float(data["value"] or 0)
            except (TypeError, ValueError):
                opp.value = 0.0
        if "stage_id" in data and data["stage_id"]:
            new_stage = int(data["stage_id"])
            if new_stage != (opp.stage_id or 0):
                opp.stage_changed_at = datetime.utcnow()
            opp.stage_id = new_stage
        session.commit()
        return opp.id
    finally:
        session.close()


def move_opportunity(opp_id, stage_id):
    session = SessionLocal()
    try:
        opp = session.get(Opportunity, int(opp_id))
        if opp is None:
            return False
        opp.stage_id = int(stage_id)
        opp.stage_changed_at = datetime.utcnow()
        session.commit()
        return True
    finally:
        session.close()


def delete_opportunity(opp_id):
    session = SessionLocal()
    try:
        opp = session.get(Opportunity, int(opp_id))
        if opp is not None:
            session.delete(opp)
            session.commit()
    finally:
        session.close()


def opportunity_stage_totals(workspace=None):
    """Return {stage_id: {'count': n, 'value': sum}} for column headers."""
    session = SessionLocal()
    try:
        q = session.query(Opportunity)
        if workspace:
            q = q.filter(Opportunity.workspace_name == workspace)
        totals = {}
        for opp in q.all():
            t = totals.setdefault(opp.stage_id, {"count": 0, "value": 0.0})
            t["count"] += 1
            t["value"] += float(opp.value or 0)
        return totals
    finally:
        session.close()


def extract_lead_enrichment(lead_data_json):
    """Pull company/location/LinkedIn/website out of the raw platform payload
    (Bison & Instantly use different key names — be tolerant)."""
    out = {"company": "", "location": "", "contact_linkedin": "", "company_linkedin": "", "website": ""}
    try:
        d = json.loads(lead_data_json or "{}")
    except Exception:
        return out
    if not isinstance(d, dict):
        return out
    # payloads sometimes nest under "lead" / "custom_variables" / "customFields"
    layers = [d]
    for k in ("lead", "custom_variables", "customFields", "custom_fields", "payload"):
        if isinstance(d.get(k), dict):
            layers.append(d[k])
    def pick(*keys):
        for layer in layers:
            low = {str(kk).lower().replace("_", "").replace(" ", ""): vv for kk, vv in layer.items()}
            for key in keys:
                v = low.get(key)
                if v and isinstance(v, str) and v.strip():
                    return v.strip()
        return ""
    out["company"] = pick("companyname", "company", "organization", "orgname")
    out["location"] = pick("location", "city", "companylocation", "region", "country", "state")
    out["contact_linkedin"] = pick("linkedin", "linkedinurl", "linkedinprofile", "personallinkedin", "linkedinpersonal")
    out["company_linkedin"] = pick("companylinkedin", "companylinkedinurl", "organizationlinkedin", "linkedincompany", "companylinkedinprofile")
    out["website"] = pick("website", "companywebsite", "domain", "companydomain", "websiteurl")
    if out["website"] and not out["website"].startswith("http"):
        out["website"] = "https://" + out["website"]
    return out


def backfill_opportunities_from_booked(workspace=None):
    """One-time catch-up: create CRM opportunities for every existing lead whose
    stage is 'booked' that doesn't already have one. Optionally scoped to one
    workspace. Returns the count created."""
    session = SessionLocal()
    try:
        q = session.query(Lead).filter(Lead.stage == "booked")
        if workspace:
            q = q.filter(Lead.workspace_name == workspace)
        leads = q.all()
        existing = {(o.workspace_name or "", o.lead_id or "")
                    for o in session.query(Opportunity).all()}
        stage = (session.query(CrmStage).filter(CrmStage.name == "Meeting Booked").first()
                 or session.query(CrmStage).order_by(CrmStage.sort_order, CrmStage.id).first())
        created = 0
        for ld in leads:
            key = (ld.workspace_name or "", ld.external_lead_id or str(ld.id))
            if key in existing:
                continue
            enr = extract_lead_enrichment(ld.lead_data)
            session.add(Opportunity(
                workspace_name=key[0], lead_id=key[1],
                deal_name=(ld.company or enr["company"] or ld.name or "New deal"),
                contact_name=ld.name or "", email=ld.email or "",
                company=(ld.company or enr["company"]),
                website=enr["website"], location=enr["location"],
                contact_linkedin=enr["contact_linkedin"], company_linkedin=enr["company_linkedin"],
                lead_intent=ld.intent or "", stage_id=(stage.id if stage else None)))
            existing.add(key)
            created += 1
        session.commit()
        return created
    finally:
        session.close()


def upsert_opportunity_from_lead(workspace_name, lead_id, contact_name="", email="",
                                 company="", lead_intent="", stage_name="Meeting Booked",
                                 lead_data=""):
    """Create a CRM opportunity for a booked lead. De-dupes by (workspace, lead_id):
    if one already exists, it is left as-is (returns its id)."""
    lead_id = str(lead_id or "")
    session = SessionLocal()
    try:
        existing = (session.query(Opportunity)
                    .filter(Opportunity.workspace_name == workspace_name,
                            Opportunity.lead_id == lead_id).first())
        if existing is not None:
            return existing.id
        stage = (session.query(CrmStage).filter(CrmStage.name == stage_name).first()
                 or session.query(CrmStage).order_by(CrmStage.sort_order, CrmStage.id).first())
        enr = extract_lead_enrichment(lead_data)
        opp = Opportunity(
            workspace_name=workspace_name, lead_id=lead_id,
            deal_name=(company or enr["company"] or contact_name or "New deal"),
            contact_name=contact_name, email=email,
            company=(company or enr["company"]),
            website=enr["website"], location=enr["location"],
            contact_linkedin=enr["contact_linkedin"], company_linkedin=enr["company_linkedin"],
            lead_intent=lead_intent, stage_id=(stage.id if stage else None),
        )
        session.add(opp)
        session.commit()
        return opp.id
    finally:
        session.close()
