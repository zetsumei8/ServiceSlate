from __future__ import annotations

import json
import math
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

from .db import BACKUPS_DIR, DATA_DIR, DB_PATH, FILES_DIR, audit, command_once, connect, create_backup_archive, database_backend, hash_password, init_db, new_id, seed_profile_defaults, utcnow, verify_password
from .features import router as features_router
from .tools import router as tools_router
from .integrations import router as integrations_router
from .governance import router as governance_router
from .cloud_connectors import router as cloud_router
from .sales import router as sales_router
from .security import router as security_router, note_login_failure, login_lock_state, complete_login, verify_totp
from .updates import router as updates_router
from .lan import router as lan_router

BASE = Path(__file__).resolve().parent
SESSION_SECRET_PATH = DB_PATH.parent / ".session-secret"
SESSION_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
SESSION_SECRET = os.environ.get("SERVICESLATE_SESSION_SECRET", "").strip()
if not SESSION_SECRET:
    if SESSION_SECRET_PATH.exists():
        SESSION_SECRET = SESSION_SECRET_PATH.read_text(encoding="utf-8").strip()
    else:
        SESSION_SECRET = secrets.token_hex(32)
        SESSION_SECRET_PATH.write_text(SESSION_SECRET, encoding="utf-8")

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="ServiceSlate", version="0.11.1", lifespan=lifespan)
PRODUCTION_MODE = os.environ.get("SERVICESLATE_PRODUCTION_MODE", "0") == "1"

class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"timestamp": utcnow(), "level": record.levelname, "message": record.getMessage()}
        for key in ("method", "path", "status", "duration_ms", "request_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, separators=(",", ":"))

def _configure_service_log() -> logging.Logger:
    logger = logging.getLogger("serviceslate")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logs = DATA_DIR / "logs"; logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs / "serviceslate.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(JsonLogFormatter()); logger.addHandler(handler); logger.propagate = False
    return logger

SERVICE_LOG = _configure_service_log()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=os.environ.get("SERVICESLATE_SECURE_COOKIES","0")=="1" or PRODUCTION_MODE, max_age=int(os.environ.get("SERVICESLATE_SESSION_MAX_AGE","28800")))
if PRODUCTION_MODE:
    allowed_hosts=[h.strip() for h in os.environ.get("SERVICESLATE_ALLOWED_HOSTS","localhost,127.0.0.1").split(",") if h.strip()]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    if os.environ.get("SERVICESLATE_HTTPS_REDIRECT","1")=="1":
        app.add_middleware(HTTPSRedirectMiddleware)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(features_router)
app.include_router(tools_router)
app.include_router(integrations_router)
app.include_router(governance_router)
app.include_router(cloud_router)
app.include_router(sales_router)
app.include_router(security_router)
app.include_router(updates_router)
app.include_router(lan_router)

_RATE_EVENTS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LOCK = threading.Lock()

@app.middleware("http")
async def request_audit_log(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or secrets.token_hex(8)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration = round((time.perf_counter() - started) * 1000, 1)
        SERVICE_LOG.exception("request_failed", extra={"method":request.method,"path":request.url.path,"status":500,"duration_ms":duration,"request_id":request_id})
        raise
    duration = round((time.perf_counter() - started) * 1000, 1)
    SERVICE_LOG.info("request", extra={"method":request.method,"path":request.url.path,"status":response.status_code,"duration_ms":duration,"request_id":request_id})
    response.headers.setdefault("X-Request-ID", request_id)
    return response

def _rate_allowed(key: str, limit: int, window_seconds: int = 60) -> bool:
    now = time.monotonic(); cutoff = now - window_seconds
    with _RATE_LOCK:
        q = _RATE_EVENTS[key]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

@app.middleware("http")
async def public_rate_limit(request: Request, call_next):
    # The limiter protects an internet-facing deployment. Local Alpha/test use
    # does not share a meaningful public client IP and must not accumulate
    # artificial lockouts across unrelated users or test cases.
    if not PRODUCTION_MODE:
        return await call_next(request)
    path = request.url.path
    client = request.client.host if request.client else "unknown"
    if path.startswith("/api/public/"):
        if not _rate_allowed(f"public:{client}", int(os.environ.get("SERVICESLATE_PUBLIC_RATE_LIMIT", "90"))):
            return JSONResponse({"detail":"Too many requests. Wait a moment and try again."}, status_code=429, headers={"Retry-After":"60"})
    elif path in {"/api/login", "/api/login/mfa", "/api/security/microsoft/start"}:
        if not _rate_allowed(f"login:{client}", int(os.environ.get("SERVICESLATE_LOGIN_RATE_LIMIT", "30"))):
            return JSONResponse({"detail":"Too many sign-in attempts from this connection. Try again shortly."}, status_code=429, headers={"Retry-After":"60"})
    return await call_next(request)


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    if PRODUCTION_MODE and request.method in {"POST","PUT","PATCH","DELETE"} and request.url.path.startswith("/api/"):
        exempt = request.url.path in {"/api/login","/api/login/mfa","/api/cloud/quickbooks/desktop/soap"} or request.url.path.startswith("/api/public/") or request.url.path.startswith("/api/cloud/google-drive/oauth/callback")
        if not exempt and request.session.get("user_id"):
            supplied=request.headers.get("X-CSRF-Token","")
            expected=request.session.get("csrf_token","")
            if not expected or not secrets.compare_digest(supplied,expected):
                return JSONResponse({"detail":"Security check failed. Refresh ServiceSlate and try again."},status_code=403)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(self), camera=(self)")
    if PRODUCTION_MODE:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def rowdict(row):
    return dict(row) if row is not None else None


def require_user(request: Request) -> dict[str, Any]:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Sign in required")
    with connect() as conn:
        row = conn.execute(
            """SELECT u.*, o.name organization_name, o.profile organization_profile, o.is_demo
               FROM users u JOIN organizations o ON o.id=u.organization_id WHERE u.id=? AND u.active=1""",
            (uid,),
        ).fetchone()
    if not row:
        request.session.clear()
        raise HTTPException(401, "Sign in required")
    return dict(row)


def org_guard(user, org_id: str | None = None):
    if org_id and org_id != user["organization_id"]:
        raise HTTPException(403, "Wrong organization")


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return (BASE / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/sw.js")
def service_worker():
    return FileResponse(BASE / "static" / "sw.js", media_type="application/javascript", headers={"Service-Worker-Allowed":"/"})


@app.get("/healthz")
def process_health():
    """Minimal unauthenticated container health check; exposes no company data."""
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"ok": True, "database": database_backend()}
    except Exception:
        raise HTTPException(503, "ServiceSlate data service is unavailable")


@app.get("/api/health")
def health(request: Request):
    u = require_user(request)
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()
        if database_backend() == "postgresql":
            integrity = "ok"
        else:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) AS n FROM work_submissions WHERE organization_id=? AND state='SUBMITTED'", (u["organization_id"],)).fetchone()["n"]
        outlook_row = conn.execute("SELECT state FROM integration_status WHERE organization_id=? AND provider='OUTLOOK'", (u["organization_id"],)).fetchone()
        outlook_state = outlook_row["state"].lower() if outlook_row else "not_connected"
    return {
        "database": "working" if integrity == "ok" else "needs_attention",
        "database_integrity": integrity,
        "files": "working" if FILES_DIR.exists() else "needs_attention",
        "storage_path": str(DB_PATH.parent),
        "pending_reviews": pending,
        "email": outlook_state,
        "outlook": outlook_state,
    }


@app.post("/api/backups")
def create_backup(request: Request):
    u = require_user(request)
    if u["role"] not in ("ADMIN", "MANAGER", "COORDINATOR"):
        raise HTTPException(403, "Backup permission required")
    # A physical database archive is only safe when this installation contains
    # one live company.  Shared-host recovery remains an operator task; users
    # can use the existing organization-scoped export instead.
    from .cloud_connectors import organization_database_backup_is_safe_for_offsite
    allowed, reason = organization_database_backup_is_safe_for_offsite(u["organization_id"])
    if not allowed:
        raise HTTPException(409, reason)
    path = create_backup_archive()
    mirrors = []
    try:
        from .integrations import _provider_row
        from .cloud_connectors import organization_database_backup_is_safe_for_offsite, sync_path_to_google_drive, sync_path_to_s3
        allowed_offsite, reason = organization_database_backup_is_safe_for_offsite(u["organization_id"])
        drive_settings, _ = _provider_row(u["organization_id"], "GOOGLE_DRIVE")
        s3_settings, _ = _provider_row(u["organization_id"], "S3")
        if allowed_offsite:
            if drive_settings.get("sync_backups"):
                mirrors.append({"provider":"GOOGLE_DRIVE", **sync_path_to_google_drive(u["organization_id"], path, local_kind="backup", local_id=path.name, target="backups")})
            if s3_settings.get("sync_backups") or s3_settings.get("mirror_backups") or os.environ.get("SERVICESLATE_DEFAULT_S3_SYNC_BACKUPS", "0") == "1":
                mirrors.append({"provider":"S3", **sync_path_to_s3(u["organization_id"], path, local_kind="backup", local_id=path.name, target="backups")})
        elif drive_settings.get("sync_backups") or s3_settings.get("sync_backups") or s3_settings.get("mirror_backups"):
            mirrors.append({"state":"HOST_MANAGED_REQUIRED","error":reason})
    except Exception as exc:
        mirrors.append({"state":"NEEDS_ATTENTION","error":str(exc)[:500]})
    return {"ok": True, "filename": path.name, "path": str(path), "created_at": utcnow(), "offsite": mirrors}


