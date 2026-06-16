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
    Column,
    Integer,
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
    active = Column(Boolean, default=True)

    api_key = Column(Text, default="")
    base_url = Column(Text, default="")                      # Bison base URL
    reply_followup_campaign_id = Column(String(255), default="")

    website = Column(Text, default="")
    sender_name = Column(Text, default="")
    default_sender_email = Column(Text, default="")

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
    action = Column(String(50), default="")                 # send / skip_enrich / stop

    reply_added = Column(Boolean, default=False)            # a main_reply was generated
    replied = Column(Boolean, default=False)               # reply was actually sent
    fup_added = Column(Boolean, default=False)             # follow-up variables written

    main_reply = Column(Text, default="")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# -------------------------
# Init + seed
# -------------------------

def init_db():
    Base.metadata.create_all(bind=engine)


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
                "active": bool(ws.active),
                "api_key": ws.api_key or "",
                "base_url": (ws.base_url or "").rstrip("/"),
                "reply_followup_campaign_id": ws.reply_followup_campaign_id or "",
                "website": ws.website or "",
                "sender_name": ws.sender_name or "",
                "default_sender_email": ws.default_sender_email or "",
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
        ws.active = bool(data.get("active", True))
        ws.api_key = data.get("api_key", "")
        ws.base_url = data.get("base_url", "")
        ws.reply_followup_campaign_id = str(data.get("reply_followup_campaign_id", ""))
        ws.website = data.get("website", "")
        ws.sender_name = data.get("sender_name", "")
        ws.default_sender_email = data.get("default_sender_email", "")
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
):
    external_lead_id = str(external_lead_id or "")
    reply_id = str(reply_id or "")
    dedupe_key = f"{platform}:{external_lead_id}:{reply_id or email}"

    session = SessionLocal()
    try:
        lead = session.query(Lead).filter(Lead.dedupe_key == dedupe_key).first()
        if lead is None:
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
        lead.action = action or ""
        lead.reply_added = bool(reply_added)
        lead.replied = bool(replied)
        lead.fup_added = bool(fup_added)
        lead.main_reply = main_reply or ""

        session.commit()
        return lead.id
    finally:
        session.close()


def list_leads(workspace=None, status=None, limit=500):
    session = SessionLocal()
    try:
        q = session.query(Lead)
        if workspace:
            q = q.filter(Lead.workspace_name == workspace)
        if status == "replied":
            q = q.filter(Lead.replied.is_(True))
        elif status == "enriched":
            q = q.filter(Lead.replied.is_(False), Lead.fup_added.is_(True))
        elif status == "stopped":
            q = q.filter(Lead.action == "stop")
        return q.order_by(Lead.created_at.desc()).limit(limit).all()
    finally:
        session.close()


def lead_counts():
    session = SessionLocal()
    try:
        total = session.query(Lead).count()
        replied = session.query(Lead).filter(Lead.replied.is_(True)).count()
        enriched = session.query(Lead).filter(Lead.fup_added.is_(True)).count()
        stopped = session.query(Lead).filter(Lead.action == "stop").count()
        return {"total": total, "replied": replied, "enriched": enriched, "stopped": stopped}
    finally:
        session.close()