class OrganizationSetup(BaseModel):
    business_name: str
    profile: str
    admin_name: str
    email: str
    password: str
    phone: str | None = None
    address1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None


@app.get("/api/setup/status")
def setup_status():
    return {
        "profiles": [
            {"id":"automotive_equipment","name":"Automotive Equipment Sales & Service","description":"Equipment, jobs, inspections, parts, vehicles and technician field work."},
            {"id":"grooming","name":"Pet Grooming","description":"Pets, appointments, forms, waitlist and rebooking."},
        ],
        "local_first": True,
    }


@app.post("/api/setup/organization")
def setup_organization(p: OrganizationSetup, request: Request):
    profile = p.profile.strip()
    if profile not in ("automotive_equipment", "grooming"):
        raise HTTPException(400, "Choose one of the available business types")
    if len(p.business_name.strip()) < 2 or len(p.admin_name.strip()) < 2:
        raise HTTPException(400, "Business name and your name are required")
    if len(p.password) < 12:
        raise HTTPException(400, "Use a password with at least 12 characters")
    email = p.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter a valid email address")
    org_id = new_id("org"); user_id = new_id("user"); now = utcnow()
    production_mode = os.getenv("SERVICESLATE_PRODUCTION_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
    public_base_url = os.getenv("SERVICESLATE_PUBLIC_BASE_URL", "").strip().rstrip("/") or None
    if not public_base_url:
        domain = os.getenv("SERVICESLATE_DOMAIN", "").strip().strip("/")
        if domain:
            public_base_url = f"https://{domain}"
    if production_mode and public_base_url and not public_base_url.startswith("https://"):
        raise HTTPException(500, "Production public address must use HTTPS")
    settings = {"phone":p.phone,"address1":p.address1,"city":p.city,"state":p.state,"postal_code":p.postal_code}
    with connect() as conn:
        if conn.execute("SELECT 1 FROM users WHERE lower(email)=lower(?)", (email,)).fetchone():
            raise HTTPException(409, "That email already has a ServiceSlate account")
        conn.execute("""INSERT INTO organizations(id,name,profile,is_demo,timezone,created_at,profile_version,settings_json,setup_complete,production_mode,public_base_url)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (org_id,p.business_name.strip(),profile,0,"America/Los_Angeles",now,"1",json.dumps(settings),1,int(production_mode),public_base_url))
        role = "ADMIN"
        conn.execute("""INSERT INTO users(id,organization_id,email,name,role,password_hash,qualifications_json,active,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (user_id,org_id,email,p.admin_name.strip(),role,hash_password(p.password),"[]",1,now))
        if profile == "automotive_equipment":
            conn.execute("""INSERT INTO branches(id,organization_id,name,address1,city,state,postal_code,active,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (new_id("branch"),org_id,"Main Office",p.address1,p.city,p.state,p.postal_code,1,now,now))
        seed_profile_defaults(conn, org_id, profile)
        for provider in ("GOOGLE_DRIVE","MICROSOFT_GRAPH","MOBILE_SMS","SMTP","NTFY","GOTIFY","APPRISE","TWILIO_SMS","O365_OUTBOUND","WEBHOOK","WEBDAV","S3","CALDAV","CARDDAV","NOMINATIM","OSRM","TESSERACT","CLAMAV","OUTLOOK","QUICKBOOKS"):
            conn.execute("INSERT INTO integration_status(id,organization_id,provider,state,updated_at,enabled) VALUES(?,?,?,?,?,1)",(new_id("integration"),org_id,provider,"AVAILABLE" if provider in ("MOBILE_SMS","OUTLOOK") else "NOT_CONNECTED",now))
        audit(conn,org_id,user_id,"organization",org_id,"CREATED","Completed guided organization setup")
    request.session["user_id"] = user_id
    return {"organization_id":org_id,"profile":profile,"ready":True}


class Login(BaseModel):
    email: str
    password: str


@app.post("/api/login")
def login(payload: Login, request: Request):
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE lower(email)=lower(?) AND active=1", (payload.email.strip(),)).fetchone()
    locked, locked_until = login_lock_state(row) if row else (False, None)
    if locked:
        raise HTTPException(423, "This account is temporarily locked after repeated sign-in attempts. Try again later or contact an administrator.")
    if not row or not verify_password(payload.password, row["password_hash"]):
        note_login_failure(payload.email)
        raise HTTPException(401, "Email or password not recognized")
    if bool(row["mfa_enabled"]):
        request.session.clear(); request.session["pending_mfa_user_id"] = row["id"]
        return {"ok": False, "mfa_required": True}
    request.session.clear(); request.session["user_id"] = row["id"]; request.session["csrf_token"] = secrets.token_urlsafe(24)
    complete_login(row["id"])
    return {"ok": True, "mfa_required": False}


class LoginMfa(BaseModel):
    code: str


@app.post("/api/login/mfa")
def login_mfa(payload: LoginMfa, request: Request):
    uid = request.session.get("pending_mfa_user_id")
    if not uid:
        raise HTTPException(400, "Start sign-in again")
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    if not row or not verify_totp(row["organization_id"], row["id"], payload.code):
        raise HTTPException(401, "Authenticator code did not match")
    request.session.clear(); request.session["user_id"] = row["id"]; request.session["csrf_token"] = secrets.token_urlsafe(24)
    complete_login(row["id"])
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    u = require_user(request)
    return {**{k: u[k] for k in ["id", "name", "email", "role", "organization_id", "organization_name", "organization_profile", "is_demo"]}, "force_password_change": bool(u.get("force_password_change")), "csrf_token": request.session.get("csrf_token") or ""}


@app.get("/api/workspace/readiness")
def workspace_readiness(request: Request):
    """Return practical next steps for a real workspace without exposing demo data."""
    u = require_user(request)
    org_id = u["organization_id"]
    profile = u["organization_profile"]
    with connect() as conn:
        customers = conn.execute("SELECT COUNT(*) FROM customers WHERE organization_id=?", (org_id,)).fetchone()[0]
        team = conn.execute("SELECT COUNT(*) FROM users WHERE organization_id=? AND active=1", (org_id,)).fetchone()[0]
        services = conn.execute("SELECT COUNT(*) FROM service_catalog WHERE organization_id=? AND active=1", (org_id,)).fetchone()[0]
        assets_table = "pets" if profile == "grooming" else "equipment"
        assets = conn.execute(f"SELECT COUNT(*) FROM {assets_table} WHERE organization_id=?", (org_id,)).fetchone()[0]
    from .cloud_connectors import organization_database_backup_is_safe_for_offsite
    backup_allowed, _ = organization_database_backup_is_safe_for_offsite(org_id)
    backups = list(BACKUPS_DIR.glob("ServiceSlate-Backup-*.zip")) if backup_allowed else []
    items = [
        {"id": "customers", "label": "Add your first customer", "detail": "Start building a trusted service history.", "complete": customers > 0, "action": "customers"},
        {"id": "team", "label": "Add your team", "detail": "Give each person only the access they need.", "complete": team > 1, "action": "team"},
        {"id": "services", "label": "Tailor your service catalog", "detail": "Make new work and appointments faster to create.", "complete": services > 0, "action": "services"},
        {"id": "records", "label": "Add your first " + ("pet" if profile == "grooming" else "equipment record"), "detail": "Keep service context attached to the right record.", "complete": assets > 0, "action": "pets" if profile == "grooming" else "equipment"},
        {"id": "backup", "label": "Create a first backup", "detail": "Protect real business records before they grow.", "complete": bool(backups), "action": "system"},
    ]
    return {"is_demo": bool(u["is_demo"]), "complete": sum(item["complete"] for item in items), "total": len(items), "items": items}


@app.get("/api/demo-accounts")
def demo_accounts():
    with connect() as conn:
        rows = conn.execute(
            """SELECT u.email,u.name,u.role,o.name organization_name,o.profile FROM users u
               JOIN organizations o ON o.id=u.organization_id WHERE o.is_demo=1 ORDER BY o.name,u.role"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/dashboard")
def dashboard(request: Request):
    u = require_user(request)
    org = u["organization_id"]
    if u["organization_profile"] == "grooming":
        with connect() as conn:
            appts = [dict(r) for r in conn.execute(
                """SELECT a.*, c.name customer_name, p.name pet_name, p.breed, u.name groomer_name
                   FROM grooming_appointments a JOIN customers c ON c.id=a.customer_id
                   JOIN pets p ON p.id=a.pet_id LEFT JOIN users u ON u.id=a.groomer_user_id
                   WHERE a.organization_id=? AND substr(a.start_at,1,10)>=? AND a.status NOT IN ('COMPLETED','CANCELED','NO_SHOW') ORDER BY a.start_at""", (org,date.today().isoformat())).fetchall()]
            counts = {
                "appointments": len(appts),
                "forms_incomplete": sum(1 for a in appts if not a["forms_complete"]),
                "rebooking_due": conn.execute("SELECT COUNT(*) FROM rebooking_obligations WHERE organization_id=? AND status='DUE'", (org,)).fetchone()[0],
            }
        return {"profile": "grooming", "counts": counts, "appointments": appts}
    with connect() as conn:
        jobs = [dict(r) for r in conn.execute(
            """SELECT j.*,c.name customer_name,l.name location_name,l.city,l.latitude,l.longitude,
                      u.name owner_name,e.manufacturer,e.model,e.serial_number
               FROM jobs j JOIN customers c ON c.id=j.customer_id JOIN locations l ON l.id=j.location_id
               LEFT JOIN users u ON u.id=j.owner_user_id LEFT JOIN equipment e ON e.id=j.equipment_id
               WHERE j.organization_id=? ORDER BY CASE j.priority WHEN 'HIGH' THEN 0 ELSE 1 END,j.due_date""", (org,)).fetchall()]
        reviews = conn.execute("SELECT COUNT(*) FROM work_submissions WHERE organization_id=? AND state='SUBMITTED'", (org,)).fetchone()[0]
        fastfield_review = conn.execute("SELECT COUNT(*) FROM import_batches WHERE organization_id=? AND source_type='FASTFIELD_EMAIL' AND status='STAGED'", (org,)).fetchone()[0]
        fastfield_batches = [dict(r) for r in conn.execute("SELECT id,filename,ready_count,issue_count,created_at FROM import_batches WHERE organization_id=? AND source_type='FASTFIELD_EMAIL' AND status='STAGED' ORDER BY created_at DESC LIMIT 5", (org,)).fetchall()]
    categories = {
        "new_requests": [j for j in jobs if j["status"] in ("NEW_REQUEST","TRIAGE")],
        "needs_scheduling": [j for j in jobs if j["status"] == "APPROVED_UNSCHEDULED"],
        "blocked": [j for j in jobs if j["status"] == "BLOCKED"],
        "work_complete": [j for j in jobs if j["status"] == "WORK_COMPLETE"],
    }
    return {"profile":"automotive_equipment","counts":{k:len(v) for k,v in categories.items()}|{"reviews":reviews,"fastfield_review":fastfield_review},"queues":categories,"fastfield_batches":fastfield_batches}


@app.get("/api/customers")
def customers(request: Request, q: str = ""):
    u = require_user(request); org=u["organization_id"]
    like=f"%{q.strip()}%"
    with connect() as conn:
        rows=conn.execute("""SELECT c.*,COUNT(DISTINCT l.id) location_count,COUNT(DISTINCT e.id) equipment_count
            FROM customers c LEFT JOIN locations l ON l.customer_id=c.id LEFT JOIN equipment e ON e.customer_id=c.id
            WHERE c.organization_id=? AND (c.name LIKE ? OR c.phone LIKE ? OR c.email LIKE ?) GROUP BY c.id ORDER BY c.name""",
            (org,like,like,like)).fetchall()
    return [dict(r) for r in rows]


class CustomerCreate(BaseModel):
    command_id:str|None=None; name:str; phone:str|None=None; email:str|None=None; website:str|None=None; business_type:str|None=None
    address1:str|None=None; city:str|None=None; state:str|None=None; postal_code:str|None=None
    latitude:float|None=None; longitude:float|None=None

@app.post("/api/customers")
def create_customer(payload:CustomerCreate, request:Request):
    u=require_user(request); org=u["organization_id"]
    with connect() as conn:
        def do():
            now=utcnow(); cid=new_id("cust"); lid=new_id("loc")
            dup=conn.execute("SELECT id,name FROM customers WHERE organization_id=? AND lower(name)=lower(?)",(org,payload.name.strip())).fetchone()
            if dup: raise HTTPException(409,f"Possible duplicate: {dup['name']}")
            conn.execute("INSERT INTO customers(id,organization_id,name,phone,email,website,business_type,data_origin,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cid,org,payload.name.strip(),payload.phone,payload.email,payload.website,payload.business_type,"manual",now,now))
            conn.execute("INSERT INTO locations(id,organization_id,customer_id,name,address1,city,state,postal_code,latitude,longitude,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (lid,org,cid,"Primary Location",payload.address1,payload.city,payload.state,payload.postal_code,payload.latitude,payload.longitude,now,now))
            audit(conn,org,u["id"],"customer",cid,"CREATED",f"Created customer {payload.name}")
            return {"id":cid,"location_id":lid}
        return command_once(conn,org,payload.command_id,do) if payload.command_id else do()


@app.get("/api/customers/{customer_id}")
def customer_detail(customer_id:str, request:Request):
    u=require_user(request); org=u["organization_id"]
    with connect() as conn:
        c=conn.execute("SELECT * FROM customers WHERE id=? AND organization_id=?",(customer_id,org)).fetchone()
        if not c: raise HTTPException(404,"Customer not found")
        loc=[dict(r) for r in conn.execute("SELECT * FROM locations WHERE customer_id=? ORDER BY name",(customer_id,)).fetchall()]
        eq=[dict(r) for r in conn.execute("SELECT * FROM equipment WHERE customer_id=? ORDER BY category,manufacturer,model",(customer_id,)).fetchall()]
        if u["role"]=="TECHNICIAN":
            jobs=[dict(r) for r in conn.execute("SELECT * FROM jobs WHERE customer_id=? AND status IN ('WORK_COMPLETE','READY_TO_INVOICE','INVOICED','CLOSED') ORDER BY created_at DESC",(customer_id,)).fetchall()]
            events=[]
        else:
            jobs=[dict(r) for r in conn.execute("SELECT * FROM jobs WHERE customer_id=? ORDER BY created_at DESC",(customer_id,)).fetchall()]
            events=[dict(r) for r in conn.execute("SELECT * FROM audit_events WHERE organization_id=? AND (entity_id=? OR entity_id IN (SELECT id FROM jobs WHERE customer_id=?)) ORDER BY id DESC LIMIT 100",(org,customer_id,customer_id)).fetchall()]
    return {"customer":dict(c),"locations":loc,"equipment":eq,"jobs":jobs,"history":events}


@app.get("/api/customers/{customer_id}/field-history")
def customer_field_history(customer_id:str, request:Request):
    u=require_user(request); org=u["organization_id"]
    if u["role"] not in ("TECHNICIAN","ADMIN","MANAGER","COORDINATOR"):
        raise HTTPException(403,"Field history access required")
    with connect() as conn:
        c=conn.execute("SELECT id,name,business_type FROM customers WHERE id=? AND organization_id=?",(customer_id,org)).fetchone()
        if not c: raise HTTPException(404,"Customer not found")
        eq=[dict(r) for r in conn.execute("""SELECT e.id,e.category,e.manufacturer,e.model,e.serial_number,e.bay,l.name location_name
            FROM equipment e JOIN locations l ON l.id=e.location_id WHERE e.customer_id=? ORDER BY l.name,e.category,e.manufacturer,e.model""",(customer_id,)).fetchall()]
        hist=[dict(r) for r in conn.execute("""SELECT j.id job_id,j.job_number,j.description,j.job_type,e.manufacturer,e.model,e.serial_number,e.bay,
            w.finding,w.cause,w.correction,w.verification,w.outcome,COALESCE(w.reviewed_at,w.submitted_at) completed_at,COALESCE(w.performed_by_text,u.name) technician_name
            FROM work_submissions w JOIN jobs j ON j.id=w.job_id LEFT JOIN equipment e ON e.id=j.equipment_id JOIN users u ON u.id=w.technician_user_id
            WHERE j.organization_id=? AND j.customer_id=? AND w.state IN ('APPROVED','LOCKED') ORDER BY COALESCE(w.reviewed_at,w.submitted_at) DESC LIMIT 60""",(org,customer_id)).fetchall()]
    return {"customer":dict(c),"equipment":eq,"approved_history":hist}


@app.get("/api/equipment")
def equipment(request:Request,q:str=""):
    u=require_user(request); org=u["organization_id"]; like=f"%{q}%"
    with connect() as conn:
        rows=conn.execute("""SELECT e.*,c.name customer_name,l.name location_name,l.city
            FROM equipment e JOIN customers c ON c.id=e.customer_id JOIN locations l ON l.id=e.location_id
            WHERE e.organization_id=? AND (e.serial_number LIKE ? OR e.model LIKE ? OR e.manufacturer LIKE ? OR c.name LIKE ?)
            ORDER BY c.name,e.category""",(org,like,like,like,like)).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/equipment/{equipment_id}/history")
def equipment_history(equipment_id:str,request:Request):
    u=require_user(request); org=u["organization_id"]
    with connect() as conn:
        e=conn.execute("""SELECT e.*,c.name customer_name,l.name location_name FROM equipment e JOIN customers c ON c.id=e.customer_id JOIN locations l ON l.id=e.location_id WHERE e.id=? AND e.organization_id=?""",(equipment_id,org)).fetchone()
        if not e: raise HTTPException(404,"Equipment not found")
        if u["role"]=="TECHNICIAN":
            jobs=[dict(r) for r in conn.execute("""SELECT j.* FROM jobs j WHERE j.equipment_id=? AND j.status IN ('READY_TO_INVOICE','INVOICED','CLOSED')
                AND EXISTS (SELECT 1 FROM work_submissions w WHERE w.job_id=j.id AND w.state IN ('APPROVED','LOCKED')) ORDER BY j.created_at DESC""",(equipment_id,)).fetchall()]
        else:
            jobs=[dict(r) for r in conn.execute("SELECT * FROM jobs WHERE equipment_id=? ORDER BY created_at DESC",(equipment_id,)).fetchall()]
        subs=[dict(r) for r in conn.execute("""SELECT w.*,j.job_number,j.description,COALESCE(w.performed_by_text,u.name) technician_name FROM work_submissions w JOIN jobs j ON j.id=w.job_id JOIN users u ON u.id=w.technician_user_id WHERE j.equipment_id=? AND w.state IN ('APPROVED','LOCKED') ORDER BY COALESCE(w.reviewed_at,w.submitted_at) DESC""",(equipment_id,)).fetchall()]
    for s in subs:
        s["measurements"]=json.loads(s.pop("measurements_json") or "[]"); s["parts"]=json.loads(s.pop("parts_json") or "[]")
    return {"equipment":dict(e),"jobs":jobs,"approved_work":subs}


@app.get("/api/jobs")
def jobs(request:Request,status:str=""):
    u=require_user(request); org=u["organization_id"]
    where="j.organization_id=?"; args:[Any]=[org]
    if u["role"]=="TECHNICIAN":
        where += " AND (EXISTS (SELECT 1 FROM visits vv WHERE vv.job_id=j.id AND (vv.technician_user_id=? OR vv.helper_user_id=?)) OR (j.status IN ('READY_TO_INVOICE','INVOICED','CLOSED') AND EXISTS (SELECT 1 FROM work_submissions ww WHERE ww.job_id=j.id AND ww.state IN ('APPROVED','LOCKED'))))"
        args.extend([u["id"],u["id"]])
    if status: where+=" AND j.status=?"; args.append(status)
    with connect() as conn:
        rows=conn.execute(f"""SELECT j.*,c.name customer_name,l.name location_name,l.city,u.name owner_name,
            e.manufacturer,e.model,e.serial_number FROM jobs j JOIN customers c ON c.id=j.customer_id JOIN locations l ON l.id=j.location_id
            LEFT JOIN users u ON u.id=j.owner_user_id LEFT JOIN equipment e ON e.id=j.equipment_id WHERE {where} ORDER BY j.updated_at DESC""",args).fetchall()
    return [dict(r) for r in rows]


class JobCreate(BaseModel):
    command_id:str|None=None; customer_id:str; location_id:str; equipment_id:str|None=None; job_type:str="Service Call"; description:str
    priority:str="NORMAL"; estimated_minutes:int=60; crew_min:int=1; crew_recommended:int=1; simultaneous_crew_minutes:int=0
    qualification_required:str|None=None; commitment_type:str="FLEXIBLE_DAY"; due_date:str|None=None

@app.post("/api/jobs")
def create_job(p:JobCreate,request:Request):
    u=require_user(request); org=u["organization_id"]
    with connect() as conn:
        def do():
            valid=conn.execute("SELECT 1 FROM customers WHERE id=? AND organization_id=?",(p.customer_id,org)).fetchone()
            if not valid: raise HTTPException(400,"Customer not in organization")
            loc=conn.execute("SELECT 1 FROM locations WHERE id=? AND customer_id=? AND organization_id=?",(p.location_id,p.customer_id,org)).fetchone()
            if not loc: raise HTTPException(400,"Choose a location that belongs to this customer")
            if p.equipment_id and not conn.execute("SELECT 1 FROM equipment WHERE id=? AND customer_id=? AND location_id=? AND organization_id=?",(p.equipment_id,p.customer_id,p.location_id,org)).fetchone():
                raise HTTPException(400,"Choose equipment that belongs to this customer and location")
            n=conn.execute("SELECT COUNT(*) FROM jobs WHERE organization_id=?",(org,)).fetchone()[0]+1
            jid=new_id("job"); jobnum=f"WO-{date.today().strftime('%y')}{100+n:03d}"; now=utcnow()
            conn.execute("""INSERT INTO jobs(id,organization_id,customer_id,location_id,equipment_id,job_number,job_type,description,status,priority,owner_user_id,next_action,due_date,estimated_minutes,crew_min,crew_recommended,simultaneous_crew_minutes,qualification_required,parts_status,commitment_type,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (jid,org,p.customer_id,p.location_id,p.equipment_id,jobnum,p.job_type,p.description,"NEW_REQUEST",p.priority,u["id"],"Review request",p.due_date or str(date.today()),p.estimated_minutes,p.crew_min,p.crew_recommended,p.simultaneous_crew_minutes,p.qualification_required,"READY",p.commitment_type,now,now))
            audit(conn,org,u["id"],"job",jid,"CREATED",f"Created {jobnum}: {p.description}")
            return {"id":jid,"job_number":jobnum}
        return command_once(conn,org,p.command_id,do) if p.command_id else do()


class JobUpdate(BaseModel):
    version:int; status:str|None=None; owner_user_id:str|None=None; next_action:str|None=None; due_date:str|None=None; parts_status:str|None=None; blocked_reason:str|None=None

@app.patch("/api/jobs/{job_id}")
def update_job(job_id:str,p:JobUpdate,request:Request):
    u=require_user(request); org=u["organization_id"]
    if u["role"] not in ("ADMIN", "MANAGER", "COORDINATOR"):
        raise HTTPException(403, "Office permission required to update jobs")
    with connect() as conn:
        row=conn.execute("SELECT * FROM jobs WHERE id=? AND organization_id=?",(job_id,org)).fetchone()
        if not row: raise HTTPException(404,"Job not found")
        if row["version"]!=p.version: raise HTTPException(409,"This job changed while you were working. Reload and review the latest version.")
        before=dict(row); fields=[]; vals=[]
        for key,val in p.model_dump(exclude={"version"}).items():
            if val is not None: fields.append(f"{key}=?"); vals.append(val)
        fields += ["version=version+1","updated_at=?"]; vals += [utcnow(),job_id,org,p.version]
        cur=conn.execute(f"UPDATE jobs SET {','.join(fields)} WHERE id=? AND organization_id=? AND version=?",vals)
        if cur.rowcount!=1: raise HTTPException(409,"This job changed while you were working")
        after=dict(conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone())
        audit(conn,org,u["id"],"job",job_id,"UPDATED",f"Updated {row['job_number']}",before,after)
    return after


def haversine(lat1,lon1,lat2,lon2):
    if None in (lat1,lon1,lat2,lon2): return 9999.0
    r=3958.8
    p1,p2=math.radians(lat1),math.radians(lat2); dphi=math.radians(lat2-lat1); dl=math.radians(lon2-lon1)
    a=math.sin(dphi/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))

@app.get("/api/compass")
def compass(request:Request):
    u=require_user(request); org=u["organization_id"]
    if u["organization_profile"]!="automotive_equipment": return {"suggestions":[], "duplication_warnings":[]}
    if u["role"] not in ("ADMIN","MANAGER","COORDINATOR"):
        raise HTTPException(403,"Scheduling recommendations are limited to office roles")
    with connect() as conn:
        techs=[dict(r) for r in conn.execute("SELECT id,name,qualifications_json FROM users WHERE organization_id=? AND role='TECHNICIAN' AND active=1",(org,)).fetchall()]
        candidates=[dict(r) for r in conn.execute("""SELECT j.*,c.name customer_name,l.city,l.latitude,l.longitude FROM jobs j JOIN customers c ON c.id=j.customer_id JOIN locations l ON l.id=j.location_id WHERE j.organization_id=? AND j.status='APPROVED_UNSCHEDULED' AND j.parts_status IN ('READY','RECEIVED')""",(org,)).fetchall()]
        visits=[dict(r) for r in conn.execute("""SELECT v.*,l.latitude,l.longitude,j.estimated_minutes,c.name customer_name FROM visits v JOIN jobs j ON j.id=v.job_id JOIN locations l ON l.id=j.location_id JOIN customers c ON c.id=j.customer_id WHERE v.organization_id=? AND v.execution_status NOT IN ('CANCELED')""",(org,)).fetchall()]
        branch_rows=[dict(r) for r in conn.execute("""SELECT v.assigned_user_id technician_user_id,b.name branch_name,b.latitude,b.longitude FROM vehicles v
            JOIN branches b ON b.organization_id=v.organization_id AND lower(b.name)=lower(v.branch)
            WHERE v.organization_id=? AND v.assigned_user_id IS NOT NULL AND v.availability='AVAILABLE'""",(org,)).fetchall()]
        remote_rows=[dict(r) for r in conn.execute("""SELECT technician_user_id,work_date,origin_label,latitude,longitude FROM remote_start_authorizations
            WHERE organization_id=? AND status='APPROVED'""",(org,)).fetchall()]
    branch_by_tech={r["technician_user_id"]:r for r in branch_rows}
    remote_by_tech_day={(r["technician_user_id"],r["work_date"]):r for r in remote_rows}
    out=[]
    for j in candidates:
        target_day=(j.get("due_date") or date.today().isoformat())[:10]
        day_visits=[v for v in visits if (v.get("start_at") or "")[:10]==target_day]
        scored=[]
        for t in techs:
            quals=json.loads(t["qualifications_json"] or "[]")
            req=j["qualification_required"]
            if req and req not in quals and "All" not in quals: continue
            tv=[v for v in day_visits if v["technician_user_id"]==t["id"] or v.get("helper_user_id")==t["id"]]
            minutes=0
            for v in tv:
                try:
                    vs=datetime.fromisoformat(v["start_at"]); ve=datetime.fromisoformat(v["end_at"]); minutes += max(0,int((ve-vs).total_seconds()/60))
                except (TypeError,ValueError):
                    minutes += int(v.get("estimated_minutes") or 0)
            if tv:
                nearest=min(haversine(j["latitude"],j["longitude"],v["latitude"],v["longitude"]) for v in tv)
                origin_kind="existing work"
            else:
                remote=remote_by_tech_day.get((t["id"],target_day))
                branch=branch_by_tech.get(t["id"])
                if remote and remote.get("latitude") is not None and remote.get("longitude") is not None:
                    nearest=haversine(j["latitude"],j["longitude"],remote["latitude"],remote["longitude"]); origin_kind="approved remote start"
                elif branch and branch.get("latitude") is not None and branch.get("longitude") is not None:
                    nearest=haversine(j["latitude"],j["longitude"],branch["latitude"],branch["longitude"]); origin_kind=f"{branch['branch_name']} branch"
                else:
                    nearest=12.0; origin_kind="normal branch"
            capacity_pen=max(0,(minutes+j["estimated_minutes"])-420)/30
            score=nearest+capacity_pen*8
            scored.append((score,t,nearest,minutes,origin_kind))
        scored.sort(key=lambda x:x[0])
        needed=max(1,j["crew_min"])
        if len(scored)>=needed:
            chosen=scored[:needed]
            names=[x[1]["name"] for x in chosen]
            reason=f"{', '.join(names)} fit the qualification and {target_day} workload requirements."
            if chosen[0][2]<5: reason+=f" Nearest {chosen[0][4]} that day is about {chosen[0][2]:.1f} mi away."
            elif chosen[0][4]=="approved remote start": reason+=" An approved remote-start origin was used for planning."
            if needed>1: reason+=f" This job requires {needed} technicians"
            if j["simultaneous_crew_minutes"]: reason+=f" for about {j['simultaneous_crew_minutes']} shared minutes."
            out.append({"job_id":j["id"],"job_number":j["job_number"],"customer":j["customer_name"],"description":j["description"],"target_day":target_day,"recommended_technicians":[x[1]["id"] for x in chosen],"recommended_names":names,"reason":reason,"crew":needed})
    # Only compare different technicians on the same workday; this is workload affinity, not tracking.
    dup=[]
    for i,a in enumerate(visits):
        for b in visits[i+1:]:
            if (a.get("start_at") or "")[:10] != (b.get("start_at") or "")[:10]: continue
            if a["technician_user_id"]==b["technician_user_id"]: continue
            d=haversine(a["latitude"],a["longitude"],b["latitude"],b["longitude"])
            if d<=3.0:
                dup.append({"work_date":a["start_at"][:10],"distance_miles":round(d,1),"customer_a":a["customer_name"],"customer_b":b["customer_name"]})
    return {"suggestions":out,"duplication_warnings":dup[:6]}


@app.get("/api/calendar")
def calendar(request:Request):
    u=require_user(request); org=u["organization_id"]
    with connect() as conn:
        techs=[dict(r) for r in conn.execute("SELECT id,name,qualifications_json FROM users WHERE organization_id=? AND role='TECHNICIAN' ORDER BY name",(org,)).fetchall()]
        visits=[dict(r) for r in conn.execute("""SELECT v.*,j.job_number,j.description,j.crew_min,c.name customer_name,l.city FROM visits v JOIN jobs j ON j.id=v.job_id JOIN customers c ON c.id=j.customer_id JOIN locations l ON l.id=j.location_id WHERE v.organization_id=? ORDER BY v.start_at""",(org,)).fetchall()]
        uns=[dict(r) for r in conn.execute("""SELECT j.*,c.name customer_name,l.city FROM jobs j JOIN customers c ON c.id=j.customer_id JOIN locations l ON l.id=j.location_id WHERE j.organization_id=? AND j.status='APPROVED_UNSCHEDULED' ORDER BY j.priority DESC,j.due_date""",(org,)).fetchall()]
    return {"technicians":techs,"visits":visits,"unscheduled":uns}

class ScheduleJob(BaseModel):
    command_id:str; job_id:str; technician_user_id:str; helper_user_id:str|None=None; start_at:str; end_at:str


def _has_visit_overlap(conn: sqlite3.Connection, org: str, user_id: str, start_at: str, end_at: str) -> bool:
    rows=conn.execute("""SELECT technician_user_id,helper_user_id,start_at,end_at,helper_end_at FROM visits
        WHERE organization_id=? AND execution_status!='CANCELED'
        AND (technician_user_id=? OR helper_user_id=?)""",(org,user_id,user_id)).fetchall()
    wanted_start=datetime.fromisoformat(start_at); wanted_end=datetime.fromisoformat(end_at)
    for row in rows:
        existing_start=datetime.fromisoformat(row["start_at"])
        existing_end=datetime.fromisoformat(row["end_at"])
        if row["helper_user_id"]==user_id and row["technician_user_id"]!=user_id and row["helper_end_at"]:
            existing_end=datetime.fromisoformat(row["helper_end_at"])
        if existing_start < wanted_end and existing_end > wanted_start:
            return True
    return False


@app.post("/api/schedule")
def schedule(p:ScheduleJob,request:Request):
    u=require_user(request); org=u["organization_id"]
    if u["role"] not in ("ADMIN","MANAGER","COORDINATOR"):
        raise HTTPException(403,"Scheduling permission required")
    with connect() as conn:
        def do():
            j=conn.execute("SELECT * FROM jobs WHERE id=? AND organization_id=?",(p.job_id,org)).fetchone()
            if not j: raise HTTPException(404,"Job not found")
            if j["status"] in ("CANCELED","CLOSED","INVOICED"):
                raise HTTPException(409,"This job is no longer schedulable")
            if j["crew_min"]>=2 and not p.helper_user_id: raise HTTPException(400,"This job requires two technicians")
            if p.helper_user_id and p.helper_user_id==p.technician_user_id:
                raise HTTPException(400,"Lead and helper must be different technicians")
            start_dt=datetime.fromisoformat(p.start_at); end_dt=datetime.fromisoformat(p.end_at)
            if end_dt<=start_dt: raise HTTPException(400,"End time must be after start time")
            helper_end_at=None
            if p.helper_user_id:
                overlap_minutes=int(j["simultaneous_crew_minutes"] or 0)
                if overlap_minutes>0 and overlap_minutes < int((end_dt-start_dt).total_seconds()/60):
                    helper_end_at=(start_dt+timedelta(minutes=overlap_minutes)).isoformat()
                else:
                    helper_end_at=p.end_at
            # A re-schedule replaces the active visit rather than stacking a second copy.
            prior_visits = conn.execute(
                "SELECT id FROM visits WHERE organization_id=? AND job_id=? AND execution_status!='CANCELED'",
                (org, p.job_id),
            ).fetchall()
            if prior_visits:
                replacement_at = utcnow()
                conn.execute("UPDATE visits SET execution_status='CANCELED',updated_at=? WHERE organization_id=? AND job_id=? AND execution_status!='CANCELED'", (replacement_at, org, p.job_id))
                conn.execute("UPDATE service_day_confirmations SET status='STALE',updated_at=? WHERE organization_id=? AND job_id=? AND status!='STALE'", (replacement_at, org, p.job_id))
            # Lead is committed for the whole visit; helper only for the simultaneous portion.
            if _has_visit_overlap(conn,org,p.technician_user_id,p.start_at,p.end_at):
                raise HTTPException(409,"The lead technician already has work in that time window")
            if p.helper_user_id and _has_visit_overlap(conn,org,p.helper_user_id,p.start_at,helper_end_at or p.end_at):
                raise HTTPException(409,"The helper already has work during the required shared-work window")
            vid=new_id("visit"); now=utcnow()
            conn.execute("""INSERT INTO visits(id,organization_id,job_id,technician_user_id,helper_user_id,start_at,end_at,helper_end_at,commitment_type,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(vid,org,p.job_id,p.technician_user_id,p.helper_user_id,p.start_at,p.end_at,helper_end_at,j["commitment_type"],now,now))
            conn.execute("UPDATE jobs SET status='SCHEDULED',next_action='Perform scheduled work',version=version+1,updated_at=? WHERE id=?",(now,p.job_id))
            action = "RESCHEDULED" if prior_visits else "SCHEDULED"
            audit(conn,org,u["id"],"job",p.job_id,action,f"{'Rescheduled' if prior_visits else 'Scheduled'} {j['job_number']}")
            return {"visit_id":vid,"helper_end_at":helper_end_at,"rescheduled":bool(prior_visits)}
        return command_once(conn,org,p.command_id,do)


@app.get("/api/tech/today")
def tech_today(request:Request):
    u=require_user(request); org=u["organization_id"]
    if u["role"] not in ("TECHNICIAN","GROOMER","ADMIN","MANAGER"): raise HTTPException(403,"Technician access required")
    if u["organization_profile"]=="grooming":
        with connect() as conn:
            rows=conn.execute("""SELECT a.*,c.name customer_name,p.name pet_name,p.breed,p.safety_notes FROM grooming_appointments a JOIN customers c ON c.id=a.customer_id JOIN pets p ON p.id=a.pet_id WHERE a.organization_id=? AND substr(a.start_at,1,10)>=? AND (a.groomer_user_id=? OR ? IN ('ADMIN','MANAGER')) ORDER BY a.start_at""",(org,date.today().isoformat(),u["id"],u["role"])).fetchall()
        return {"profile":"grooming","appointments":[dict(r) for r in rows]}
    with connect() as conn:
        rows=conn.execute("""SELECT v.*,j.job_number,j.description,j.status,j.job_type,j.equipment_id,j.crew_min,j.simultaneous_crew_minutes,c.name customer_name,l.name location_name,l.address1,l.city,e.manufacturer,e.model,e.serial_number,e.bay
            FROM visits v JOIN jobs j ON j.id=v.job_id JOIN customers c ON c.id=j.customer_id JOIN locations l ON l.id=j.location_id LEFT JOIN equipment e ON e.id=j.equipment_id
            WHERE v.organization_id=? AND substr(v.start_at,1,10)>=? AND v.execution_status!='CANCELED' AND (v.technician_user_id=? OR v.helper_user_id=? OR ? IN ('ADMIN','MANAGER','COORDINATOR')) ORDER BY v.start_at""",(org,date.today().isoformat(),u["id"],u["id"],u["role"])).fetchall()
    return {"profile":"automotive_equipment","visits":[dict(r) for r in rows]}

@app.get("/api/jobs/{job_id}/context")
def job_context(job_id:str,request:Request):
    u=require_user(request); org=u["organization_id"]
    with connect() as conn:
        j=conn.execute("""SELECT j.*,c.name customer_name,l.name location_name,l.address1,l.city,l.state,l.postal_code,l.area_label,l.access_notes,
            e.manufacturer,e.model,e.serial_number,e.bay,e.category equipment_category,e.notes equipment_notes,e.install_date,e.warranty_until,e.operational_status,e.inspection_due_date,e.inspection_interval_months
            FROM jobs j JOIN customers c ON c.id=j.customer_id JOIN locations l ON l.id=j.location_id LEFT JOIN equipment e ON e.id=j.equipment_id
            WHERE j.id=? AND j.organization_id=?""",(job_id,org)).fetchone()
        if not j: raise HTTPException(404,"Job not found")
        if u["role"]=="TECHNICIAN":
            assigned=conn.execute("SELECT 1 FROM visits WHERE job_id=? AND organization_id=? AND (technician_user_id=? OR helper_user_id=?) LIMIT 1",(job_id,org,u["id"],u["id"])).fetchone()
            if not assigned:
                raise HTTPException(403,"This live job is not assigned to you")
        hist=[]; similar=[]; recs=[]; files=[]; inspections=[]; inspection_templates=[]
        if j["equipment_id"]:
            hist=[dict(r) for r in conn.execute("""SELECT w.*,jj.job_number,jj.description,jj.job_type,COALESCE(w.performed_by_text,uu.name) technician_name
                FROM work_submissions w JOIN jobs jj ON jj.id=w.job_id JOIN users uu ON uu.id=w.technician_user_id
                WHERE jj.equipment_id=? AND w.state IN ('APPROVED','LOCKED') ORDER BY COALESCE(w.reviewed_at,w.submitted_at) DESC LIMIT 12""",(j["equipment_id"],)).fetchall()]
            if j["manufacturer"] or j["model"]:
                similar=[dict(r) for r in conn.execute("""SELECT w.id,w.finding,w.cause,w.correction,w.verification,w.outcome,jj.job_number,jj.description,ee.manufacturer,ee.model,COALESCE(w.performed_by_text,uu.name) technician_name,COALESCE(w.reviewed_at,w.submitted_at) completed_at
                    FROM work_submissions w JOIN jobs jj ON jj.id=w.job_id JOIN equipment ee ON ee.id=jj.equipment_id JOIN users uu ON uu.id=w.technician_user_id
                    WHERE jj.organization_id=? AND jj.equipment_id<>? AND w.state IN ('APPROVED','LOCKED')
                    AND (ee.model=? OR (ee.manufacturer=? AND ? IS NOT NULL))
                    ORDER BY COALESCE(w.reviewed_at,w.submitted_at) DESC LIMIT 8""",(org,j["equipment_id"],j["model"],j["manufacturer"],j["manufacturer"])).fetchall()]
            recs=[dict(r) for r in conn.execute("SELECT * FROM recommendations WHERE organization_id=? AND equipment_id=? AND status NOT IN ('CONVERTED','SUPERSEDED') ORDER BY created_at DESC",(org,j["equipment_id"])).fetchall()]
            files=[dict(r) for r in conn.execute("SELECT id,category,original_name,mime_type,size_bytes,created_at FROM file_records WHERE organization_id=? AND entity_type='equipment' AND entity_id=? ORDER BY created_at DESC",(org,j["equipment_id"])).fetchall()]
            inspections=[dict(r) for r in conn.execute("""SELECT i.id,i.inspection_date,i.result,i.summary,i.next_due_date,it.name template_name,u.name inspector_name
                FROM inspections i JOIN inspection_templates it ON it.id=i.template_id JOIN users u ON u.id=i.inspector_user_id
                WHERE i.organization_id=? AND i.equipment_id=? AND i.state='LOCKED' ORDER BY i.inspection_date DESC LIMIT 6""",(org,j["equipment_id"])).fetchall()]
            inspection_templates=[]
            for tr in conn.execute("""SELECT * FROM inspection_templates WHERE organization_id=? AND status='PUBLISHED'
                AND (equipment_category IS NULL OR equipment_category=?) ORDER BY name,version DESC""",(org,j["equipment_category"])).fetchall():
                td=dict(tr); td["items"]=json.loads(td.pop("items_json") or "[]"); inspection_templates.append(td)
        job_files=[dict(r) for r in conn.execute("SELECT id,category,original_name,mime_type,size_bytes,created_at FROM file_records WHERE organization_id=? AND entity_type='job' AND entity_id=? ORDER BY created_at DESC",(org,job_id)).fetchall()]
        contacts=[dict(r) for r in conn.execute("""SELECT name,title,phone,email,preferred_channel FROM contacts
            WHERE organization_id=? AND customer_id=? AND active=1 AND (location_id IS NULL OR location_id=?) ORDER BY CASE WHEN location_id=? THEN 0 ELSE 1 END,name LIMIT 6""",
            (org,j["customer_id"],j["location_id"],j["location_id"])).fetchall()]
    for h in hist:
        h["measurements"]=json.loads(h.pop("measurements_json") or "[]"); h["parts"]=json.loads(h.pop("parts_json") or "[]"); h["recommendations"]=json.loads(h.pop("recommendations_json") or "[]")
    return {"job":dict(j),"history":hist,"similar_history":similar,"open_recommendations":recs,"files":files+job_files,"contacts":contacts,"inspections":inspections,"inspection_templates":inspection_templates}


class Submission(BaseModel):
    command_id:str
    complaint:str|None=None
    finding:str|None=None
    cause:str|None=None
    correction:str|None=None
    verification:str|None=None
    internal_note:str|None=None
    customer_report:str|None=None
    outcome:str="COMPLETED"
    measurements:list[dict[str,Any]]=[]
    parts:list[dict[str,Any]]=[]
    recommendations:list[dict[str,Any]]=[]
    signature_name:str|None=None
    labor_minutes:int|None=None
    safety_restriction:str|None=None


@app.post("/api/jobs/{job_id}/submit-work")
def submit_work(job_id:str,p:Submission,request:Request):
    u=require_user(request); org=u["organization_id"]
    if u["role"] not in ("TECHNICIAN","MANAGER","ADMIN"):
        raise HTTPException(403,"Technician access required")
    with connect() as conn:
        def do():
            j=conn.execute("SELECT * FROM jobs WHERE id=? AND organization_id=?",(job_id,org)).fetchone()
            if not j: raise HTTPException(404,"Job not found")
            if u["role"] == "TECHNICIAN":
                assignment = conn.execute(
                    """SELECT 1 FROM visits WHERE organization_id=? AND job_id=?
                       AND (technician_user_id=? OR helper_user_id=?) LIMIT 1""",
                    (org, job_id, u["id"], u["id"]),
                ).fetchone()
                if not assignment:
                    raise HTTPException(403, "You can submit work only for a job assigned to you")
            active = conn.execute("SELECT id,state FROM work_submissions WHERE job_id=? AND technician_user_id=? AND state IN ('SUBMITTED','NEEDS_CORRECTION') ORDER BY created_at DESC LIMIT 1", (job_id,u["id"])).fetchone()
            if active:
                return {"submission_id": active["id"], "state": active["state"], "already_exists": True}
            sid=new_id("sub"); now=utcnow()
            conn.execute("""INSERT INTO work_submissions(id,organization_id,job_id,technician_user_id,state,complaint,finding,cause,correction,verification,internal_note,customer_report,outcome,measurements_json,parts_json,recommendations_json,signature_name,signature_at,labor_minutes,safety_restriction,submitted_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid,org,job_id,u["id"],"SUBMITTED",p.complaint,p.finding,p.cause,p.correction,p.verification,p.internal_note,p.customer_report,p.outcome,json.dumps(p.measurements),json.dumps(p.parts),json.dumps(p.recommendations),p.signature_name,now if p.signature_name else None,p.labor_minutes,p.safety_restriction,now,now,now))
            conn.execute("UPDATE jobs SET status='WORK_COMPLETE',next_action='Coordinator review',safety_status=CASE WHEN ?='SAFETY_RESTRICTION' THEN 'RESTRICTED' ELSE safety_status END,version=version+1,updated_at=? WHERE id=?",(p.outcome,now,job_id))
            audit(conn,org,u["id"],"job",job_id,"TECHNICIAN_SUBMITTED",f"Technician submitted work for {j['job_number']}")
            return {"submission_id":sid,"state":"SUBMITTED"}
        return command_once(conn,org,p.command_id,do)


@app.get("/api/reviews")
def reviews(request:Request):
    u=require_user(request); org=u["organization_id"]
    if u["role"] not in ("ADMIN", "MANAGER", "COORDINATOR"):
        raise HTTPException(403, "Review permission required")
    with connect() as conn:
        rows=conn.execute("""SELECT w.*,j.job_number,j.description,c.name customer_name,COALESCE(w.performed_by_text,uu.name) technician_name FROM work_submissions w JOIN jobs j ON j.id=w.job_id JOIN customers c ON c.id=j.customer_id JOIN users uu ON uu.id=w.technician_user_id WHERE w.organization_id=? AND w.state IN ('SUBMITTED','NEEDS_CORRECTION') ORDER BY w.submitted_at""",(org,)).fetchall()
    out=[]
    for r in rows:
        d=dict(r)
        d["measurements"]=json.loads(d.pop("measurements_json") or "[]")
        d["parts"]=json.loads(d.pop("parts_json") or "[]")
        d["recommendations"]=json.loads(d.pop("recommendations_json") or "[]")
        missing=[f for f in ("finding","correction","verification") if not d.get(f)]
        warnings=[]
        if d.get("outcome")=="SAFETY_RESTRICTION" and not d.get("safety_restriction"):
            warnings.append("Safety restriction details are missing")
        if d.get("outcome")=="NEED_PARTS" and not d.get("parts"):
            warnings.append("Outcome says parts are needed but no parts were listed")
        if d.get("outcome")=="ADDITIONAL_AUTHORIZATION_REQUIRED" and not d.get("customer_report"):
            warnings.append("Customer-facing explanation is missing")
        d["validation"]={"clean":not missing and not warnings,"missing":missing,"warnings":warnings}
        out.append(d)
    return out


class ReviewAction(BaseModel):
    command_id:str
    action:str
    note:str|None=None


@app.post("/api/reviews/{submission_id}")
def review_submission(submission_id:str,p:ReviewAction,request:Request):
    u=require_user(request); org=u["organization_id"]
    if u["role"] not in ("COORDINATOR","MANAGER","ADMIN"):
        raise HTTPException(403,"Coordinator review permission required")
    with connect() as conn:
        def do():
            w=conn.execute("SELECT * FROM work_submissions WHERE id=? AND organization_id=?",(submission_id,org)).fetchone()
            if not w: raise HTTPException(404,"Submission not found")
            now=utcnow()
            if p.action=="approve":
                conn.execute("UPDATE work_submissions SET state='LOCKED',reviewed_by_user_id=?,reviewed_at=?,review_note=?,version=version+1,updated_at=? WHERE id=?",(u["id"],now,p.note,now,submission_id))
                outcome=w["outcome"] or "COMPLETED"
                if outcome in ("COMPLETED","UNABLE_TO_REPRODUCE"):
                    job_status,next_action="READY_TO_INVOICE","Billing review"
                elif outcome in ("NEED_RETURN_VISIT","NEED_SECOND_TECHNICIAN","NEED_MORE_DIAGNOSTIC_TIME","CUSTOMER_UNAVAILABLE","EQUIPMENT_INACCESSIBLE"):
                    job_status,next_action="APPROVED_UNSCHEDULED","Schedule return visit"
                elif outcome=="ADDITIONAL_AUTHORIZATION_REQUIRED":
                    job_status,next_action="AWAITING_CUSTOMER_APPROVAL","Obtain customer authorization"
                else:
                    job_status,next_action="BLOCKED","Resolve technician-reported blocker"
                crew_sql=""
                if outcome=="NEED_SECOND_TECHNICIAN":
                    crew_sql=",crew_min=CASE WHEN crew_min<2 THEN 2 ELSE crew_min END,crew_recommended=CASE WHEN crew_recommended<2 THEN 2 ELSE crew_recommended END"
                safety_sql=""
                if outcome=="SAFETY_RESTRICTION":
                    safety_sql=",safety_status='RESTRICTED'"
                conn.execute(f"UPDATE jobs SET status=?,next_action=?{crew_sql}{safety_sql},version=version+1,updated_at=? WHERE id=?",(job_status,next_action,now,w["job_id"]))

                # Promote approved technician-entered parts and recommendations into structured history once.
                source=f"submission:{submission_id}"
                for part in json.loads(w["parts_json"] or "[]"):
                    desc=(part.get("part") or part.get("description") or part.get("name") or "Part used").strip()
                    qty=float(part.get("qty") or part.get("quantity") or 1)
                    exists=conn.execute("SELECT 1 FROM job_parts WHERE job_id=? AND source=? AND description=?",(w["job_id"],source,desc)).fetchone()
                    if not exists:
                        conn.execute("INSERT INTO job_parts(id,organization_id,job_id,description,quantity,status,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",(new_id("jp"),org,w["job_id"],desc,qty,"INSTALLED",source,now,now))
                job=conn.execute("SELECT equipment_id FROM jobs WHERE id=?",(w["job_id"],)).fetchone()
                for rec in json.loads(w["recommendations_json"] or "[]"):
                    summary=(rec.get("summary") or rec.get("text") or "").strip()
                    if not summary: continue
                    exists=conn.execute("SELECT 1 FROM recommendations WHERE job_id=? AND summary=?",(w["job_id"],summary)).fetchone()
                    if not exists:
                        conn.execute("INSERT INTO recommendations(id,organization_id,job_id,equipment_id,created_by_user_id,summary,urgency,status,details,followup_due,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(new_id("rec"),org,w["job_id"],job["equipment_id"] if job else None,w["technician_user_id"],summary,rec.get("urgency") or "RECOMMENDED","NEW",rec.get("details"),rec.get("followup_due"),now))
                audit(conn,org,u["id"],"job",w["job_id"],"TECHNICIAN_WORK_APPROVED",f"Coordinator approved technician paperwork; outcome {outcome}")
                return {"state":"LOCKED","job_status":job_status}
            if p.action=="return":
                conn.execute("UPDATE work_submissions SET state='NEEDS_CORRECTION',reviewed_by_user_id=?,reviewed_at=?,review_note=?,version=version+1,updated_at=? WHERE id=?",(u["id"],now,p.note or "Correction requested",now,submission_id))
                conn.execute("UPDATE jobs SET next_action='Technician correction required',version=version+1,updated_at=? WHERE id=?",(now,w["job_id"]))
                audit(conn,org,u["id"],"job",w["job_id"],"TECHNICIAN_CORRECTION_REQUESTED",p.note or "Correction requested")
                return {"state":"NEEDS_CORRECTION"}
            raise HTTPException(400,"Unknown review action")
        return command_once(conn,org,p.command_id,do)

@app.get("/api/vehicles")
def vehicles(request:Request):
    u=require_user(request); org=u["organization_id"]
    with connect() as conn:
        rows=conn.execute("SELECT v.*,u.name assigned_technician FROM vehicles v LEFT JOIN users u ON u.id=v.assigned_user_id WHERE v.organization_id=? ORDER BY unit_number",(org,)).fetchall()
    out=[]
    for r in rows:
        d=dict(r); d["soft_inventory"]=json.loads(d.pop("soft_inventory_json") or "{}"); out.append(d)
    return out

@app.get("/api/search")
def search(request:Request,q:str):
    u=require_user(request); org=u["organization_id"]; q=q.strip(); like=f"%{q}%"
    if not q: return []
    with connect() as conn:
        cust=[dict(r)|{"type":"customer"} for r in conn.execute("SELECT id,name label,coalesce(phone,'') subtitle FROM customers WHERE organization_id=? AND (name LIKE ? OR phone LIKE ? OR email LIKE ?) LIMIT 10",(org,like,like,like)).fetchall()]
        contacts=[]
        if u["role"] != "TECHNICIAN":
            contacts=[dict(r)|{"type":"contact"} for r in conn.execute("SELECT id,name label,coalesce(title,'')||CASE WHEN phone IS NOT NULL THEN ' · '||phone ELSE '' END subtitle,customer_id FROM contacts WHERE organization_id=? AND active=1 AND (name LIKE ? OR phone LIKE ? OR email LIKE ?) LIMIT 8",(org,like,like,like)).fetchall()]
        if u["organization_profile"]=="automotive_equipment":
            eq=[dict(r)|{"type":"equipment"} for r in conn.execute("SELECT id,trim(coalesce(manufacturer,'')||' '||coalesce(model,'')) label,coalesce(serial_number,'') subtitle FROM equipment WHERE organization_id=? AND (serial_number LIKE ? OR model LIKE ? OR manufacturer LIKE ? OR notes LIKE ?) LIMIT 12",(org,like,like,like,like)).fetchall()]
            job_where="j.organization_id=? AND (j.job_number LIKE ? OR j.description LIKE ?)"; args=[org,like,like]
            if u["role"]=="TECHNICIAN":
                job_where += " AND ((j.status IN ('READY_TO_INVOICE','INVOICED','CLOSED') AND EXISTS (SELECT 1 FROM work_submissions ww WHERE ww.job_id=j.id AND ww.state IN ('APPROVED','LOCKED'))) OR EXISTS (SELECT 1 FROM visits v WHERE v.job_id=j.id AND (v.technician_user_id=? OR v.helper_user_id=?)))"
                args += [u["id"],u["id"]]
            jobs=[dict(r)|{"type":"job"} for r in conn.execute(f"SELECT j.id,j.job_number||' — '||j.description label,j.status subtitle FROM jobs j WHERE {job_where} LIMIT 12",args).fetchall()]
            history=[dict(r)|{"type":"history"} for r in conn.execute("""SELECT j.id,j.job_number||' — '||coalesce(w.finding,j.description) label,
                trim(coalesce(w.correction,'')||CASE WHEN w.outcome IS NOT NULL THEN ' · '||w.outcome ELSE '' END) subtitle
                FROM work_submissions w JOIN jobs j ON j.id=w.job_id WHERE j.organization_id=? AND w.state IN ('APPROVED','LOCKED')
                AND (w.finding LIKE ? OR w.cause LIKE ? OR w.correction LIKE ? OR w.verification LIKE ? OR w.customer_report LIKE ? OR j.description LIKE ?)
                ORDER BY COALESCE(w.reviewed_at,w.submitted_at) DESC LIMIT 10""",(org,like,like,like,like,like,like)).fetchall()]
            parts=[dict(r)|{"type":"part"} for r in conn.execute("SELECT id,name label,trim(coalesce(sku,'')||CASE WHEN manufacturer IS NOT NULL THEN ' · '||manufacturer ELSE '' END) subtitle FROM parts WHERE organization_id=? AND active=1 AND (name LIKE ? OR sku LIKE ? OR manufacturer_part_number LIKE ?) LIMIT 8",(org,like,like,like)).fetchall()]
            return cust+contacts+eq+jobs+history+parts
        pets=[dict(r)|{"type":"pet"} for r in conn.execute("SELECT p.id,p.name label,coalesce(p.breed,'')||' · '||c.name subtitle FROM pets p JOIN customers c ON c.id=p.customer_id WHERE p.organization_id=? AND (p.name LIKE ? OR p.breed LIKE ? OR c.name LIKE ?) LIMIT 12",(org,like,like,like)).fetchall()]
        return cust+contacts+pets


@app.get("/api/grooming/pets")
def grooming_pets(request:Request):
    u=require_user(request); org=u["organization_id"]
    if u["organization_profile"]!="grooming": raise HTTPException(404)
    with connect() as conn:
        rows=conn.execute("SELECT p.*,c.name customer_name,u.name preferred_groomer_name FROM pets p JOIN customers c ON c.id=p.customer_id LEFT JOIN users u ON u.id=p.preferred_groomer_user_id WHERE p.organization_id=? ORDER BY p.name",(org,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/grooming/groomers")
def grooming_groomers(request: Request):
    u = require_user(request); org = u["organization_id"]
    if u["organization_profile"] != "grooming": raise HTTPException(404)
    with connect() as conn:
        rows = conn.execute("SELECT id,name,qualifications_json FROM users WHERE organization_id=? AND role='GROOMER' AND active=1 ORDER BY name", (org,)).fetchall()
    return [dict(r) for r in rows]


class GroomingAppointmentCreate(BaseModel):
    command_id: str
    pet_id: str
    groomer_user_id: str
    service_name: str
    start_at: str
    end_at: str
    forms_complete: bool = False


@app.post("/api/grooming/appointments")
def create_grooming_appointment(p: GroomingAppointmentCreate, request: Request):
    u = require_user(request); org = u["organization_id"]
    if u["organization_profile"] != "grooming": raise HTTPException(404)
    with connect() as conn:
        def do():
            pet = conn.execute("SELECT * FROM pets WHERE id=? AND organization_id=?", (p.pet_id, org)).fetchone()
            if not pet: raise HTTPException(404, "Pet not found")
            groomer = conn.execute("SELECT 1 FROM users WHERE id=? AND organization_id=? AND role='GROOMER' AND active=1", (p.groomer_user_id, org)).fetchone()
            if not groomer: raise HTTPException(400, "Groomer not available")
            overlap = conn.execute("SELECT 1 FROM grooming_appointments WHERE organization_id=? AND groomer_user_id=? AND status NOT IN ('CANCELED','DECLINED') AND NOT(end_at<=? OR start_at>=?)", (org,p.groomer_user_id,p.start_at,p.end_at)).fetchone()
            if overlap: raise HTTPException(409, "That groomer already has an appointment in this time window")
            aid = new_id("gapp"); now = utcnow()
            status = "CONFIRMED" if p.forms_complete else "AWAITING_FORMS"
            conn.execute("INSERT INTO grooming_appointments(id,organization_id,customer_id,pet_id,groomer_user_id,service_name,start_at,end_at,status,forms_complete,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (aid,org,pet["customer_id"],p.pet_id,p.groomer_user_id,p.service_name,p.start_at,p.end_at,status,1 if p.forms_complete else 0,now,now))
            audit(conn,org,u["id"],"grooming_appointment",aid,"CREATED",f"Booked {p.service_name}")
            return {"id": aid, "status": status}
        return command_once(conn,org,p.command_id,do)

@app.get("/api/audit/{entity_type}/{entity_id}")
def entity_audit(entity_type:str,entity_id:str,request:Request):
    u=require_user(request); org=u["organization_id"]
    with connect() as conn:
        rows=conn.execute("SELECT a.*,u.name actor_name FROM audit_events a LEFT JOIN users u ON u.id=a.actor_user_id WHERE a.organization_id=? AND a.entity_type=? AND a.entity_id=? ORDER BY a.id DESC",(org,entity_type,entity_id)).fetchall()
    return [dict(r) for r in rows]
