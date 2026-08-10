from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import sqlite3
import secrets
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .db import BACKUPS_DIR, DB_PATH, FILES_DIR, DATA_DIR, audit, command_once, connect, create_backup_archive, database_backend, hash_password, new_id, utcnow

router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_RESTORE_BYTES = 1024 * 1024 * 1024
MAX_RESTORE_MEMBERS = 20_000
MAX_RESTORE_EXPANDED_BYTES = 250 * 1024 * 1024
MAX_CSV_ROWS = 10_000


async def read_upload_limited(file: UploadFile, limit: int | None = None) -> bytes:
    """Read an uploaded file without allowing an unbounded request body."""
    limit = MAX_UPLOAD_BYTES if limit is None else limit
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"Upload is larger than the {limit // (1024 * 1024)} MB limit")
        chunks.append(chunk)
    return b"".join(chunks)


def user_for(request: Request) -> dict[str, Any]:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Sign in required")
    with connect() as conn:
        row = conn.execute(
            """SELECT u.*,o.name organization_name,o.profile organization_profile,o.is_demo
               FROM users u JOIN organizations o ON o.id=u.organization_id
               WHERE u.id=? AND u.active=1""",
            (uid,),
        ).fetchone()
    if not row:
        request.session.clear()
        raise HTTPException(401, "Sign in required")
    return dict(row)


def require_roles(user: dict[str, Any], *roles: str) -> None:
    if user["role"] not in roles:
        raise HTTPException(403, "You do not have permission for that action")


def require_automotive(user: dict[str, Any]) -> None:
    if user["organization_profile"] != "automotive_equipment":
        raise HTTPException(404, "This feature is not enabled for this business profile")


def require_grooming(user: dict[str, Any]) -> None:
    if user["organization_profile"] != "grooming":
        raise HTTPException(404, "This feature is not enabled for this business profile")


def rowdict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def iso_today() -> str:
    return date.today().isoformat()


def add_months(value: str, months: int) -> str:
    d = date.fromisoformat(value[:10])
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    max_day = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(d.day, max_day)).isoformat()


@router.get("/service-catalog")
def service_catalog(request: Request):
    u = user_for(request)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM service_catalog WHERE organization_id=? AND profile=? AND active=1 ORDER BY category,name",
            (u["organization_id"], u["organization_profile"]),
        ).fetchall()
    return [dict(r) for r in rows]


class ServiceCatalogCreate(BaseModel):
    name: str
    category: str | None = None
    default_minutes: int = Field(default=60, ge=15, le=1440)
    default_crew: int = Field(default=1, ge=1, le=8)
    qualification_required: str | None = None


@router.post("/service-catalog")
def create_service_catalog_item(p: ServiceCatalogCreate, request: Request):
    u = user_for(request)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR", "RECEPTION")
    org = u["organization_id"]
    name = p.name.strip()
    if not name:
        raise HTTPException(400, "Give this service a name")
    with connect() as conn:
        if conn.execute("SELECT 1 FROM service_catalog WHERE organization_id=? AND profile=? AND lower(name)=lower(?) AND active=1", (org, u["organization_profile"], name)).fetchone():
            raise HTTPException(409, "That service already exists")
        sid = new_id("svc")
        now = utcnow()
        conn.execute(
            """INSERT INTO service_catalog(id,organization_id,profile,name,category,default_minutes,default_crew,qualification_required,active,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,1,?,?)""",
            (sid, org, u["organization_profile"], name, p.category, p.default_minutes, p.default_crew, p.qualification_required, now, now),
        )
    return {"id": sid}




class ServiceCatalogUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    default_minutes: int | None = Field(default=None, ge=15, le=1440)
    default_crew: int | None = Field(default=None, ge=1, le=8)
    qualification_required: str | None = None
    active: bool | None = None


@router.patch("/service-catalog/{service_id}")
def update_service_catalog_item(service_id: str, p: ServiceCatalogUpdate, request: Request):
    u = user_for(request)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR", "RECEPTION")
    org = u["organization_id"]
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM service_catalog WHERE id=? AND organization_id=? AND profile=?",
            (service_id, org, u["organization_profile"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Service not found")
        values = p.model_dump(exclude_unset=True)
        if "name" in values:
            values["name"] = (values["name"] or "").strip()
            if not values["name"]:
                raise HTTPException(400, "Give this service a name")
            dup = conn.execute(
                "SELECT 1 FROM service_catalog WHERE organization_id=? AND profile=? AND lower(name)=lower(?) AND id<>? AND active=1",
                (org, u["organization_profile"], values["name"], service_id),
            ).fetchone()
            if dup:
                raise HTTPException(409, "That service already exists")
        if "active" in values:
            values["active"] = int(bool(values["active"]))
        if not values:
            return dict(row)
        values["updated_at"] = utcnow()
        conn.execute(
            f"UPDATE service_catalog SET {','.join(f'{k}=?' for k in values)} WHERE id=?",
            (*values.values(), service_id),
        )
        after = conn.execute("SELECT * FROM service_catalog WHERE id=?", (service_id,)).fetchone()
        audit(conn, org, u["id"], "service", service_id, "UPDATED", f"Updated service {after['name']}")
    return dict(after)


class BranchCreate(BaseModel):
    name: str
    address1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@router.get("/branches")
def branches(request: Request):
    u = user_for(request)
    require_automotive(u)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM branches WHERE organization_id=? AND active=1 ORDER BY name",
            (u["organization_id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/branches")
def create_branch(p: BranchCreate, request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER")
    name = p.name.strip()
    if not name:
        raise HTTPException(400, "Give this branch a name")
    with connect() as conn:
        if conn.execute(
            "SELECT 1 FROM branches WHERE organization_id=? AND lower(name)=lower(?) AND active=1",
            (u["organization_id"], name),
        ).fetchone():
            raise HTTPException(409, "That branch already exists")
        bid = new_id("branch"); now = utcnow()
        conn.execute(
            """INSERT INTO branches(id,organization_id,name,address1,city,state,postal_code,latitude,longitude,active,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,1,?,?)""",
            (bid,u["organization_id"],name,p.address1,p.city,p.state,p.postal_code,p.latitude,p.longitude,now,now),
        )
        audit(conn,u["organization_id"],u["id"],"branch",bid,"CREATED",f"Added branch {name}")
    return {"id": bid}


@router.get("/operations-summary")
def operations_summary(request: Request):
    u = user_for(request)
    org = u["organization_id"]
    today = date.today()
    soon = (today + timedelta(days=45)).isoformat()
    with connect() as conn:
        followups = conn.execute(
            "SELECT COUNT(*) FROM followups WHERE organization_id=? AND status='PENDING'",
            (org,),
        ).fetchone()[0]
        due_followups = conn.execute(
            "SELECT COUNT(*) FROM followups WHERE organization_id=? AND status='PENDING' AND due_at IS NOT NULL AND substr(due_at,1,10)<=?",
            (org, today.isoformat()),
        ).fetchone()[0]
        recurring = conn.execute(
            "SELECT COUNT(*) FROM recurring_plans WHERE organization_id=? AND status='ACTIVE' AND next_due_date<=?",
            (org, soon),
        ).fetchone()[0]
        estimates = conn.execute(
            "SELECT COUNT(*) FROM estimates WHERE organization_id=? AND status IN ('DRAFT','READY_TO_SEND','SENT','VIEWED','QUESTIONS')",
            (org,),
        ).fetchone()[0]
        parts = conn.execute(
            "SELECT COUNT(*) FROM job_parts WHERE organization_id=? AND status NOT IN ('RECEIVED','RESERVED','LOADED','INSTALLED','RETURNED')",
            (org,),
        ).fetchone()[0]
        recommendations = conn.execute(
            "SELECT COUNT(*) FROM recommendations WHERE organization_id=? AND status IN ('NEW','NEEDS_ESTIMATE','DEFERRED')",
            (org,),
        ).fetchone()[0]
        files = conn.execute("SELECT COUNT(*) FROM file_records WHERE organization_id=?", (org,)).fetchone()[0]
    return {
        "followups": followups,
        "followups_due": due_followups,
        "recurring_due_45": recurring,
        "estimates_open": estimates,
        "parts_open": parts,
        "recommendations_open": recommendations,
        "files": files,
    }


@router.get("/team")
def team(request: Request):
    u = user_for(request)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,name,email,role,qualifications_json,active,failed_login_count,locked_until,force_password_change FROM users WHERE organization_id=? ORDER BY role,name",
            (u["organization_id"],),
        ).fetchall()
    return [dict(r) | {"qualifications": loads(r["qualifications_json"], [])} for r in rows]


class TeamMemberCreate(BaseModel):
    name: str
    email: str
    role: str
    temporary_password: str = Field(min_length=12)
    qualifications: list[str] = []


@router.post("/team")
def create_team_member(p: TeamMemberCreate, request: Request):
    u = user_for(request)
    require_roles(u, "ADMIN", "MANAGER")
    org = u["organization_id"]
    allowed_auto = {"ADMIN","MANAGER","COORDINATOR","TECHNICIAN","BILLING","READ_ONLY"}
    allowed_groom = {"ADMIN","MANAGER","RECEPTION","GROOMER","READ_ONLY"}
    allowed = allowed_groom if u["organization_profile"] == "grooming" else allowed_auto
    role = p.role.upper()
    if role not in allowed:
        raise HTTPException(400, "That role is not available for this business profile")
    email = p.email.strip().lower()
    with connect() as conn:
        if conn.execute("SELECT 1 FROM users WHERE lower(email)=lower(?)", (email,)).fetchone():
            raise HTTPException(409, "That email already has a ServiceSlate account")
        uid = new_id("user"); now = utcnow()
        conn.execute("""INSERT INTO users(id,organization_id,email,name,role,password_hash,qualifications_json,active,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (uid,org,email,p.name.strip(),role,hash_password(p.temporary_password),json.dumps(p.qualifications),1,now))
        audit(conn,org,u["id"],"user",uid,"CREATED",f"Added {p.name} as {role}")
    return {"id":uid,"email":email,"role":role}


class ContactCreate(BaseModel):
    name: str
    title: str | None = None
    phone: str | None = None
    email: str | None = None
    location_id: str | None = None
    preferred_channel: str = "PHONE"
    can_approve_estimates: bool = False
    can_sign_work: bool = False


@router.post("/customers/{customer_id}/contacts")
def add_contact(customer_id: str, p: ContactCreate, request: Request):
    u = user_for(request)
    org = u["organization_id"]
    now = utcnow()
    with connect() as conn:
        customer = conn.execute("SELECT name FROM customers WHERE id=? AND organization_id=?", (customer_id, org)).fetchone()
        if not customer:
            raise HTTPException(404, "Customer not found")
        cid = new_id("contact")
        conn.execute(
            """INSERT INTO contacts(id,organization_id,customer_id,location_id,name,title,phone,email,preferred_channel,
               can_approve_estimates,can_sign_work,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, org, customer_id, p.location_id, p.name.strip(), p.title, p.phone, p.email, p.preferred_channel,
             int(p.can_approve_estimates), int(p.can_sign_work), now, now),
        )
        audit(conn, org, u["id"], "customer", customer_id, "CONTACT_ADDED", f"Added contact {p.name}")
    return {"id": cid}


class LocationCreate(BaseModel):
    name: str
    address1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    area_label: str | None = None
    access_notes: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@router.post("/customers/{customer_id}/locations")
def add_location(customer_id: str, p: LocationCreate, request: Request):
    u = user_for(request)
    org = u["organization_id"]
    now = utcnow()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM customers WHERE id=? AND organization_id=?", (customer_id, org)).fetchone():
            raise HTTPException(404, "Customer not found")
        lid = new_id("loc")
        conn.execute(
            """INSERT INTO locations(id,organization_id,customer_id,name,address1,city,state,postal_code,latitude,longitude,
               area_label,access_notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lid, org, customer_id, p.name.strip(), p.address1, p.city, p.state, p.postal_code, p.latitude, p.longitude,
             p.area_label, p.access_notes, now, now),
        )
        audit(conn, org, u["id"], "customer", customer_id, "LOCATION_ADDED", f"Added location {p.name}")
    return {"id": lid}


class EquipmentCreate(BaseModel):
    command_id: str | None = None
    customer_id: str
    location_id: str
    category: str
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    bay: str | None = None
    install_date: str | None = None
    warranty_until: str | None = None
    inspection_due_date: str | None = None
    inspection_interval_months: int | None = None
    notes: str | None = None


@router.post("/equipment")
def add_equipment(p: EquipmentCreate, request: Request):
    u = user_for(request)
    require_automotive(u)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            now = utcnow()
            loc = conn.execute(
                "SELECT 1 FROM locations WHERE id=? AND customer_id=? AND organization_id=?",
                (p.location_id, p.customer_id, org),
            ).fetchone()
            if not loc:
                raise HTTPException(400, "Choose a location that belongs to this customer")
            if p.serial_number:
                dup = conn.execute(
                    "SELECT id,manufacturer,model FROM equipment WHERE organization_id=? AND lower(serial_number)=lower(?)",
                    (org, p.serial_number.strip()),
                ).fetchone()
                if dup:
                    raise HTTPException(409, "That serial number already belongs to an equipment record")
            eid = new_id("equip")
            conn.execute(
                """INSERT INTO equipment(id,organization_id,customer_id,location_id,bay,category,manufacturer,model,serial_number,
                   install_date,warranty_until,inspection_due_date,inspection_interval_months,notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (eid, org, p.customer_id, p.location_id, p.bay, p.category, p.manufacturer, p.model, p.serial_number,
                 p.install_date, p.warranty_until, p.inspection_due_date, p.inspection_interval_months, p.notes, now, now),
            )
            audit(conn, org, u["id"], "equipment", eid, "CREATED", f"Created equipment {p.manufacturer or ''} {p.model or p.category}".strip())
            return {"id": eid}
        return command_once(conn, org, p.command_id, do) if p.command_id else do()


class EquipmentUpdate(BaseModel):
    version: int
    bay: str | None = None
    serial_number: str | None = None
    operational_status: str | None = None
    warranty_until: str | None = None
    inspection_due_date: str | None = None
    inspection_interval_months: int | None = None
    notes: str | None = None


@router.patch("/equipment/{equipment_id}")
def update_equipment(equipment_id: str, p: EquipmentUpdate, request: Request):
    u = user_for(request)
    require_automotive(u)
    org = u["organization_id"]
    with connect() as conn:
        row = conn.execute("SELECT * FROM equipment WHERE id=? AND organization_id=?", (equipment_id, org)).fetchone()
        if not row:
            raise HTTPException(404, "Equipment not found")
        if row["version"] != p.version:
            raise HTTPException(409, "This equipment changed while you were working. Reload and review the latest information.")
        changes = p.model_dump(exclude={"version"}, exclude_unset=True)
        if "serial_number" in changes and changes["serial_number"]:
            duplicate = conn.execute("SELECT 1 FROM equipment WHERE organization_id=? AND lower(serial_number)=lower(?) AND id<>?", (org, changes["serial_number"].strip(), equipment_id)).fetchone()
            if duplicate:
                raise HTTPException(409, "That serial number already belongs to another equipment record")
            changes["serial_number"] = changes["serial_number"].strip()
        if not changes:
            return dict(row)
        before = dict(row)
        fields = [f"{key}=?" for key in changes]
        vals = list(changes.values()) + [utcnow(), equipment_id, org, p.version]
        cur = conn.execute(
            f"UPDATE equipment SET {','.join(fields)},version=version+1,updated_at=? WHERE id=? AND organization_id=? AND version=?",
            vals,
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "This equipment changed while you were working")
        after = dict(conn.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone())
        audit(conn, org, u["id"], "equipment", equipment_id, "UPDATED", "Updated equipment record", before, after)
        return after


class CustomerUpdate(BaseModel):
    version: int
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    business_type: str | None = None
    status: str | None = None
    notes: str | None = None


@router.patch("/customers/{customer_id}")
def update_customer(customer_id: str, p: CustomerUpdate, request: Request):
    u = user_for(request)
    if u["role"] == "TECHNICIAN":
        raise HTTPException(403, "Technicians do not edit office customer records")
    org = u["organization_id"]
    with connect() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id=? AND organization_id=?", (customer_id, org)).fetchone()
        if not row:
            raise HTTPException(404, "Customer not found")
        if row["version"] != p.version:
            raise HTTPException(409, "This customer changed while you were working. Reload and review the latest information.")
        changes = p.model_dump(exclude={"version"}, exclude_unset=True)
        if "name" in changes:
            if changes["name"] is None:
                raise HTTPException(400, "Customer name cannot be blank")
            changes["name"] = changes["name"].strip()
            if not changes["name"]:
                raise HTTPException(400, "Customer name cannot be blank")
            dup = conn.execute("SELECT 1 FROM customers WHERE organization_id=? AND lower(name)=lower(?) AND id<>?", (org, changes["name"], customer_id)).fetchone()
            if dup:
                raise HTTPException(409, "Another customer already uses that name")
        if not changes:
            return dict(row)
        before = dict(row); now = utcnow()
        fields = [f"{k}=?" for k in changes]
        cur = conn.execute(
            f"UPDATE customers SET {','.join(fields)},version=version+1,updated_at=? WHERE id=? AND organization_id=? AND version=?",
            (*changes.values(), now, customer_id, org, p.version),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "This customer changed while you were working")
        after = dict(conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone())
        audit(conn,org,u["id"],"customer",customer_id,"UPDATED","Updated customer record",before,after)
        return after


class CommunicationCreate(BaseModel):
    command_id: str
    contact_id: str | None = None
    job_id: str | None = None
    channel: str = "PHONE"
    direction: str = "OUTBOUND"
    subject: str | None = None
    body: str | None = None
    outcome: str | None = None
    followup_summary: str | None = None
    followup_due_at: str | None = None


@router.get("/customers/{customer_id}/communications")
def customer_communications(customer_id: str, request: Request):
    u = user_for(request)
    if u["role"] == "TECHNICIAN":
        raise HTTPException(403, "Technicians use approved field history instead of office communication history")
    with connect() as conn:
        rows = conn.execute(
            """SELECT m.*,c.name contact_name,j.job_number,u.name created_by_name FROM communications m
               LEFT JOIN contacts c ON c.id=m.contact_id LEFT JOIN jobs j ON j.id=m.job_id
               LEFT JOIN users u ON u.id=m.created_by_user_id
               WHERE m.organization_id=? AND m.customer_id=? ORDER BY m.occurred_at DESC LIMIT 100""",
            (u["organization_id"], customer_id),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/customers/{customer_id}/communications")
def create_customer_communication(customer_id: str, p: CommunicationCreate, request: Request):
    u = user_for(request)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR", "BILLING", "RECEPTION")
    org = u["organization_id"]
    channel = p.channel.upper()
    if channel not in ("PHONE", "EMAIL", "IN_PERSON", "SMS", "OTHER"):
        raise HTTPException(400, "Choose a valid communication channel")
    direction = p.direction.upper()
    if direction not in ("OUTBOUND", "INBOUND"):
        raise HTTPException(400, "Choose inbound or outbound")
    with connect() as conn:
        def do():
            if not conn.execute("SELECT 1 FROM customers WHERE id=? AND organization_id=?", (customer_id, org)).fetchone():
                raise HTTPException(404, "Customer not found")
            if p.contact_id and not conn.execute("SELECT 1 FROM contacts WHERE id=? AND customer_id=? AND organization_id=?", (p.contact_id, customer_id, org)).fetchone():
                raise HTTPException(400, "That contact does not belong to this customer")
            if p.job_id and not conn.execute("SELECT 1 FROM jobs WHERE id=? AND customer_id=? AND organization_id=?", (p.job_id, customer_id, org)).fetchone():
                raise HTTPException(400, "That job does not belong to this customer")
            now = utcnow(); mid = new_id("comm")
            # Without a transport connector, email/SMS are prepared records, never falsely marked sent.
            status = "PREPARED" if channel in ("EMAIL", "SMS") and direction == "OUTBOUND" else "LOGGED"
            conn.execute(
                """INSERT INTO communications(id,organization_id,customer_id,contact_id,job_id,created_by_user_id,channel,direction,subject,body,status,outcome,occurred_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid,org,customer_id,p.contact_id,p.job_id,u["id"],channel,direction,p.subject,p.body,status,p.outcome,now,now),
            )
            if p.followup_summary:
                fid = new_id("follow")
                conn.execute(
                    """INSERT INTO followups(id,organization_id,customer_id,job_id,owner_user_id,kind,summary,due_at,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,'CUSTOMER_CONTACT',?,?, 'PENDING',?,?)""",
                    (fid,org,customer_id,p.job_id,u["id"],p.followup_summary,p.followup_due_at,now,now),
                )
            audit(conn,org,u["id"],"customer",customer_id,"COMMUNICATION_LOGGED",f"{direction.title()} {channel.lower()} {status.lower()}")
            return {"id": mid, "status": status}
        return command_once(conn, org, p.command_id, do)


@router.get("/customer-workspace/{customer_id}")
def customer_workspace(customer_id: str, request: Request):
    u = user_for(request)
    org = u["organization_id"]
    if u["role"] == "TECHNICIAN":
        raise HTTPException(403, "Technicians use equipment and approved work history for field context")
    with connect() as conn:
        customer = conn.execute("SELECT * FROM customers WHERE id=? AND organization_id=?", (customer_id, org)).fetchone()
        if not customer:
            raise HTTPException(404, "Customer not found")
        contacts = [dict(r) for r in conn.execute("SELECT * FROM contacts WHERE customer_id=? AND active=1 ORDER BY name", (customer_id,)).fetchall()]
        locations = [dict(r) for r in conn.execute("SELECT * FROM locations WHERE customer_id=? ORDER BY name", (customer_id,)).fetchall()]
        equipment = [dict(r) for r in conn.execute("SELECT * FROM equipment WHERE customer_id=? ORDER BY category,manufacturer,model", (customer_id,)).fetchall()]
        jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE customer_id=? ORDER BY created_at DESC LIMIT 30", (customer_id,)).fetchall()]
        estimates = [dict(r) for r in conn.execute("SELECT * FROM estimates WHERE customer_id=? ORDER BY created_at DESC", (customer_id,)).fetchall()]
        followups = [dict(r) for r in conn.execute("SELECT * FROM followups WHERE customer_id=? AND status IN ('PENDING','SNOOZED') ORDER BY due_at", (customer_id,)).fetchall()]
        recurring = [dict(r) for r in conn.execute("SELECT * FROM recurring_plans WHERE customer_id=? AND status='ACTIVE' ORDER BY next_due_date", (customer_id,)).fetchall()]
        files = [dict(r) for r in conn.execute("SELECT * FROM file_records WHERE organization_id=? AND entity_type='customer' AND entity_id=? ORDER BY created_at DESC", (org, customer_id)).fetchall()]
        communications = [dict(r) for r in conn.execute("""SELECT m.*,c.name contact_name,j.job_number,u.name created_by_name FROM communications m
            LEFT JOIN contacts c ON c.id=m.contact_id LEFT JOIN jobs j ON j.id=m.job_id LEFT JOIN users u ON u.id=m.created_by_user_id
            WHERE m.organization_id=? AND m.customer_id=? ORDER BY m.occurred_at DESC LIMIT 20""", (org, customer_id)).fetchall()]
    return {"customer": dict(customer), "contacts": contacts, "locations": locations, "equipment": equipment,
            "jobs": jobs, "estimates": estimates, "followups": followups, "recurring": recurring, "files": files, "communications": communications}


@router.get("/jobs/{job_id}/detail")
def job_detail(job_id: str, request: Request):
    u = user_for(request)
    org = u["organization_id"]
    with connect() as conn:
        job = conn.execute(
            """SELECT j.*,c.name customer_name,c.email customer_email,c.phone customer_phone,l.name location_name,l.address1,l.city,l.state,
               e.manufacturer,e.model,e.serial_number,e.category,e.bay,u.name owner_name
               FROM jobs j JOIN customers c ON c.id=j.customer_id JOIN locations l ON l.id=j.location_id
               LEFT JOIN equipment e ON e.id=j.equipment_id LEFT JOIN users u ON u.id=j.owner_user_id
               WHERE j.id=? AND j.organization_id=?""",
            (job_id, org),
        ).fetchone()
        if not job:
            raise HTTPException(404, "Job not found")
        tech_assigned = False
        if u["role"] == "TECHNICIAN":
            tech_assigned = bool(conn.execute(
                "SELECT 1 FROM visits WHERE job_id=? AND organization_id=? AND (technician_user_id=? OR helper_user_id=?) LIMIT 1",
                (job_id, org, u["id"], u["id"]),
            ).fetchone())
            historical = job["status"] in ("READY_TO_INVOICE", "INVOICED", "CLOSED") and bool(conn.execute(
                "SELECT 1 FROM work_submissions WHERE job_id=? AND state IN ('APPROVED','LOCKED') LIMIT 1", (job_id,)
            ).fetchone())
            if not tech_assigned and not historical:
                raise HTTPException(403, "This live or unapproved job is not assigned to you")
        visits = [dict(r) for r in conn.execute(
            """SELECT v.*,a.name technician_name,b.name helper_name FROM visits v
               JOIN users a ON a.id=v.technician_user_id LEFT JOIN users b ON b.id=v.helper_user_id
               WHERE v.job_id=? ORDER BY v.start_at""", (job_id,)).fetchall()]
        parts = [dict(r) for r in conn.execute(
            """SELECT jp.*,p.sku,p.manufacturer,p.manufacturer_part_number FROM job_parts jp
               LEFT JOIN parts p ON p.id=jp.part_id WHERE jp.job_id=? ORDER BY jp.created_at""", (job_id,)).fetchall()]
        recommendations = [dict(r) for r in conn.execute("SELECT * FROM recommendations WHERE job_id=? ORDER BY created_at DESC", (job_id,)).fetchall()]
        if u["role"] == "TECHNICIAN" and not tech_assigned:
            submissions = [dict(r) for r in conn.execute(
                """SELECT w.*,COALESCE(w.performed_by_text,u.name) technician_name FROM work_submissions w JOIN users u ON u.id=w.technician_user_id
                   WHERE w.job_id=? AND w.state IN ('APPROVED','LOCKED') ORDER BY w.created_at DESC""", (job_id,)).fetchall()]
        elif u["role"] == "TECHNICIAN":
            submissions = [dict(r) for r in conn.execute(
                """SELECT w.*,COALESCE(w.performed_by_text,u.name) technician_name FROM work_submissions w JOIN users u ON u.id=w.technician_user_id
                   WHERE w.job_id=? AND (w.technician_user_id=? OR w.state IN ('APPROVED','LOCKED')) ORDER BY w.created_at DESC""", (job_id,u["id"])).fetchall()]
        else:
            submissions = [dict(r) for r in conn.execute(
                """SELECT w.*,COALESCE(w.performed_by_text,u.name) technician_name FROM work_submissions w JOIN users u ON u.id=w.technician_user_id
                   WHERE w.job_id=? ORDER BY w.created_at DESC""", (job_id,)).fetchall()]
        forms = [dict(r) for r in conn.execute(
            """SELECT fc.*,ft.name template_name,ft.category FROM form_completions fc JOIN form_templates ft ON ft.id=fc.template_id
               WHERE fc.job_id=? ORDER BY fc.created_at DESC""", (job_id,)).fetchall()]
        files = [dict(r) for r in conn.execute("SELECT * FROM file_records WHERE organization_id=? AND entity_type='job' AND entity_id=? ORDER BY created_at DESC", (org, job_id)).fetchall()]
        auths = [dict(r) for r in conn.execute("SELECT * FROM authorizations WHERE organization_id=? AND job_id=? ORDER BY authorized_at DESC", (org, job_id)).fetchall()]
        confirmations = [dict(r) for r in conn.execute(
            "SELECT * FROM service_day_confirmations WHERE organization_id=? AND job_id=? ORDER BY created_at DESC", (org, job_id)
        ).fetchall()]
        audit_rows = [dict(r) for r in conn.execute(
            """SELECT a.*,u.name actor_name FROM audit_events a LEFT JOIN users u ON u.id=a.actor_user_id
               WHERE a.organization_id=? AND a.entity_type='job' AND a.entity_id=? ORDER BY a.id DESC LIMIT 80""",
            (org, job_id),).fetchall()]
    for sub in submissions:
        sub["measurements"] = loads(sub.pop("measurements_json"), [])
        sub["parts"] = loads(sub.pop("parts_json"), [])
    for form in forms:
        form["values"] = loads(form.pop("values_json"), {})
    if u["role"] == "TECHNICIAN":
        # Field context excludes office-only financial/authorization/audit administration.
        auths = []
        audit_rows = []
    return {"job": dict(job), "visits": visits, "parts": parts, "recommendations": recommendations,
            "submissions": submissions, "forms": forms, "files": files, "authorizations": auths,
            "confirmations": confirmations, "history": audit_rows}


class RecommendationCreate(BaseModel):
    command_id: str
    summary: str
    details: str | None = None
    urgency: str = "RECOMMENDED"
    followup_due: str | None = None


@router.post("/jobs/{job_id}/recommendations")
def add_recommendation(job_id: str, p: RecommendationCreate, request: Request):
    u = user_for(request)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            j = conn.execute("SELECT * FROM jobs WHERE id=? AND organization_id=?", (job_id, org)).fetchone()
            if not j:
                raise HTTPException(404, "Job not found")
            rid = new_id("rec")
            conn.execute(
                """INSERT INTO recommendations(id,organization_id,job_id,equipment_id,created_by_user_id,summary,urgency,status,details,followup_due,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, org, job_id, j["equipment_id"], u["id"], p.summary, p.urgency, "NEW", p.details, p.followup_due, utcnow()),
            )
            audit(conn, org, u["id"], "job", job_id, "RECOMMENDATION_ADDED", p.summary)
            return {"id": rid}
        return command_once(conn, org, p.command_id, do)


class RemoteStartCreate(BaseModel):
    command_id: str
    technician_user_id: str
    vehicle_id: str | None = None
    work_date: str
    origin_label: str
    latitude: float | None = None
    longitude: float | None = None
    reason: str
    expected_return_label: str | None = None


@router.get("/remote-starts")
def remote_starts(request: Request):
    u = user_for(request); require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    with connect() as conn:
        rows = conn.execute("""SELECT r.*,t.name technician_name,v.unit_number,a.name approved_by_name
            FROM remote_start_authorizations r JOIN users t ON t.id=r.technician_user_id
            LEFT JOIN vehicles v ON v.id=r.vehicle_id JOIN users a ON a.id=r.approved_by_user_id
            WHERE r.organization_id=? AND r.status='APPROVED' AND r.work_date>=? ORDER BY r.work_date,t.name""",
            (u["organization_id"], iso_today())).fetchall()
    return [dict(r) for r in rows]


@router.post("/remote-starts")
def create_remote_start(p: RemoteStartCreate, request: Request):
    u = user_for(request); require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER")
    org=u["organization_id"]
    with connect() as conn:
        def do():
            if not conn.execute("SELECT 1 FROM users WHERE id=? AND organization_id=? AND role='TECHNICIAN' AND active=1",(p.technician_user_id,org)).fetchone():
                raise HTTPException(400,"Choose an active technician")
            if p.vehicle_id and not conn.execute("SELECT 1 FROM vehicles WHERE id=? AND organization_id=?",(p.vehicle_id,org)).fetchone():
                raise HTTPException(400,"Choose a vehicle in this organization")
            rid=new_id("remote"); now=utcnow()
            conn.execute("""INSERT INTO remote_start_authorizations(id,organization_id,technician_user_id,vehicle_id,work_date,origin_label,latitude,longitude,reason,approved_by_user_id,expected_return_label,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(organization_id,technician_user_id,work_date) DO UPDATE SET vehicle_id=excluded.vehicle_id,origin_label=excluded.origin_label,latitude=excluded.latitude,longitude=excluded.longitude,reason=excluded.reason,approved_by_user_id=excluded.approved_by_user_id,expected_return_label=excluded.expected_return_label,status='APPROVED',updated_at=excluded.updated_at""",
                (rid,org,p.technician_user_id,p.vehicle_id,p.work_date,p.origin_label.strip(),p.latitude,p.longitude,p.reason.strip(),u["id"],p.expected_return_label,"APPROVED",now,now))
            audit(conn,org,u["id"],"remote_start",rid,"APPROVED",f"Approved remote start for {p.work_date}")
            return {"id":rid,"state":"APPROVED"}
        return command_once(conn,org,p.command_id,do)


@router.post("/remote-starts/{remote_id}/cancel")
def cancel_remote_start(remote_id: str, request: Request):
    u=user_for(request); require_automotive(u); require_roles(u,"ADMIN","MANAGER")
    with connect() as conn:
        cur=conn.execute("UPDATE remote_start_authorizations SET status='CANCELED',updated_at=? WHERE id=? AND organization_id=?",(utcnow(),remote_id,u["organization_id"]))
        if cur.rowcount!=1: raise HTTPException(404,"Remote start not found")
    return {"state":"CANCELED"}


@router.get("/recommendations")
def recommendations(request: Request, status: str = ""):
    u = user_for(request)
    org = u["organization_id"]
    where = "r.organization_id=?"
    args: list[Any] = [org]
    if status:
        where += " AND r.status=?"
        args.append(status)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT r.*,j.job_number,c.name customer_name,e.manufacturer,e.model,e.serial_number
                FROM recommendations r JOIN jobs j ON j.id=r.job_id JOIN customers c ON c.id=j.customer_id
                LEFT JOIN equipment e ON e.id=r.equipment_id WHERE {where} ORDER BY r.created_at DESC""", args).fetchall()
    return [dict(r) for r in rows]


class RecommendationDecision(BaseModel):
    command_id: str
    status: str
    customer_decision: str | None = None
    followup_due: str | None = None


@router.post("/recommendations/{recommendation_id}/decision")
def recommendation_decision(recommendation_id: str, p: RecommendationDecision, request: Request):
    u = user_for(request)
    require_roles(u, "COORDINATOR", "MANAGER", "ADMIN", "TECHNICIAN")
    org = u["organization_id"]
    with connect() as conn:
        def do():
            r = conn.execute("SELECT * FROM recommendations WHERE id=? AND organization_id=?", (recommendation_id, org)).fetchone()
            if not r:
                raise HTTPException(404, "Recommendation not found")
            conn.execute("UPDATE recommendations SET status=?,customer_decision=?,followup_due=? WHERE id=?", (p.status, p.customer_decision, p.followup_due, recommendation_id))
            if p.followup_due and p.status in ("DEFERRED", "NEW", "NEEDS_ESTIMATE"):
                fid = new_id("follow")
                conn.execute(
                    """INSERT INTO followups(id,organization_id,job_id,equipment_id,recommendation_id,owner_user_id,kind,summary,due_at,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (fid, org, r["job_id"], r["equipment_id"], recommendation_id, u["id"], "RECOMMENDATION", r["summary"], p.followup_due, "PENDING", utcnow(), utcnow()),
                )
            audit(conn, org, u["id"], "job", r["job_id"], "RECOMMENDATION_UPDATED", f"Recommendation set to {p.status}")
            return {"state": p.status}
        return command_once(conn, org, p.command_id, do)


class FollowupCreate(BaseModel):
    command_id: str
    customer_id: str | None = None
    job_id: str | None = None
    equipment_id: str | None = None
    owner_user_id: str | None = None
    kind: str = "CALLBACK"
    summary: str
    due_at: str | None = None


@router.get("/followups")
def list_followups(request: Request, include_done: bool = False):
    u = user_for(request)
    org = u["organization_id"]
    condition = "" if include_done else "AND f.status IN ('PENDING','SNOOZED')"
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT f.*,c.name customer_name,j.job_number,u.name owner_name FROM followups f
                LEFT JOIN customers c ON c.id=f.customer_id LEFT JOIN jobs j ON j.id=f.job_id
                LEFT JOIN users u ON u.id=f.owner_user_id WHERE f.organization_id=? {condition}
                ORDER BY CASE WHEN f.due_at IS NULL THEN 1 ELSE 0 END,f.due_at""", (org,)).fetchall()
    return [dict(r) for r in rows]


@router.post("/followups")
def create_followup(p: FollowupCreate, request: Request):
    u = user_for(request)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            fid = new_id("follow")
            now = utcnow()
            conn.execute(
                """INSERT INTO followups(id,organization_id,customer_id,job_id,equipment_id,owner_user_id,kind,summary,due_at,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fid, org, p.customer_id, p.job_id, p.equipment_id, p.owner_user_id or u["id"], p.kind, p.summary, p.due_at, "PENDING", now, now),
            )
            return {"id": fid}
        return command_once(conn, org, p.command_id, do)


class FollowupAction(BaseModel):
    command_id: str
    action: str
    outcome: str | None = None
    new_due_at: str | None = None


@router.post("/followups/{followup_id}/action")
def act_followup(followup_id: str, p: FollowupAction, request: Request):
    u = user_for(request)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            f = conn.execute("SELECT * FROM followups WHERE id=? AND organization_id=?", (followup_id, org)).fetchone()
            if not f:
                raise HTTPException(404, "Follow-up not found")
            if p.action == "complete":
                state = "COMPLETED"
                due = f["due_at"]
            elif p.action == "snooze":
                if not p.new_due_at:
                    raise HTTPException(400, "Choose a new follow-up date")
                state = "SNOOZED"
                due = p.new_due_at
            elif p.action == "cancel":
                state = "CANCELED"
                due = f["due_at"]
            else:
                raise HTTPException(400, "Unknown follow-up action")
            conn.execute("UPDATE followups SET status=?,outcome=?,due_at=?,updated_at=? WHERE id=?", (state, p.outcome, due, utcnow(), followup_id))
            return {"state": state}
        return command_once(conn, org, p.command_id, do)


class RecurringCreate(BaseModel):
    command_id: str
    customer_id: str
    location_id: str | None = None
    equipment_id: str | None = None
    service_name: str
    frequency_months: int = Field(ge=1, le=120)
    lead_days: int = Field(default=30, ge=0, le=365)
    next_due_date: str
    owner_user_id: str | None = None
    notes: str | None = None


@router.get("/recurring")
def recurring(request: Request):
    u = user_for(request)
    org = u["organization_id"]
    with connect() as conn:
        rows = conn.execute(
            """SELECT rp.*,c.name customer_name,l.name location_name,e.manufacturer,e.model,e.serial_number,u.name owner_name
               FROM recurring_plans rp JOIN customers c ON c.id=rp.customer_id
               LEFT JOIN locations l ON l.id=rp.location_id LEFT JOIN equipment e ON e.id=rp.equipment_id
               LEFT JOIN users u ON u.id=rp.owner_user_id WHERE rp.organization_id=?
               ORDER BY rp.next_due_date""", (org,)).fetchall()
    return [dict(r) for r in rows]


@router.post("/recurring")
def create_recurring(p: RecurringCreate, request: Request):
    u = user_for(request)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            rid = new_id("recur")
            now = utcnow()
            conn.execute(
                """INSERT INTO recurring_plans(id,organization_id,customer_id,location_id,equipment_id,service_name,frequency_months,
                   lead_days,next_due_date,owner_user_id,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, org, p.customer_id, p.location_id, p.equipment_id, p.service_name, p.frequency_months, p.lead_days,
                 p.next_due_date, p.owner_user_id or u["id"], p.notes, now, now),
            )
            return {"id": rid}
        return command_once(conn, org, p.command_id, do)


class RecurringAction(BaseModel):
    command_id: str
    action: str


@router.post("/recurring/{plan_id}/action")
def recurring_action(plan_id: str, p: RecurringAction, request: Request):
    u = user_for(request)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            plan = conn.execute("SELECT * FROM recurring_plans WHERE id=? AND organization_id=?", (plan_id, org)).fetchone()
            if not plan:
                raise HTTPException(404, "Recurring plan not found")
            if p.action == "create_job":
                existing = conn.execute(
                    """SELECT id,job_number FROM jobs WHERE organization_id=? AND customer_id=? AND job_type=?
                       AND due_date=? AND status NOT IN ('CANCELED','CLOSED')""",
                    (org, plan["customer_id"], plan["service_name"], plan["next_due_date"]),
                ).fetchone()
                if existing:
                    return {"job_id": existing["id"], "job_number": existing["job_number"], "already_exists": True}
                n = conn.execute("SELECT COUNT(*) FROM jobs WHERE organization_id=?", (org,)).fetchone()[0] + 1
                jid = new_id("job")
                num = f"WO-{date.today().strftime('%y')}{100+n:03d}"
                now = utcnow()
                location_id = plan["location_id"] or conn.execute("SELECT id FROM locations WHERE customer_id=? ORDER BY created_at LIMIT 1", (plan["customer_id"],)).fetchone()[0]
                conn.execute(
                    """INSERT INTO jobs(id,organization_id,customer_id,location_id,equipment_id,job_number,job_type,description,status,
                       priority,owner_user_id,next_action,due_date,estimated_minutes,crew_min,crew_recommended,simultaneous_crew_minutes,
                       parts_status,commitment_type,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (jid, org, plan["customer_id"], location_id, plan["equipment_id"], num, plan["service_name"],
                     f"Recurring obligation: {plan['service_name']}", "APPROVED_UNSCHEDULED", "NORMAL", plan["owner_user_id"],
                     "Schedule recurring service", plan["next_due_date"], 120, 1, 1, 0, "READY", "FLEXIBLE_DAY", now, now),
                )
                audit(conn, org, u["id"], "job", jid, "CREATED_FROM_RECURRENCE", f"Created {num} from recurring plan")
                return {"job_id": jid, "job_number": num}
            if p.action == "complete_cycle":
                next_due = add_months(plan["next_due_date"], plan["frequency_months"])
                conn.execute("UPDATE recurring_plans SET last_completed_date=?,next_due_date=?,updated_at=? WHERE id=?", (plan["next_due_date"], next_due, utcnow(), plan_id))
                return {"next_due_date": next_due}
            if p.action in ("pause", "cancel"):
                state = "PAUSED" if p.action == "pause" else "CANCELED"
                conn.execute("UPDATE recurring_plans SET status=?,updated_at=? WHERE id=?", (state, utcnow(), plan_id))
                return {"state": state}
            raise HTTPException(400, "Unknown recurring action")
        return command_once(conn, org, p.command_id, do)


class PartCreate(BaseModel):
    name: str
    sku: str | None = None
    manufacturer: str | None = None
    manufacturer_part_number: str | None = None
    default_cost_cents: int | None = None
    min_stock: float | None = None


@router.get("/parts")
def list_parts(request: Request):
    u = user_for(request)
    require_automotive(u)
    org = u["organization_id"]
    with connect() as conn:
        catalog = [dict(r) for r in conn.execute("SELECT * FROM parts WHERE organization_id=? AND active=1 ORDER BY name", (org,)).fetchall()]
        holds = [dict(r) for r in conn.execute(
            """SELECT jp.*,j.job_number,j.description,c.name customer_name,p.sku FROM job_parts jp JOIN jobs j ON j.id=jp.job_id
               JOIN customers c ON c.id=j.customer_id LEFT JOIN parts p ON p.id=jp.part_id WHERE jp.organization_id=?
               ORDER BY CASE jp.status WHEN 'BACKORDERED' THEN 0 WHEN 'ORDERED' THEN 1 WHEN 'NEEDED' THEN 2 ELSE 3 END,jp.updated_at DESC""", (org,)).fetchall()]
    return {"catalog": catalog, "job_parts": holds}


@router.post("/parts")
def create_part(p: PartCreate, request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR", "BILLING")
    org = u["organization_id"]
    with connect() as conn:
        pid = new_id("part")
        now = utcnow()
        conn.execute(
            """INSERT INTO parts(id,organization_id,sku,manufacturer,manufacturer_part_number,name,default_cost_cents,min_stock,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (pid, org, p.sku, p.manufacturer, p.manufacturer_part_number, p.name, p.default_cost_cents, p.min_stock, now, now),
        )
    return {"id": pid}


class JobPartCreate(BaseModel):
    command_id: str
    part_id: str | None = None
    description: str
    quantity: float = Field(default=1, gt=0)
    status: str = "NEEDED"
    vendor: str | None = None
    expected_date: str | None = None
    notes: str | None = None


@router.post("/jobs/{job_id}/parts")
def add_job_part(job_id: str, p: JobPartCreate, request: Request):
    u = user_for(request)
    require_automotive(u)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            job = conn.execute("SELECT * FROM jobs WHERE id=? AND organization_id=?", (job_id, org)).fetchone()
            if not job:
                raise HTTPException(404, "Job not found")
            pid = new_id("jp")
            now = utcnow()
            conn.execute(
                """INSERT INTO job_parts(id,organization_id,job_id,part_id,description,quantity,status,source,vendor,expected_date,notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, org, job_id, p.part_id, p.description, p.quantity, p.status, "office", p.vendor, p.expected_date, p.notes, now, now),
            )
            if p.status not in ("RECEIVED", "RESERVED", "LOADED", "INSTALLED"):
                conn.execute("UPDATE jobs SET status=CASE WHEN status='SCHEDULED' THEN status ELSE 'BLOCKED' END,parts_status=?,blocked_reason='Waiting for parts',next_action='Track required parts',version=version+1,updated_at=? WHERE id=?", (p.status, now, job_id))
            audit(conn, org, u["id"], "job", job_id, "PART_REQUIRED", f"Required part: {p.description}")
            return {"id": pid}
        return command_once(conn, org, p.command_id, do)


class PartStatus(BaseModel):
    command_id: str
    status: str
    expected_date: str | None = None
    notes: str | None = None


@router.post("/job-parts/{job_part_id}/status")
def part_status(job_part_id: str, p: PartStatus, request: Request):
    u = user_for(request)
    require_automotive(u)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            part = conn.execute("SELECT * FROM job_parts WHERE id=? AND organization_id=?", (job_part_id, org)).fetchone()
            if not part:
                raise HTTPException(404, "Job part not found")
            now = utcnow()
            conn.execute("UPDATE job_parts SET status=?,expected_date=COALESCE(?,expected_date),notes=COALESCE(?,notes),updated_at=? WHERE id=?", (p.status, p.expected_date, p.notes, now, job_part_id))
            open_count = conn.execute(
                "SELECT COUNT(*) FROM job_parts WHERE job_id=? AND status NOT IN ('RECEIVED','RESERVED','LOADED','INSTALLED','RETURNED')",
                (part["job_id"],),
            ).fetchone()[0]
            if open_count == 0:
                job = conn.execute("SELECT status FROM jobs WHERE id=?", (part["job_id"],)).fetchone()
                if job and job["status"] == "BLOCKED":
                    conn.execute("UPDATE jobs SET status='APPROVED_UNSCHEDULED',parts_status='RECEIVED',blocked_reason=NULL,next_action='Parts ready — schedule return visit',version=version+1,updated_at=? WHERE id=?", (now, part["job_id"]))
                    audit(conn, org, u["id"], "job", part["job_id"], "PARTS_READY", "All required parts are ready; schedule return visit")
            return {"state": p.status, "all_ready": open_count == 0}
        return command_once(conn, org, p.command_id, do)


class EstimateLineIn(BaseModel):
    line_type: str = "LABOR"
    description: str
    quantity: float = Field(default=1, gt=0)
    unit_price_cents: int = Field(default=0, ge=0)


class EstimateCreate(BaseModel):
    command_id: str
    customer_id: str
    job_id: str | None = None
    lines: list[EstimateLineIn]
    assumptions: str | None = None
    exclusions: str | None = None
    expires_on: str | None = None


@router.get("/estimates")
def estimates(request: Request):
    u = user_for(request)
    require_automotive(u)
    org = u["organization_id"]
    with connect() as conn:
        rows = conn.execute(
            """SELECT e.*,c.name customer_name,j.job_number FROM estimates e JOIN customers c ON c.id=e.customer_id
               LEFT JOIN jobs j ON j.id=e.job_id WHERE e.organization_id=? ORDER BY e.created_at DESC""", (org,)).fetchall()
    return [dict(r) for r in rows]


@router.post("/estimates")
def create_estimate(p: EstimateCreate, request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR", "BILLING")
    org = u["organization_id"]
    with connect() as conn:
        def do():
            if not conn.execute("SELECT 1 FROM customers WHERE id=? AND organization_id=?", (p.customer_id, org)).fetchone():
                raise HTTPException(404, "Customer not found")
            count = conn.execute("SELECT COUNT(DISTINCT estimate_number) FROM estimates WHERE organization_id=?", (org,)).fetchone()[0] + 1
            number = f"EST-{date.today().strftime('%y')}{100+count:03d}"
            eid = new_id("est")
            subtotal = sum(round(line.quantity * line.unit_price_cents) for line in p.lines)
            now = utcnow()
            conn.execute(
                """INSERT INTO estimates(id,organization_id,customer_id,job_id,estimate_number,revision,status,subtotal_cents,total_cents,
                   assumptions,exclusions,expires_on,created_by_user_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (eid, org, p.customer_id, p.job_id, number, 1, "DRAFT", subtotal, subtotal, p.assumptions, p.exclusions, p.expires_on, u["id"], now, now),
            )
            for index, line in enumerate(p.lines):
                amount = round(line.quantity * line.unit_price_cents)
                conn.execute("INSERT INTO estimate_lines(id,estimate_id,line_type,description,quantity,unit_price_cents,amount_cents,sort_order) VALUES(?,?,?,?,?,?,?,?)",
                             (new_id("eline"), eid, line.line_type, line.description, line.quantity, line.unit_price_cents, amount, index))
            if p.job_id:
                conn.execute("UPDATE jobs SET status='ESTIMATE_REQUIRED',next_action='Review estimate',version=version+1,updated_at=? WHERE id=? AND organization_id=?", (now, p.job_id, org))
                audit(conn, org, u["id"], "job", p.job_id, "ESTIMATE_CREATED", f"Created {number}")
            return {"id": eid, "estimate_number": number}
        return command_once(conn, org, p.command_id, do)


@router.get("/estimates/{estimate_id}")
def estimate_detail(estimate_id: str, request: Request):
    u = user_for(request)
    org = u["organization_id"]
    with connect() as conn:
        est = conn.execute("SELECT e.*,c.name customer_name,j.job_number FROM estimates e JOIN customers c ON c.id=e.customer_id LEFT JOIN jobs j ON j.id=e.job_id WHERE e.id=? AND e.organization_id=?", (estimate_id, org)).fetchone()
        if not est:
            raise HTTPException(404, "Estimate not found")
        lines = [dict(r) for r in conn.execute("SELECT * FROM estimate_lines WHERE estimate_id=? ORDER BY sort_order", (estimate_id,)).fetchall()]
        auth = [dict(r) for r in conn.execute("SELECT * FROM authorizations WHERE estimate_id=? ORDER BY authorized_at DESC", (estimate_id,)).fetchall()]
    return {"estimate": dict(est), "lines": lines, "authorizations": auth}


class EstimateStatus(BaseModel):
    command_id: str
    action: str
    signer_name: str | None = None
    signer_title: str | None = None
    note: str | None = None


@router.post("/estimates/{estimate_id}/action")
def estimate_action(estimate_id: str, p: EstimateStatus, request: Request):
    u = user_for(request)
    require_automotive(u)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            est = conn.execute("SELECT * FROM estimates WHERE id=? AND organization_id=?", (estimate_id, org)).fetchone()
            if not est:
                raise HTTPException(404, "Estimate not found")
            now = utcnow()
            if p.action == "ready":
                state = "READY_TO_SEND"
            elif p.action == "send":
                state = "SENT"
                conn.execute("UPDATE estimates SET sent_at=? WHERE id=?", (now, estimate_id))
            elif p.action == "approve":
                if not p.signer_name:
                    raise HTTPException(400, "Record who approved this estimate")
                state = "APPROVED"
                scope = f"Approved {est['estimate_number']} revision {est['revision']} for {est['total_cents']/100:.2f} {est['currency']}"
                conn.execute(
                    """INSERT INTO authorizations(id,organization_id,customer_id,job_id,estimate_id,kind,scope_text,signer_name,signer_title,method,signature_text,authorized_at,created_by_user_id,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id("auth"), org, est["customer_id"], est["job_id"], estimate_id, "ESTIMATE_APPROVAL", scope, p.signer_name, p.signer_title,
                     "IN_PERSON", p.signer_name, now, u["id"], now),
                )
                conn.execute("UPDATE estimates SET decided_at=? WHERE id=?", (now, estimate_id))
                if est["job_id"]:
                    conn.execute("UPDATE jobs SET status='APPROVED_UNSCHEDULED',next_action='Schedule approved work',version=version+1,updated_at=? WHERE id=?", (now, est["job_id"]))
                    audit(conn, org, u["id"], "job", est["job_id"], "ESTIMATE_APPROVED", scope)
            elif p.action == "decline":
                state = "DECLINED"
                conn.execute("UPDATE estimates SET decided_at=? WHERE id=?", (now, estimate_id))
            elif p.action == "cancel":
                state = "CANCELED"
            else:
                raise HTTPException(400, "Unknown estimate action")
            conn.execute("UPDATE estimates SET status=?,updated_at=? WHERE id=?", (state, now, estimate_id))
            return {"state": state}
        return command_once(conn, org, p.command_id, do)



SERVICE_DAY_NOTICE = "Your appointment is reserved for the scheduled service day unless a specific arrival time or time window has been confirmed separately. Arrival timing may vary as field service work progresses throughout the day. If an unexpected circumstance requires a change to the scheduled date, we’ll contact you promptly with an update and coordinate the next available service day."


class PortalLinkCreate(BaseModel):
    expires_days: int = Field(default=7, ge=1, le=30)


class ServiceDayLinkCreate(PortalLinkCreate):
    command_id: str
    recipient: str | None = None
    channel: str = "EMAIL"


@router.post("/estimates/{estimate_id}/portal-link")
def create_estimate_portal_link(estimate_id: str, p: PortalLinkCreate, request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR", "BILLING")
    org = u["organization_id"]
    with connect() as conn:
        est = conn.execute("SELECT * FROM estimates WHERE id=? AND organization_id=?", (estimate_id, org)).fetchone()
        if not est:
            raise HTTPException(404, "Estimate not found")
        if est["status"] in ("APPROVED", "DECLINED", "CANCELED", "SUPERSEDED"):
            raise HTTPException(400, "This estimate revision is no longer waiting for a customer decision")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        lid = new_id("portal"); now = utcnow()
        expires = (datetime.now(timezone.utc) + timedelta(days=p.expires_days)).isoformat()
        conn.execute(
            """INSERT INTO portal_links(id,organization_id,token_hash,customer_id,job_id,purpose,expires_at,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (lid, org, token_hash, est["customer_id"], est["job_id"], f"ESTIMATE_APPROVAL:{estimate_id}", expires, now),
        )
        audit(conn, org, u["id"], "estimate", estimate_id, "PORTAL_LINK_CREATED", f"Created secure approval link for {est['estimate_number']} revision {est['revision']}")
    return {"token": token, "path": f"/?portal={token}", "expires_at": expires}


@router.get("/service-confirmations")
def list_service_confirmations(request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR", "BILLING", "READ_ONLY")
    with connect() as conn:
        rows = conn.execute(
            """SELECT sc.*,j.job_number,j.description,c.name customer_name,l.name location_name
               FROM service_day_confirmations sc JOIN jobs j ON j.id=sc.job_id
               JOIN customers c ON c.id=sc.customer_id JOIN locations l ON l.id=j.location_id
               WHERE sc.organization_id=? ORDER BY sc.scheduled_date,sc.created_at DESC""",
            (u["organization_id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/jobs/{job_id}/service-day-link")
def create_service_day_link(job_id: str, p: ServiceDayLinkCreate, request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    org = u["organization_id"]
    with connect() as conn:
        def do():
            job = conn.execute("SELECT * FROM jobs WHERE id=? AND organization_id=?", (job_id, org)).fetchone()
            if not job:
                raise HTTPException(404, "Job not found")
            visit = conn.execute(
                """SELECT * FROM visits WHERE organization_id=? AND job_id=? AND execution_status!='CANCELED'
                   ORDER BY start_at DESC LIMIT 1""", (org, job_id)
            ).fetchone()
            if not visit:
                raise HTTPException(400, "Schedule the job before requesting customer confirmation")
            scheduled_date = visit["start_at"][:10]
            now = utcnow()
            conn.execute(
                """UPDATE service_day_confirmations SET status='STALE',updated_at=?
                   WHERE organization_id=? AND job_id=? AND status IN ('AWAITING_CONFIRMATION','CONFIRMED','CHANGE_REQUESTED')
                   AND (visit_id<>? OR scheduled_date<>?)""",
                (now, org, job_id, visit["id"], scheduled_date),
            )
            existing = conn.execute(
                """SELECT sc.* FROM service_day_confirmations sc
                   WHERE sc.organization_id=? AND sc.job_id=? AND sc.visit_id=? AND sc.scheduled_date=?
                   AND sc.status IN ('AWAITING_CONFIRMATION','CONFIRMED','CHANGE_REQUESTED')
                   ORDER BY sc.created_at DESC LIMIT 1""",
                (org, job_id, visit["id"], scheduled_date),
            ).fetchone()
            if existing and existing["status"] == "CONFIRMED":
                return {"confirmation_id": existing["id"], "status": "CONFIRMED", "scheduled_date": scheduled_date, "already_confirmed": True}
            if existing:
                conn.execute("UPDATE service_day_confirmations SET status='STALE',updated_at=? WHERE id=?", (now, existing["id"]))
                if existing["portal_link_id"]:
                    conn.execute("UPDATE portal_links SET consumed_at=COALESCE(consumed_at,?) WHERE id=?", (now, existing["portal_link_id"]))
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            lid = new_id("portal"); cid = new_id("sdc")
            reply_code = secrets.token_hex(4).upper()
            expires = (datetime.now(timezone.utc) + timedelta(days=p.expires_days)).isoformat()
            conn.execute(
                """INSERT INTO portal_links(id,organization_id,token_hash,customer_id,job_id,purpose,expires_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (lid, org, token_hash, job["customer_id"], job_id, f"SERVICE_DAY_CONFIRMATION:{cid}", expires, now),
            )
            conn.execute(
                """INSERT INTO service_day_confirmations(id,organization_id,customer_id,job_id,visit_id,portal_link_id,scheduled_date,
                   commitment_type,disclosure_version,disclosure_text,status,recipient,channel,reply_code,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,1,?,'AWAITING_CONFIRMATION',?,?,?,?,?)""",
                (cid, org, job["customer_id"], job_id, visit["id"], lid, scheduled_date, visit["commitment_type"], SERVICE_DAY_NOTICE, p.recipient, p.channel.upper(), reply_code, now, now),
            )
            audit(conn, org, u["id"], "job", job_id, "SERVICE_DAY_CONFIRMATION_REQUESTED", f"Requested customer confirmation for service day {scheduled_date}")
            return {"confirmation_id": cid, "token": token, "path": f"/?portal={token}", "expires_at": expires, "scheduled_date": scheduled_date, "status": "AWAITING_CONFIRMATION", "disclosure_text": SERVICE_DAY_NOTICE, "reply_code": reply_code}
        return command_once(conn, org, p.command_id, do)


class ServiceDayMessagePrepare(BaseModel):
    command_id: str


@router.post("/service-confirmations/{confirmation_id}/prepare-message")
def prepare_service_day_message(confirmation_id: str, p: ServiceDayMessagePrepare, request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    org = u["organization_id"]
    with connect() as conn:
        def do():
            row = conn.execute(
                """SELECT sc.*,j.job_number,j.description,c.name customer_name,c.email customer_email,
                          o.name organization_name,o.settings_json,o.public_base_url
                   FROM service_day_confirmations sc JOIN jobs j ON j.id=sc.job_id
                   JOIN customers c ON c.id=sc.customer_id JOIN organizations o ON o.id=sc.organization_id
                   WHERE sc.id=? AND sc.organization_id=?""",
                (confirmation_id, org),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Service-day confirmation not found")
            if row["status"] == "STALE":
                raise HTTPException(409, "The scheduled date changed. Prepare a confirmation for the current service day.")
            recipient = (row["recipient"] or row["customer_email"] or "").strip()
            if not recipient:
                raise HTTPException(400, "Add a customer email address before preparing the confirmation")
            code = row["reply_code"] or secrets.token_hex(4).upper()
            if not row["reply_code"]:
                conn.execute("UPDATE service_day_confirmations SET reply_code=?,updated_at=? WHERE id=?", (code, utcnow(), confirmation_id))
            try:
                settings = json.loads(row["settings_json"] or "{}")
            except json.JSONDecodeError:
                settings = {}
            public_base = str(row["public_base_url"] or settings.get("public_base_url") or "").strip().rstrip("/")
            public_link = ""
            if public_base and row["portal_link_id"]:
                # Rotate a short-lived portal token at message-preparation time so the raw token never
                # needs to live in the database. The communication body preserves exactly what was sent.
                portal_token = secrets.token_urlsafe(32)
                portal_hash = hashlib.sha256(portal_token.encode()).hexdigest()
                portal_expires = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
                conn.execute("UPDATE portal_links SET token_hash=?,expires_at=?,consumed_at=NULL WHERE id=?", (portal_hash, portal_expires, row["portal_link_id"]))
                public_link = f"{public_base}/?portal={portal_token}"
            from datetime import date as _date
            try:
                pretty_date = _date.fromisoformat(row["scheduled_date"]).strftime("%A, %B %d, %Y").replace(" 0", " ")
            except Exception:
                pretty_date = row["scheduled_date"]
            subject = f"Service Day Confirmation — {pretty_date} [SSDAY:{code}]"
            body = (
                f"Hello,\n\nYour service with {row['organization_name']} is scheduled for {pretty_date}.\n\n"
                f"{row['disclosure_text']}\n\n"
                f"Please reply CONFIRM if {pretty_date} works for you. "
                "If you need another day, reply DIFFERENT DAY and our service team will follow up.\n\n"
                + (f"You can also confirm securely here: {public_link}\n\n" if public_link else "")
                + f"Reference: [SSDAY:{code}]\n\nThank you,\n{row['organization_name']}"
            )
            existing = conn.execute(
                """SELECT * FROM communications WHERE organization_id=? AND service_day_confirmation_id=?
                   AND direction='OUTBOUND' AND channel='EMAIL' AND status IN ('PREPARED','SENT')
                   ORDER BY created_at DESC LIMIT 1""",
                (org, confirmation_id),
            ).fetchone()
            if existing and existing["status"] == "SENT":
                return {"communication_id": existing["id"], "status": "SENT", "recipient": recipient, "subject": existing["subject"], "already_sent": True}
            now = utcnow()
            if existing:
                comm_id = existing["id"]
                conn.execute("UPDATE communications SET subject=?,body=?,status='PREPARED',occurred_at=? WHERE id=?", (subject, body, now, comm_id))
            else:
                comm_id = new_id("comm")
                conn.execute(
                    """INSERT INTO communications(id,organization_id,customer_id,job_id,created_by_user_id,channel,direction,
                       subject,body,status,outcome,occurred_at,created_at,service_day_confirmation_id)
                       VALUES(?,?,?,?,?,'EMAIL','OUTBOUND',?,?,'PREPARED','Service day confirmation',?,?,?)""",
                    (comm_id, org, row["customer_id"], row["job_id"], u["id"], subject, body, now, now, confirmation_id),
                )
            audit(conn, org, u["id"], "job", row["job_id"], "SERVICE_DAY_MESSAGE_PREPARED", f"Prepared service-day confirmation email for {pretty_date}")
            return {"communication_id": comm_id, "status": "PREPARED", "recipient": recipient, "subject": subject, "body": body, "reply_code": code, "public_link": public_link or None}
        return command_once(conn, org, p.command_id, do)


class ManualServiceDayConfirmation(BaseModel):
    command_id: str
    contact_name: str
    channel: str = "PHONE"


@router.post("/service-confirmations/{confirmation_id}/manual-confirm")
def manual_service_day_confirmation(confirmation_id: str, p: ManualServiceDayConfirmation, request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    org = u["organization_id"]
    with connect() as conn:
        def do():
            row = conn.execute("SELECT * FROM service_day_confirmations WHERE id=? AND organization_id=?", (confirmation_id, org)).fetchone()
            if not row:
                raise HTTPException(404, "Service-day confirmation not found")
            if row["status"] == "STALE":
                raise HTTPException(409, "The scheduled date changed. Create a confirmation for the current service day.")
            name = p.contact_name.strip()
            if not name:
                raise HTTPException(400, "Enter the customer contact name")
            now = utcnow(); channel = p.channel.upper()
            conn.execute("UPDATE service_day_confirmations SET status='CONFIRMED',channel=?,confirmed_by_name=?,confirmed_at=?,updated_at=? WHERE id=?", (channel, name, now, now, confirmation_id))
            audit(conn, org, u["id"], "job", row["job_id"], "CUSTOMER_SERVICE_DAY_CONFIRMED", f"{name} confirmed service day {row['scheduled_date']} by {channel.lower()}")
            return {"state": "CONFIRMED", "confirmed_at": now}
        return command_once(conn, org, p.command_id, do)

def _portal_record(conn: sqlite3.Connection, token: str) -> dict[str, Any]:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    link = conn.execute(
        """SELECT p.*,c.name customer_name,o.name organization_name
           FROM portal_links p JOIN customers c ON c.id=p.customer_id JOIN organizations o ON o.id=p.organization_id
           WHERE p.token_hash=?""",
        (token_hash,),
    ).fetchone()
    if not link:
        raise HTTPException(404, "This secure link is not valid")
    try:
        if datetime.fromisoformat(link["expires_at"]) < datetime.now(timezone.utc):
            raise HTTPException(410, "This secure link has expired")
        kind, record_id = link["purpose"].split(":", 1)
    except (ValueError, IndexError):
        raise HTTPException(410, "This secure link is not valid")
    base = dict(link) | {"portal_kind": kind, "record_id": record_id}
    if kind == "ESTIMATE_APPROVAL":
        est = conn.execute("SELECT * FROM estimates WHERE id=? AND organization_id=?", (record_id, link["organization_id"])).fetchone()
        if not est:
            raise HTTPException(404, "This estimate revision is no longer available")
        return base | {f"estimate_{k}": v for k, v in dict(est).items()} | {"estimate_id": est["id"], "estimate_number": est["estimate_number"], "revision": est["revision"], "estimate_status": est["status"], "currency": est["currency"], "total_cents": est["total_cents"], "subtotal_cents": est["subtotal_cents"], "tax_cents": est["tax_cents"], "assumptions": est["assumptions"], "exclusions": est["exclusions"], "expires_on": est["expires_on"]}
    if kind == "SERVICE_DAY_CONFIRMATION":
        confirmation = conn.execute(
            """SELECT sc.*,j.job_number,j.description,l.name location_name,l.city,l.state
               FROM service_day_confirmations sc JOIN jobs j ON j.id=sc.job_id JOIN locations l ON l.id=j.location_id
               WHERE sc.id=? AND sc.organization_id=?""", (record_id, link["organization_id"])
        ).fetchone()
        if not confirmation:
            raise HTTPException(404, "This service-day confirmation is no longer available")
        if confirmation["status"] == "STALE":
            raise HTTPException(409, "The service date changed. Please use the latest confirmation message from the service team.")
        return base | {f"confirmation_{k}": v for k, v in dict(confirmation).items()} | {"confirmation_id": confirmation["id"]}
    raise HTTPException(404, "This secure link is not supported")


def _consume_portal_link(conn: sqlite3.Connection, link_id: str, now: str) -> None:
    """Claim a customer-action link exactly once before changing business state."""
    claimed = conn.execute(
        "UPDATE portal_links SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
        (now, link_id),
    )
    if claimed.rowcount != 1:
        raise HTTPException(409, "This secure link has already been used")


@router.get("/public/portal/{token}")
def public_portal(token: str):
    with connect() as conn:
        row = _portal_record(conn, token)
        if row["portal_kind"] == "ESTIMATE_APPROVAL":
            lines = [dict(r) for r in conn.execute("SELECT line_type,description,quantity,unit_price_cents,amount_cents FROM estimate_lines WHERE estimate_id=? ORDER BY sort_order", (row["estimate_id"],)).fetchall()]
            return {
                "kind": "estimate", "organization_name": row["organization_name"], "customer_name": row["customer_name"],
                "estimate_id": row["estimate_id"], "estimate_number": row["estimate_number"], "revision": row["revision"],
                "status": row["estimate_status"], "currency": row["currency"], "subtotal_cents": row["subtotal_cents"],
                "tax_cents": row["tax_cents"], "total_cents": row["total_cents"], "assumptions": row["assumptions"],
                "exclusions": row["exclusions"], "expires_on": row["expires_on"], "link_expires_at": row["expires_at"],
                "consumed": bool(row["consumed_at"]), "lines": lines,
            }
        now = utcnow()
        if not row.get("confirmation_viewed_at"):
            conn.execute("UPDATE service_day_confirmations SET viewed_at=?,updated_at=? WHERE id=?", (now, now, row["confirmation_id"]))
        return {
            "kind": "service_day", "organization_name": row["organization_name"], "customer_name": row["customer_name"],
            "job_number": row["confirmation_job_number"], "description": row["confirmation_description"],
            "location_name": row["confirmation_location_name"], "city": row["confirmation_city"], "state": row["confirmation_state"],
            "scheduled_date": row["confirmation_scheduled_date"], "commitment_type": row["confirmation_commitment_type"],
            "status": row["confirmation_status"], "disclosure_text": row["confirmation_disclosure_text"],
            "confirmed_by_name": row["confirmation_confirmed_by_name"], "confirmed_at": row["confirmation_confirmed_at"],
            "link_expires_at": row["expires_at"], "consumed": bool(row["consumed_at"]),
        }


class PublicPortalDecision(BaseModel):
    command_id: str
    action: str
    signer_name: str | None = None
    signer_title: str | None = None
    note: str | None = None


@router.post("/public/portal/{token}/decision")
def public_portal_decision(token: str, p: PublicPortalDecision):
    with connect() as conn:
        row = _portal_record(conn, token)
        org = row["organization_id"]
        receipt = conn.execute(
            "SELECT result_json FROM command_receipts WHERE command_id=? AND organization_id=?",
            (p.command_id, org),
        ).fetchone()
        if receipt:
            return json.loads(receipt["result_json"])
        if row["consumed_at"]:
            raise HTTPException(409, "This secure link has already been used")
        if row["portal_kind"] == "SERVICE_DAY_CONFIRMATION":
            def service_do():
                current = conn.execute("SELECT * FROM service_day_confirmations WHERE id=? AND organization_id=?", (row["confirmation_id"], org)).fetchone()
                if not current:
                    raise HTTPException(404, "Service-day confirmation not found")
                if current["status"] == "STALE":
                    raise HTTPException(409, "The scheduled date changed. Please use the latest confirmation message.")
                if current["status"] == "CONFIRMED" and p.action == "confirm":
                    return {"state": "CONFIRMED", "already_recorded": True}
                if p.action not in {"confirm", "request_change"}:
                    raise HTTPException(400, "Choose confirm or request a different day")
                now = utcnow(); name = (p.signer_name or "Customer contact").strip() or "Customer contact"
                _consume_portal_link(conn, row["id"], now)
                if p.action == "confirm":
                    state = "CONFIRMED"
                    conn.execute("UPDATE service_day_confirmations SET status=?,confirmed_by_name=?,confirmed_at=?,updated_at=? WHERE id=?", (state, name, now, now, current["id"]))
                    summary = f"{name} confirmed service day {current['scheduled_date']}"
                elif p.action == "request_change":
                    state = "CHANGE_REQUESTED"
                    conn.execute("UPDATE service_day_confirmations SET status=?,change_request_note=?,updated_at=? WHERE id=?", (state, p.note, now, current["id"]))
                    conn.execute("UPDATE jobs SET next_action='Contact customer to coordinate a new service day',version=version+1,updated_at=? WHERE id=?", (now, current["job_id"]))
                    summary = f"Customer requested a different service day from {current['scheduled_date']}"
                audit(conn, org, None, "job", current["job_id"], f"CUSTOMER_SERVICE_DAY_{state}", summary)
                return {"state": state}
            return command_once(conn, org, p.command_id, service_do)

        def estimate_do():
            current = conn.execute("SELECT * FROM estimates WHERE id=? AND organization_id=?", (row["estimate_id"], org)).fetchone()
            if not current:
                raise HTTPException(404, "Estimate not found")
            if current["status"] in ("APPROVED", "DECLINED"):
                return {"state": current["status"], "already_recorded": True}
            if current["status"] in ("CANCELED", "SUPERSEDED"):
                raise HTTPException(409, "This estimate revision is no longer active")
            signer = (p.signer_name or "").strip()
            if not signer:
                raise HTTPException(400, "Enter the name of the person making this decision")
            if p.action not in {"approve", "decline"}:
                raise HTTPException(400, "Choose approve or decline")
            now = utcnow()
            _consume_portal_link(conn, row["id"], now)
            if p.action == "approve":
                state = "APPROVED"
                scope = f"Approved {current['estimate_number']} revision {current['revision']} for {current['total_cents']/100:.2f} {current['currency']}"
                conn.execute(
                    """INSERT INTO authorizations(id,organization_id,customer_id,job_id,estimate_id,kind,scope_text,signer_name,signer_title,method,signature_text,authorized_at,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id("auth"),org,current["customer_id"],current["job_id"],current["id"],"ESTIMATE_APPROVAL",scope,signer,p.signer_title,"SECURE_LINK",signer,now,now),
                )
                if current["job_id"]:
                    conn.execute("UPDATE jobs SET status='APPROVED_UNSCHEDULED',next_action='Schedule approved work',version=version+1,updated_at=? WHERE id=?", (now,current["job_id"]))
                    audit(conn,org,None,"job",current["job_id"],"ESTIMATE_APPROVED",scope)
            elif p.action == "decline":
                state = "DECLINED"
            conn.execute("UPDATE estimates SET status=?,decided_at=?,updated_at=? WHERE id=?", (state,now,now,current["id"]))
            audit(conn,org,None,"estimate",current["id"],f"CUSTOMER_{state}",f"{signer} {state.lower()} {current['estimate_number']} revision {current['revision']} through secure link")
            return {"state": state}
        return command_once(conn, org, p.command_id, estimate_do)


@router.post("/estimates/{estimate_id}/revise")
def revise_estimate(estimate_id: str, request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR", "BILLING")
    org = u["organization_id"]
    with connect() as conn:
        old = conn.execute("SELECT * FROM estimates WHERE id=? AND organization_id=?", (estimate_id, org)).fetchone()
        if not old:
            raise HTTPException(404, "Estimate not found")
        new_rev = old["revision"] + 1
        eid = new_id("est")
        now = utcnow()
        conn.execute(
            """INSERT INTO estimates(id,organization_id,customer_id,job_id,estimate_number,revision,status,currency,subtotal_cents,tax_cents,total_cents,
               assumptions,exclusions,expires_on,supersedes_id,created_by_user_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, org, old["customer_id"], old["job_id"], old["estimate_number"], new_rev, "DRAFT", old["currency"], old["subtotal_cents"], old["tax_cents"], old["total_cents"],
             old["assumptions"], old["exclusions"], old["expires_on"], estimate_id, u["id"], now, now),
        )
        for line in conn.execute("SELECT * FROM estimate_lines WHERE estimate_id=? ORDER BY sort_order", (estimate_id,)).fetchall():
            conn.execute("INSERT INTO estimate_lines(id,estimate_id,line_type,description,quantity,unit_price_cents,amount_cents,sort_order) VALUES(?,?,?,?,?,?,?,?)",
                         (new_id("eline"), eid, line["line_type"], line["description"], line["quantity"], line["unit_price_cents"], line["amount_cents"], line["sort_order"]))
        conn.execute("UPDATE estimates SET status='SUPERSEDED',updated_at=? WHERE id=?", (now, estimate_id))
        return {"id": eid, "estimate_number": old["estimate_number"], "revision": new_rev}


@router.get("/forms/templates")
def form_templates(request: Request):
    u = user_for(request)
    org = u["organization_id"]
    with connect() as conn:
        rows = conn.execute("SELECT * FROM form_templates WHERE organization_id=? AND profile=? AND status='PUBLISHED' ORDER BY category,name", (org, u["organization_profile"])).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["schema"] = loads(d.pop("schema_json"), [])
        result.append(d)
    return result


class FormFieldIn(BaseModel):
    key: str
    label: str
    type: str = "text"
    required: bool = False
    options: list[str] = []


class FormTemplateCreate(BaseModel):
    command_id: str
    name: str
    category: str = "CUSTOM"
    wording: str | None = None
    fields: list[FormFieldIn] = []


@router.post("/forms/templates")
def create_form_template(p: FormTemplateCreate, request: Request):
    u = user_for(request)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR", "RECEPTION")
    org = u["organization_id"]
    allowed_types = {"text", "long_text", "number", "date", "choice", "checkbox"}
    if not p.name.strip():
        raise HTTPException(400, "Give this form a name")
    seen: set[str] = set()
    schema: list[dict[str, Any]] = []
    for field in p.fields:
        key = "_".join(field.key.strip().lower().split())
        if not key or key in seen:
            raise HTTPException(400, "Every form field needs a unique name")
        if field.type not in allowed_types:
            raise HTTPException(400, f"Unsupported form field type: {field.type}")
        if field.type == "choice" and not [x.strip() for x in field.options if x.strip()]:
            raise HTTPException(400, f"Choice field '{field.label}' needs at least one option")
        seen.add(key)
        schema.append({
            "key": key,
            "label": field.label.strip() or key.replace("_", " ").title(),
            "type": field.type,
            "required": field.required,
            "options": [x.strip() for x in field.options if x.strip()],
        })
    with connect() as conn:
        def do():
            tid = new_id("ft")
            now = utcnow()
            conn.execute(
                """INSERT INTO form_templates(id,organization_id,profile,name,category,version,status,wording,schema_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (tid, org, u["organization_profile"], p.name.strip(), p.category.strip().upper() or "CUSTOM", 1,
                 "PUBLISHED", p.wording, json.dumps(schema), now),
            )
            audit(conn, org, u["id"], "form_template", tid, "PUBLISHED", f"Published {p.name.strip()} version 1")
            return {"id": tid, "version": 1, "status": "PUBLISHED"}
        return command_once(conn, org, p.command_id, do)


class FormComplete(BaseModel):
    command_id: str
    template_id: str
    customer_id: str | None = None
    job_id: str | None = None
    equipment_id: str | None = None
    pet_id: str | None = None
    completed_by_name: str | None = None
    values: dict[str, Any] = {}
    signature_text: str | None = None
    submit: bool = True


@router.post("/forms/completions")
def complete_form(p: FormComplete, request: Request):
    u = user_for(request)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            template = conn.execute("SELECT * FROM form_templates WHERE id=? AND organization_id=? AND status='PUBLISHED'", (p.template_id, org)).fetchone()
            if not template:
                raise HTTPException(404, "Form template not found")
            fid = new_id("form")
            now = utcnow()
            state = "COMPLETED" if p.submit else "DRAFT"
            conn.execute(
                """INSERT INTO form_completions(id,organization_id,template_id,template_version,customer_id,job_id,equipment_id,pet_id,
                   completed_by_name,values_json,signature_text,state,completed_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fid, org, p.template_id, template["version"], p.customer_id, p.job_id, p.equipment_id, p.pet_id,
                 p.completed_by_name, json.dumps(p.values), p.signature_text, state, now if p.submit else None, now, now),
            )
            if p.job_id:
                audit(conn, org, u["id"], "job", p.job_id, "FORM_COMPLETED", f"Completed {template['name']}")
            return {"id": fid, "state": state}
        return command_once(conn, org, p.command_id, do)


@router.get("/forms/completions")
def form_completion_list(request: Request, job_id: str | None = None, pet_id: str | None = None):
    u = user_for(request)
    org = u["organization_id"]
    clauses = ["fc.organization_id=?"]
    args: list[Any] = [org]
    if job_id:
        clauses.append("fc.job_id=?")
        args.append(job_id)
    if pet_id:
        clauses.append("fc.pet_id=?")
        args.append(pet_id)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT fc.*,ft.name template_name,ft.category FROM form_completions fc JOIN form_templates ft ON ft.id=fc.template_id
                WHERE {' AND '.join(clauses)} ORDER BY fc.created_at DESC""", args).fetchall()
    out=[]
    for row in rows:
        d=dict(row); d["values"]=loads(d.pop("values_json"),{}); out.append(d)
    return out


@router.get("/inspections/templates")
def inspection_templates(request: Request):
    u = user_for(request)
    require_automotive(u)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM inspection_templates WHERE organization_id=? AND status='PUBLISHED' ORDER BY name,version DESC",
            (u["organization_id"],),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["items"] = loads(d.pop("items_json"), [])
        out.append(d)
    return out


@router.get("/inspections")
def inspections(request: Request, equipment_id: str | None = None, job_id: str | None = None):
    u = user_for(request)
    require_automotive(u)
    org = u["organization_id"]
    where = ["i.organization_id=?"]
    args: list[Any] = [org]
    if equipment_id:
        where.append("i.equipment_id=?")
        args.append(equipment_id)
    if job_id:
        where.append("i.job_id=?")
        args.append(job_id)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT i.*,it.name template_name,e.manufacturer,e.model,e.serial_number,e.bay,c.name customer_name,u.name inspector_name
                FROM inspections i JOIN inspection_templates it ON it.id=i.template_id JOIN equipment e ON e.id=i.equipment_id
                JOIN customers c ON c.id=e.customer_id JOIN users u ON u.id=i.inspector_user_id
                WHERE {' AND '.join(where)} ORDER BY i.inspection_date DESC,i.created_at DESC""",
            args,
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/inspections/{inspection_id}")
def inspection_detail(inspection_id: str, request: Request):
    u = user_for(request)
    require_automotive(u)
    org = u["organization_id"]
    with connect() as conn:
        row = conn.execute(
            """SELECT i.*,it.name template_name,it.procedure_reference,e.manufacturer,e.model,e.serial_number,e.bay,c.name customer_name,u.name inspector_name
               FROM inspections i JOIN inspection_templates it ON it.id=i.template_id JOIN equipment e ON e.id=i.equipment_id
               JOIN customers c ON c.id=e.customer_id JOIN users u ON u.id=i.inspector_user_id
               WHERE i.id=? AND i.organization_id=?""",
            (inspection_id, org),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Inspection not found")
        items = [dict(r) for r in conn.execute("SELECT * FROM inspection_items WHERE inspection_id=? ORDER BY sort_order", (inspection_id,)).fetchall()]
    return {"inspection": dict(row), "items": items}


class InspectionItemIn(BaseModel):
    item_key: str
    label: str
    result: str
    measurement_value: float | None = None
    measurement_unit: str | None = None
    comment: str | None = None


class InspectionCreate(BaseModel):
    command_id: str
    equipment_id: str
    template_id: str
    job_id: str | None = None
    inspection_date: str
    items: list[InspectionItemIn]
    summary: str | None = None
    customer_ack_name: str | None = None
    next_due_date: str | None = None


@router.post("/inspections")
def create_inspection(p: InspectionCreate, request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR", "TECHNICIAN")
    org = u["organization_id"]
    with connect() as conn:
        def do():
            equipment = conn.execute("SELECT * FROM equipment WHERE id=? AND organization_id=?", (p.equipment_id, org)).fetchone()
            if not equipment:
                raise HTTPException(404, "Equipment not found")
            template = conn.execute("SELECT * FROM inspection_templates WHERE id=? AND organization_id=? AND status='PUBLISHED'", (p.template_id, org)).fetchone()
            if not template:
                raise HTTPException(404, "Inspection template not found")
            if template["equipment_category"] and template["equipment_category"] != equipment["category"]:
                raise HTTPException(400, "That inspection template is for a different equipment category")
            if p.job_id:
                job = conn.execute("SELECT * FROM jobs WHERE id=? AND organization_id=? AND equipment_id=?", (p.job_id, org, p.equipment_id)).fetchone()
                if not job:
                    raise HTTPException(400, "The inspection job and equipment do not match")
                if u["role"] == "TECHNICIAN" and not conn.execute(
                    "SELECT 1 FROM visits WHERE job_id=? AND (technician_user_id=? OR helper_user_id=?) LIMIT 1",
                    (p.job_id, u["id"], u["id"]),
                ).fetchone():
                    raise HTTPException(403, "That inspection job is not assigned to you")
            allowed = {"PASS", "FAIL", "NA"}
            if not p.items:
                raise HTTPException(400, "Complete at least one inspection item")
            for item in p.items:
                if item.result.upper() not in allowed:
                    raise HTTPException(400, f"Choose Pass, Fail or N/A for {item.label}")
                if item.result.upper() == "FAIL" and not (item.comment or "").strip():
                    raise HTTPException(400, f"Describe the deficiency for {item.label}")
            result = "FAIL" if any(x.result.upper() == "FAIL" for x in p.items) else "PASS"
            iid = new_id("insp")
            now = utcnow()
            conn.execute(
                """INSERT INTO inspections(id,organization_id,job_id,equipment_id,template_id,template_version,inspector_user_id,
                   inspection_date,state,result,summary,customer_ack_name,next_due_date,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (iid, org, p.job_id, p.equipment_id, p.template_id, template["version"], u["id"], p.inspection_date, "LOCKED", result,
                 p.summary, p.customer_ack_name, p.next_due_date, now, now),
            )
            for index, item in enumerate(p.items):
                conn.execute(
                    """INSERT INTO inspection_items(id,inspection_id,item_key,label,result,measurement_value,measurement_unit,comment,sort_order)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (new_id("ii"), iid, item.item_key, item.label, item.result.upper(), item.measurement_value, item.measurement_unit, item.comment, index),
                )
            if p.next_due_date:
                conn.execute("UPDATE equipment SET inspection_due_date=?,version=version+1,updated_at=? WHERE id=?", (p.next_due_date, now, p.equipment_id))
            if p.job_id:
                audit(conn, org, u["id"], "job", p.job_id, "INSPECTION_RECORDED", f"{template['name']} result: {result}")
            audit(conn, org, u["id"], "equipment", p.equipment_id, "INSPECTION_RECORDED", f"{template['name']} result: {result}")
            return {"id": iid, "result": result, "state": "LOCKED"}
        return command_once(conn, org, p.command_id, do)


@router.post("/files")
async def upload_file(
    request: Request,
    entity_type: str = Form(...),
    entity_id: str = Form(...),
    category: str = Form("DOCUMENT"),
    file: UploadFile = File(...),
):
    u = user_for(request)
    org = u["organization_id"]
    content = await file.read()
    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(413, "File is larger than the 25 MB upload limit")
    from .integrations import scan_upload_if_configured
    try:
        scan_result = scan_upload_if_configured(org, content, file.filename or "file")
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    digest = hashlib.sha256(content).hexdigest()
    suffix = Path(file.filename or "file.bin").suffix[:12]
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM file_records WHERE organization_id=? AND entity_type=? AND entity_id=? AND sha256=?",
            (org, entity_type, entity_id, digest),
        ).fetchone()
        if existing:
            return dict(existing) | {"already_exists": True}
        fid = new_id("file")
        org_dir = FILES_DIR / org
        org_dir.mkdir(parents=True, exist_ok=True)
        stored = f"{fid}{suffix}"
        (org_dir / stored).write_bytes(content)
        conn.execute(
            """INSERT INTO file_records(id,organization_id,entity_type,entity_id,category,original_name,stored_name,mime_type,size_bytes,sha256,created_by_user_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fid, org, entity_type, entity_id, category, file.filename or "file", stored, file.content_type, len(content), digest, u["id"], utcnow()),
        )
        if entity_type == "job":
            audit(conn, org, u["id"], "job", entity_id, "FILE_ADDED", f"Added {file.filename or 'file'}")
    mirror_results = []
    try:
        from .cloud_connectors import mirror_company_file
        mirror_results = mirror_company_file(org, org_dir / stored, local_kind="file", local_id=fid)
    except Exception:
        # A storage mirror is optional. The local authoritative file has already been saved.
        mirror_results = []
    return {"id": fid, "name": file.filename, "size_bytes": len(content), "already_exists": False, "upload_scan": scan_result, "storage_mirrors": mirror_results}


@router.get("/files")
def files(request: Request, entity_type: str, entity_id: str):
    u = user_for(request)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM file_records WHERE organization_id=? AND entity_type=? AND entity_id=? ORDER BY created_at DESC", (u["organization_id"], entity_type, entity_id)).fetchall()
    return [dict(r) for r in rows]


@router.get("/files/{file_id}/download")
def download_file(file_id: str, request: Request):
    u = user_for(request)
    with connect() as conn:
        row = conn.execute("SELECT * FROM file_records WHERE id=? AND organization_id=?", (file_id, u["organization_id"])).fetchone()
    if not row:
        raise HTTPException(404, "File not found")
    path = FILES_DIR / u["organization_id"] / row["stored_name"]
    if not path.exists():
        try:
            from .cloud_connectors import restore_company_file
            restored = restore_company_file(u["organization_id"], file_id, path)
        except Exception:
            restored = None
        if not restored:
            raise HTTPException(404, "The file record exists but no available company storage copy could be restored")
    return FileResponse(path, filename=row["original_name"], media_type=row["mime_type"] or "application/octet-stream")


@router.get("/reports/operations")
def operations_report(request: Request):
    u = user_for(request)
    org = u["organization_id"]
    with connect() as conn:
        job_states = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM jobs WHERE organization_id=? GROUP BY status", (org,)).fetchall()}
        technician_rows = [dict(r) for r in conn.execute(
            """SELECT u.name,COUNT(v.id) visits,COALESCE(SUM((j.estimated_minutes)),0) scheduled_minutes
               FROM users u LEFT JOIN visits v ON v.technician_user_id=u.id AND v.organization_id=u.organization_id
               LEFT JOIN jobs j ON j.id=v.job_id WHERE u.organization_id=? AND u.role='TECHNICIAN' GROUP BY u.id ORDER BY u.name""", (org,)).fetchall()]
        history = conn.execute("SELECT COUNT(*) FROM work_submissions WHERE organization_id=? AND state IN ('APPROVED','LOCKED')", (org,)).fetchone()[0]
        open_parts = conn.execute("SELECT COUNT(*) FROM job_parts WHERE organization_id=? AND status NOT IN ('RECEIVED','RESERVED','LOADED','INSTALLED','RETURNED')", (org,)).fetchone()[0]
        recurring_due = conn.execute("SELECT COUNT(*) FROM recurring_plans WHERE organization_id=? AND status='ACTIVE' AND next_due_date<=?", (org, (date.today()+timedelta(days=45)).isoformat())).fetchone()[0]
        estimates = conn.execute("SELECT COALESCE(SUM(total_cents),0) FROM estimates WHERE organization_id=? AND status='APPROVED'", (org,)).fetchone()[0]
    return {"job_states": job_states, "technicians": technician_rows, "approved_history_count": history,
            "open_parts": open_parts, "recurring_due_45": recurring_due, "approved_estimate_value_cents": estimates}


@router.get("/integrations")
def integrations(request: Request):
    u = user_for(request)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM integration_status WHERE organization_id=? ORDER BY provider", (u["organization_id"],)).fetchall()
    return [dict(r) | {"settings": loads(r["settings_json"], {})} for r in rows]


@router.get("/notifications")
def notifications(request: Request):
    u = user_for(request)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE organization_id=? AND (user_id IS NULL OR user_id=?) ORDER BY created_at DESC LIMIT 50",
            (u["organization_id"], u["id"]),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/notifications/{notification_id}/read")
def mark_notification(notification_id: str, request: Request):
    u = user_for(request)
    with connect() as conn:
        conn.execute("UPDATE notifications SET read_at=? WHERE id=? AND organization_id=? AND (user_id IS NULL OR user_id=?)", (utcnow(), notification_id, u["organization_id"], u["id"]))
    return {"ok": True}


@router.get("/system/status")
def system_status(request: Request):
    u = user_for(request)
    org = u["organization_id"]
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    from .cloud_connectors import organization_database_backup_is_safe_for_offsite
    backup_allowed, _ = organization_database_backup_is_safe_for_offsite(org)
    backups = sorted(BACKUPS_DIR.glob("ServiceSlate-Backup-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) if backup_allowed else []
    usage = shutil.disk_usage(DATA_DIR)
    with connect() as conn:
        schema = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        integrations_rows = conn.execute("SELECT provider,state,last_error,last_success_at FROM integration_status WHERE organization_id=? ORDER BY provider", (org,)).fetchall()
        records = {
            "customers": conn.execute("SELECT COUNT(*) FROM customers WHERE organization_id=?", (org,)).fetchone()[0],
            "jobs": conn.execute("SELECT COUNT(*) FROM jobs WHERE organization_id=?", (org,)).fetchone()[0],
            "files": conn.execute("SELECT COUNT(*) FROM file_records WHERE organization_id=?", (org,)).fetchone()[0],
        }
    return {
        "schema_version": schema[0] if schema else "1",
        "database_path": str(DB_PATH),
        "data_path": str(DATA_DIR),
        "backups": [{"name": p.name, "size_bytes": p.stat().st_size, "modified_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()} for p in backups[:10]],
        "disk_free_bytes": usage.free,
        "records": records,
        "integrations": [dict(r) for r in integrations_rows],
    }


@router.get("/backups/{filename}/download")
def download_backup(filename: str, request: Request):
    u = user_for(request)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    from .cloud_connectors import organization_database_backup_is_safe_for_offsite
    allowed, reason = organization_database_backup_is_safe_for_offsite(u["organization_id"])
    if not allowed:
        raise HTTPException(409, reason)
    if Path(filename).name != filename or not filename.startswith("ServiceSlate-Backup-") or not filename.endswith(".zip"):
        raise HTTPException(400, "Backup filename is not valid")
    path = BACKUPS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Backup not found")
    return FileResponse(path, filename=path.name, media_type="application/zip")


@router.post("/backups/{filename}/verify")
def verify_backup(filename: str, request: Request):
    u = user_for(request)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    from .cloud_connectors import organization_database_backup_is_safe_for_offsite
    allowed, reason = organization_database_backup_is_safe_for_offsite(u["organization_id"])
    if not allowed:
        raise HTTPException(409, reason)
    if Path(filename).name != filename or not filename.startswith("ServiceSlate-Backup-") or not filename.endswith(".zip"):
        raise HTTPException(400, "Backup filename is not valid")
    path = BACKUPS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Backup not found")
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if not ({"serviceslate.db", "serviceslate-postgresql.sql"} & names):
                raise HTTPException(400, "Backup does not contain a ServiceSlate database artifact")
            bad = zf.testzip()
            if bad:
                raise HTTPException(400, f"Backup contains a damaged file: {bad}")
            manifest = {}
            if "backup-manifest.json" in names:
                try:
                    manifest = json.loads(zf.read("backup-manifest.json"))
                except Exception:
                    raise HTTPException(400, "Backup manifest is unreadable")
    except zipfile.BadZipFile as exc:
        raise HTTPException(400, "Backup ZIP is damaged") from exc
    return {"ok": True, "verified": filename, "database_backend": manifest.get("database_backend", "sqlite")}


def _create_safety_backup(label: str = "Pre-Restore") -> Path:
    return create_backup_archive(label)


@router.post("/restore")
async def restore_backup(request: Request, file: UploadFile = File(...)):
    u = user_for(request)
    require_roles(u, "ADMIN", "MANAGER")
    if database_backend() == "postgresql":
        raise HTTPException(409, "Hosted PostgreSQL restore uses the managed restore runbook so active company data is not replaced from a browser request.")
    raw = await read_upload_limited(file, MAX_RESTORE_BYTES)
    temp_dir = DATA_DIR / f".restore-{new_id('tmp')}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    zip_path = temp_dir / "restore.zip"
    zip_path.write_bytes(raw)
    restored_db = temp_dir / "serviceslate.db"
    restored_files = temp_dir / "files"
    try:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                if "serviceslate.db" not in zf.namelist():
                    raise HTTPException(400, "That file is not a ServiceSlate backup")
                members = zf.infolist()
                if len(members) > MAX_RESTORE_MEMBERS:
                    raise HTTPException(400, "Backup contains too many files")
                total_size = sum(member.file_size for member in members)
                if total_size > MAX_RESTORE_EXPANDED_BYTES:
                    raise HTTPException(400, "Backup expands beyond the restore safety limit")
                database_member = zf.getinfo("serviceslate.db")
                if database_member.file_size > MAX_RESTORE_EXPANDED_BYTES:
                    raise HTTPException(400, "Backup database is too large")
                with zf.open(database_member) as source, restored_db.open("wb") as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                for member in zf.infolist():
                    if not member.filename.startswith("files/") or member.is_dir():
                        continue
                    rel = Path(member.filename).relative_to("files")
                    if ".." in rel.parts:
                        raise HTTPException(400, "Backup contains an unsafe file path")
                    target = restored_files / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination, 1024 * 1024)
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "Backup ZIP is damaged") from exc
        check = sqlite3.connect(restored_db)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            required = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            check.close()
        if integrity != "ok" or not {"organizations","users","customers"}.issubset(required):
            raise HTTPException(400, "The backup database could not be verified")
        safety = _create_safety_backup()
        for suffix in ("-wal", "-shm"):
            Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
        replacement = DATA_DIR / ".serviceslate-restored.db"
        shutil.copy2(restored_db, replacement)
        replacement.replace(DB_PATH)
        old_files = DATA_DIR / ".files-before-restore"
        if old_files.exists():
            shutil.rmtree(old_files)
        if FILES_DIR.exists():
            FILES_DIR.replace(old_files)
        if restored_files.exists():
            shutil.copytree(restored_files, FILES_DIR)
        else:
            FILES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(old_files, ignore_errors=True)
        # Apply any newer application migrations after restoring an older valid backup.
        from .db import init_db
        init_db()
        request.session.clear()
        return {"ok":True,"message":"Backup restored. Sign in again.","safety_backup":safety.name}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.get("/export/all")
def export_all(request: Request):
    u = user_for(request)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    org = u["organization_id"]
    export_dir = DATA_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = export_dir / f"ServiceSlate-Export-{stamp}.zip"
    tables = [
        "customers", "contacts", "locations", "equipment", "jobs", "visits", "work_submissions", "recommendations",
        "followups", "recurring_plans", "parts", "job_parts", "vehicles", "vehicle_inventory", "estimates", "estimate_lines",
        "authorizations", "form_completions", "file_records", "audit_events", "pets", "grooming_appointments", "waitlist_requests", "rebooking_obligations",
    ]
    with connect() as conn, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {"organization_id": org, "exported_at": utcnow(), "tables": {}}
        for table in tables:
            if database_backend() == "postgresql":
                cols = {r["column_name"] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=?", (table,)).fetchall()}
            else:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "organization_id" in cols:
                rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE organization_id=?", (org,)).fetchall()]
            elif table == "estimate_lines":
                rows = [dict(r) for r in conn.execute("SELECT el.* FROM estimate_lines el JOIN estimates e ON e.id=el.estimate_id WHERE e.organization_id=?", (org,)).fetchall()]
            else:
                rows = []
            manifest["tables"][table] = len(rows)
            zf.writestr(f"data/{table}.json", json.dumps(rows, indent=2, default=str))
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for row in conn.execute("SELECT stored_name,original_name FROM file_records WHERE organization_id=?", (org,)).fetchall():
            src = FILES_DIR / org / row["stored_name"]
            if src.exists():
                zf.write(src, f"files/{row['stored_name']}")
    return FileResponse(path, filename=path.name, media_type="application/zip")


@router.post("/import/customers/preview")
async def import_customers_preview(request: Request, file: UploadFile = File(...)):
    u = user_for(request)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    org = u["organization_id"]
    raw = await read_upload_limited(file)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Use a UTF-8 CSV file") from exc
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if len(rows) > MAX_CSV_ROWS:
        raise HTTPException(400, f"CSV contains more than the {MAX_CSV_ROWS:,} row import limit")
    if not rows:
        raise HTTPException(400, "No customer rows were found")
    bid = new_id("import")
    ready = issues = 0
    now = utcnow()
    with connect() as conn:
        # Create the parent batch first so staged rows always satisfy FK integrity.
        conn.execute(
            """INSERT INTO import_batches(id,organization_id,source_type,filename,status,row_count,ready_count,issue_count,created_by_user_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (bid, org, "CUSTOMER_CSV", file.filename, "STAGED", len(rows), 0, 0, u["id"], now),
        )
        for index, raw_row in enumerate(rows, start=2):
            normalized = {k.strip().lower(): (v or "").strip() for k, v in raw_row.items() if k}
            name = normalized.get("name") or normalized.get("customer") or normalized.get("customer name")
            issue = None
            match = None
            state = "READY"
            if not name:
                issue = "Missing customer name"
                state = "ISSUE"
            else:
                match = conn.execute("SELECT id,name FROM customers WHERE organization_id=? AND lower(name)=lower(?)", (org, name)).fetchone()
                if match:
                    issue = f"Possible existing customer: {match['name']}"
                    state = "REVIEW"
            if state == "READY": ready += 1
            else: issues += 1
            conn.execute(
                """INSERT INTO import_rows(id,batch_id,row_number,raw_json,normalized_json,state,issue,matched_customer_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (new_id("irow"), bid, index, json.dumps(raw_row), json.dumps(normalized), state, issue, match["id"] if match else None),
            )
        conn.execute("UPDATE import_batches SET ready_count=?,issue_count=? WHERE id=?", (ready, issues, bid))
    return {"batch_id": bid, "row_count": len(rows), "ready_count": ready, "issue_count": issues}



def _first_value(row: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value and value.strip():
            return value.strip()
    return None


@router.post("/import/work-history/preview")
async def import_work_history_preview(request: Request, file: UploadFile = File(...), source_system: str = Form("Legacy")):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    org = u["organization_id"]
    raw = await read_upload_limited(file)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Use a UTF-8 CSV file") from exc
    reader = csv.DictReader(io.StringIO(text))
    source_rows = list(reader)
    if len(source_rows) > MAX_CSV_ROWS:
        raise HTTPException(400, f"CSV contains more than the {MAX_CSV_ROWS:,} row import limit")
    if not source_rows:
        raise HTTPException(400, "No work-history rows were found")
    bid = new_id("import")
    ready = issues = 0
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """INSERT INTO import_batches(id,organization_id,source_type,filename,status,row_count,ready_count,issue_count,created_by_user_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (bid, org, "WORK_HISTORY_CSV", file.filename, "STAGED", len(source_rows), 0, 0, u["id"], now),
        )
        for index, raw_row in enumerate(source_rows, start=2):
            n = {k.strip().lower(): (v or "").strip() for k, v in raw_row.items() if k}
            customer_name = _first_value(n, "customer", "customer name", "account", "company")
            source_ref = _first_value(n, "work order", "work_order", "wo", "job", "job number", "reference")
            serial = _first_value(n, "serial", "serial number", "equipment serial")
            work_date = _first_value(n, "date", "work date", "completed date", "service date")
            tech = _first_value(n, "technician", "tech", "performed by")
            normalized = {
                "source_system": source_system.strip() or "Legacy",
                "customer": customer_name,
                "source_reference": source_ref,
                "performed_at": work_date,
                "technician": tech,
                "serial": serial,
                "manufacturer": _first_value(n, "manufacturer", "make"),
                "model": _first_value(n, "model"),
                "complaint": _first_value(n, "complaint", "problem", "reported problem", "description"),
                "finding": _first_value(n, "finding", "findings", "diagnosis", "diagnostic"),
                "cause": _first_value(n, "cause", "root cause"),
                "correction": _first_value(n, "correction", "work performed", "repair", "resolution", "completed work", "notes"),
                "verification": _first_value(n, "verification", "tested", "test result"),
            }
            issue = None
            state = "READY"
            customer = None
            equipment = None
            if not customer_name:
                issue, state = "Missing customer name", "ISSUE"
            else:
                customer = conn.execute("SELECT id,name FROM customers WHERE organization_id=? AND lower(name)=lower(?)", (org, customer_name)).fetchone()
                if not customer:
                    issue, state = f"Customer not found: {customer_name}", "REVIEW"
            if customer and serial:
                equipment = conn.execute(
                    "SELECT id,serial_number FROM equipment WHERE organization_id=? AND customer_id=? AND lower(serial_number)=lower(?)",
                    (org, customer["id"], serial),
                ).fetchone()
                if not equipment:
                    issue, state = f"Equipment serial not found for this customer: {serial}", "REVIEW"
            if customer and source_ref:
                duplicate = conn.execute(
                    "SELECT id FROM jobs WHERE organization_id=? AND data_origin='legacy_import' AND source_reference=?",
                    (org, f"{source_system.strip() or 'Legacy'}:{source_ref}"),
                ).fetchone()
                if duplicate:
                    issue, state = f"Already imported: {source_ref}", "REVIEW"
            normalized["equipment_id"] = equipment["id"] if equipment else None
            if state == "READY":
                ready += 1
            else:
                issues += 1
            conn.execute(
                """INSERT INTO import_rows(id,batch_id,row_number,raw_json,normalized_json,state,issue,matched_customer_id)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (new_id("irow"), bid, index, json.dumps(raw_row), json.dumps(normalized), state, issue, customer["id"] if customer else None),
            )
        conn.execute("UPDATE import_batches SET ready_count=?,issue_count=? WHERE id=?", (ready, issues, bid))
    return {"batch_id": bid, "row_count": len(source_rows), "ready_count": ready, "issue_count": issues, "source_type": "WORK_HISTORY_CSV"}


class WorkHistoryCommit(BaseModel):
    command_id: str


@router.post("/import/work-history/{batch_id}/commit")
def import_work_history_commit(batch_id: str, p: WorkHistoryCommit, request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    org = u["organization_id"]
    with connect() as conn:
        def do():
            batch = conn.execute("SELECT * FROM import_batches WHERE id=? AND organization_id=? AND source_type IN ('WORK_HISTORY_CSV','FASTFIELD_EMAIL')", (batch_id, org)).fetchone()
            if not batch:
                raise HTTPException(404, "Work-history review batch not found")
            if batch["status"] == "COMMITTED":
                return {"created": 0, "already_committed": True}
            rows = conn.execute("SELECT * FROM import_rows WHERE batch_id=? AND state='READY' ORDER BY row_number", (batch_id,)).fetchall()
            created = 0
            now = utcnow()
            for row in rows:
                n = loads(row["normalized_json"], {})
                customer_id = row["matched_customer_id"]
                location = conn.execute("SELECT id FROM locations WHERE organization_id=? AND customer_id=? ORDER BY created_at LIMIT 1", (org, customer_id)).fetchone()
                if not location:
                    conn.execute("UPDATE import_rows SET state='ISSUE',issue='Customer has no service location' WHERE id=?", (row["id"],))
                    continue
                source_system = n.get("source_system") or "Legacy"
                source_ref = n.get("source_reference") or f"row-{row['row_number']}"
                combined_ref = f"{source_system}:{source_ref}"
                if conn.execute("SELECT 1 FROM jobs WHERE organization_id=? AND data_origin='legacy_import' AND source_reference=?", (org, combined_ref)).fetchone():
                    conn.execute("UPDATE import_rows SET state='DUPLICATE',issue='Already imported' WHERE id=?", (row["id"],))
                    continue
                jid = new_id("job")
                sid = new_id("sub")
                safe_ref = "".join(ch for ch in str(source_ref) if ch.isalnum() or ch in "-_./")[:40]
                job_number = f"LEGACY-{safe_ref}" if safe_ref else f"LEGACY-{row['row_number']}"
                if conn.execute("SELECT 1 FROM jobs WHERE organization_id=? AND job_number=?", (org, job_number)).fetchone():
                    job_number = f"LEGACY-{batch_id[-6:]}-{row['row_number']}"
                performed = n.get("performed_at") or now
                if len(performed) == 10:
                    performed = f"{performed}T12:00:00"
                description = n.get("complaint") or n.get("correction") or f"Imported work order {source_ref}"
                conn.execute(
                    """INSERT INTO jobs(id,organization_id,customer_id,location_id,equipment_id,job_number,job_type,description,status,priority,
                       owner_user_id,next_action,due_date,estimated_minutes,crew_min,crew_recommended,simultaneous_crew_minutes,parts_status,commitment_type,
                       data_origin,source_reference,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (jid, org, customer_id, location["id"], n.get("equipment_id"), job_number, "Historical Import", description, "CLOSED", "NORMAL",
                     u["id"], "Historical record", None, 60, 1, 1, 0, "READY", "FLEXIBLE_DAY", "legacy_import", combined_ref, performed, now),
                )
                conn.execute(
                    """INSERT INTO work_submissions(id,organization_id,job_id,technician_user_id,state,complaint,finding,cause,correction,verification,
                       internal_note,customer_report,outcome,measurements_json,parts_json,recommendations_json,submitted_at,reviewed_by_user_id,reviewed_at,
                       review_note,data_origin,source_system,source_reference,performed_by_text,performed_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sid, org, jid, u["id"], "LOCKED", n.get("complaint"), n.get("finding"), n.get("cause"), n.get("correction"), n.get("verification"),
                     f"Imported from {source_system}. Original reference: {source_ref}", n.get("correction"), "COMPLETED", "[]", "[]", "[]", performed,
                     u["id"], now, "Legacy import reviewed at staging", "legacy_import", source_system, str(source_ref), n.get("technician") or "Legacy record", performed, performed, now),
                )
                audit(conn, org, u["id"], "job", jid, "LEGACY_IMPORTED", f"Imported {source_system} work record {source_ref}")
                conn.execute("UPDATE import_rows SET state='COMMITTED',created_record_id=?,review_state='HUMAN_APPROVED',reviewed_by_user_id=?,reviewed_at=? WHERE id=?", (jid, u["id"], now, row["id"]))
                created += 1
            remaining = conn.execute("SELECT COUNT(*) FROM import_rows WHERE batch_id=? AND state IN ('READY','REVIEW','ISSUE')", (batch_id,)).fetchone()[0]
            status = "COMMITTED" if remaining == 0 else "PARTIAL"
            conn.execute("UPDATE import_batches SET status=?,committed_at=? WHERE id=?", (status, now, batch_id))
            return {"created": created, "status": status}
        return command_once(conn, org, p.command_id, do)


@router.get("/import/{batch_id}")
def import_batch(batch_id: str, request: Request):
    u = user_for(request)
    with connect() as conn:
        batch = conn.execute("SELECT * FROM import_batches WHERE id=? AND organization_id=?", (batch_id, u["organization_id"])).fetchone()
        if not batch:
            raise HTTPException(404, "Import batch not found")
        rows = [dict(r) for r in conn.execute("SELECT * FROM import_rows WHERE batch_id=? ORDER BY row_number", (batch_id,)).fetchall()]
    for row in rows:
        row["normalized"] = loads(row.pop("normalized_json"), {})
        row.pop("raw_json", None)
    return {"batch": dict(batch), "rows": rows}


class ImportCommit(BaseModel):
    command_id: str


@router.post("/import/{batch_id}/commit")
def import_commit(batch_id: str, p: ImportCommit, request: Request):
    u = user_for(request)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    org = u["organization_id"]
    with connect() as conn:
        def do():
            batch = conn.execute("SELECT * FROM import_batches WHERE id=? AND organization_id=?", (batch_id, org)).fetchone()
            if not batch:
                raise HTTPException(404, "Import batch not found")
            if batch["status"] == "COMMITTED":
                return {"created": 0, "already_committed": True}
            if batch["source_type"] in ("WORK_HISTORY_CSV", "FASTFIELD_EMAIL"):
                raise HTTPException(409, "This batch contains work history. Review it with the work-history approval action.")
            created = 0
            now = utcnow()
            rows = conn.execute("SELECT * FROM import_rows WHERE batch_id=? AND state='READY' ORDER BY row_number", (batch_id,)).fetchall()
            for row in rows:
                n = loads(row["normalized_json"], {})
                name = n.get("name") or n.get("customer") or n.get("customer name")
                if not name:
                    continue
                cid = new_id("cust")
                lid = new_id("loc")
                conn.execute("INSERT INTO customers(id,organization_id,name,phone,email,website,business_type,data_origin,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (cid, org, name, n.get("phone"), n.get("email"), n.get("website"), n.get("business type") or n.get("type"), "import", now, now))
                conn.execute("INSERT INTO locations(id,organization_id,customer_id,name,address1,city,state,postal_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (lid, org, cid, n.get("location name") or "Primary Location", n.get("address") or n.get("address1"), n.get("city"), n.get("state"), n.get("zip") or n.get("postal code"), now, now))
                conn.execute("UPDATE import_rows SET state='COMMITTED',created_record_id=?,review_state='HUMAN_APPROVED',reviewed_by_user_id=?,reviewed_at=? WHERE id=?", (cid, u["id"], now, row["id"]))
                created += 1
            conn.execute("UPDATE import_batches SET status='COMMITTED',committed_at=? WHERE id=?", (now, batch_id))
            return {"created": created}
        return command_once(conn, org, p.command_id, do)


class VehicleCreate(BaseModel):
    unit_number: str
    description: str | None = None
    branch: str | None = None
    assigned_user_id: str | None = None
    availability: str = "AVAILABLE"
    notes: str | None = None


@router.post("/vehicles")
def create_vehicle(p: VehicleCreate, request: Request):
    u = user_for(request)
    require_automotive(u)
    require_roles(u, "ADMIN", "MANAGER", "COORDINATOR")
    org = u["organization_id"]
    now = utcnow()
    with connect() as conn:
        if conn.execute("SELECT 1 FROM vehicles WHERE organization_id=? AND unit_number=?", (org, p.unit_number)).fetchone():
            raise HTTPException(409, "That truck/unit number already exists")
        vid = new_id("veh")
        conn.execute("INSERT INTO vehicles(id,organization_id,unit_number,description,branch,assigned_user_id,availability,soft_inventory_json,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (vid, org, p.unit_number, p.description, p.branch, p.assigned_user_id, p.availability, "{}", p.notes, now, now))
    return {"id": vid}


class VehicleStock(BaseModel):
    command_id: str
    item_name: str
    expected_qty: float | None = None
    confirmed_qty: float | None = None
    state: str = "EXPECTED"
    notes: str | None = None


@router.post("/vehicles/{vehicle_id}/stock")
def vehicle_stock(vehicle_id: str, p: VehicleStock, request: Request):
    u = user_for(request)
    require_automotive(u)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            if not conn.execute("SELECT 1 FROM vehicles WHERE id=? AND organization_id=?", (vehicle_id, org)).fetchone():
                raise HTTPException(404, "Vehicle not found")
            now = utcnow()
            existing = conn.execute("SELECT id FROM vehicle_inventory WHERE vehicle_id=? AND lower(item_name)=lower(?)", (vehicle_id, p.item_name)).fetchone()
            if existing:
                conn.execute("UPDATE vehicle_inventory SET expected_qty=?,confirmed_qty=?,state=?,last_confirmed_at=?,notes=?,updated_at=? WHERE id=?",
                             (p.expected_qty, p.confirmed_qty, p.state, now if p.confirmed_qty is not None else None, p.notes, now, existing["id"]))
                iid = existing["id"]
            else:
                iid = new_id("vinv")
                conn.execute("INSERT INTO vehicle_inventory(id,organization_id,vehicle_id,item_name,expected_qty,confirmed_qty,state,last_confirmed_at,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             (iid, org, vehicle_id, p.item_name, p.expected_qty, p.confirmed_qty, p.state, now if p.confirmed_qty is not None else None, p.notes, now, now))
            return {"id": iid}
        return command_once(conn, org, p.command_id, do)


@router.get("/vehicles/{vehicle_id}/stock")
def vehicle_stock_list(vehicle_id: str, request: Request):
    u = user_for(request)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM vehicle_inventory WHERE vehicle_id=? AND organization_id=? ORDER BY item_name", (vehicle_id, u["organization_id"])).fetchall()
    return [dict(r) for r in rows]


class PetCreate(BaseModel):
    customer_id: str
    name: str
    breed: str | None = None
    weight_lbs: float | None = None
    coat_type: str | None = None
    preferred_groomer_user_id: str | None = None
    safety_notes: str | None = None
    recurring_weeks: int | None = None


@router.post("/grooming/pets")
def create_pet(p: PetCreate, request: Request):
    u = user_for(request)
    require_grooming(u)
    org = u["organization_id"]
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM customers WHERE id=? AND organization_id=?", (p.customer_id, org)).fetchone():
            raise HTTPException(404, "Customer not found")
        pid = new_id("pet")
        now = utcnow()
        conn.execute("INSERT INTO pets(id,organization_id,customer_id,name,breed,weight_lbs,coat_type,preferred_groomer_user_id,safety_notes,recurring_weeks,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, org, p.customer_id, p.name, p.breed, p.weight_lbs, p.coat_type, p.preferred_groomer_user_id, p.safety_notes, p.recurring_weeks, now, now))
    return {"id": pid}


@router.get("/grooming/services")
def grooming_services(request: Request):
    u = user_for(request)
    require_grooming(u)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM grooming_services WHERE organization_id=? AND active=1 ORDER BY name", (u["organization_id"],)).fetchall()
    return [dict(r) for r in rows]


@router.get("/grooming/waitlist")
def grooming_waitlist(request: Request):
    u = user_for(request)
    require_grooming(u)
    with connect() as conn:
        rows = conn.execute(
            """SELECT w.*,c.name customer_name,p.name pet_name,p.breed,u.name preferred_groomer_name FROM waitlist_requests w
               JOIN customers c ON c.id=w.customer_id JOIN pets p ON p.id=w.pet_id LEFT JOIN users u ON u.id=w.preferred_groomer_user_id
               WHERE w.organization_id=? AND w.status='WAITING' ORDER BY w.earliest_at""", (u["organization_id"],)).fetchall()
    return [dict(r) for r in rows]


class WaitlistCreate(BaseModel):
    command_id: str
    pet_id: str
    service_name: str
    preferred_groomer_user_id: str | None = None
    earliest_at: str | None = None
    latest_at: str | None = None
    notes: str | None = None


@router.post("/grooming/waitlist")
def add_waitlist(p: WaitlistCreate, request: Request):
    u = user_for(request)
    require_grooming(u)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            pet = conn.execute("SELECT * FROM pets WHERE id=? AND organization_id=?", (p.pet_id, org)).fetchone()
            if not pet:
                raise HTTPException(404, "Pet not found")
            wid = new_id("wait")
            now = utcnow()
            conn.execute("INSERT INTO waitlist_requests(id,organization_id,customer_id,pet_id,service_name,preferred_groomer_user_id,earliest_at,latest_at,notes,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (wid, org, pet["customer_id"], p.pet_id, p.service_name, p.preferred_groomer_user_id, p.earliest_at, p.latest_at, p.notes, "WAITING", now, now))
            return {"id": wid}
        return command_once(conn, org, p.command_id, do)


@router.get("/grooming/rebooking")
def grooming_rebooking(request: Request):
    u = user_for(request)
    require_grooming(u)
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*,c.name customer_name,p.name pet_name,p.breed FROM rebooking_obligations r
               JOIN customers c ON c.id=r.customer_id JOIN pets p ON p.id=r.pet_id
               WHERE r.organization_id=? AND r.status='DUE' ORDER BY r.due_date""", (u["organization_id"],)).fetchall()
    return [dict(r) for r in rows]


@router.get("/grooming/appointments/{appointment_id}")
def grooming_appointment_detail(appointment_id: str, request: Request):
    u = user_for(request)
    require_grooming(u)
    org = u["organization_id"]
    with connect() as conn:
        row = conn.execute(
            """SELECT a.*,c.name customer_name,c.phone customer_phone,p.name pet_name,p.breed,p.weight_lbs,p.coat_type,p.safety_notes,
                      p.recurring_weeks,u.name groomer_name
               FROM grooming_appointments a JOIN customers c ON c.id=a.customer_id JOIN pets p ON p.id=a.pet_id
               LEFT JOIN users u ON u.id=a.groomer_user_id WHERE a.id=? AND a.organization_id=?""",
            (appointment_id, org),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Appointment not found")
    if u["role"] == "GROOMER" and row["groomer_user_id"] != u["id"] and row["status"] not in ("COMPLETED", "CANCELED", "NO_SHOW"):
        raise HTTPException(403, "That appointment is assigned to another groomer")
    return dict(row)


class GroomCancel(BaseModel):
    command_id: str
    reason: str | None = None


@router.post("/grooming/appointments/{appointment_id}/cancel")
def cancel_grooming(appointment_id: str, p: GroomCancel, request: Request):
    u = user_for(request)
    require_grooming(u)
    require_roles(u, "ADMIN", "MANAGER", "RECEPTION")
    org = u["organization_id"]
    with connect() as conn:
        def do():
            appt = conn.execute("SELECT * FROM grooming_appointments WHERE id=? AND organization_id=?", (appointment_id, org)).fetchone()
            if not appt:
                raise HTTPException(404, "Appointment not found")
            if appt["status"] in ("COMPLETED", "CANCELED"):
                raise HTTPException(409, "That appointment is already closed")
            now = utcnow()
            note = p.reason.strip() if p.reason else None
            conn.execute(
                "UPDATE grooming_appointments SET status='CANCELED',notes=CASE WHEN ? IS NULL THEN notes WHEN notes IS NULL OR notes='' THEN ? ELSE notes || '\\nCancellation: ' || ? END,updated_at=? WHERE id=?",
                (note, note, note, now, appointment_id),
            )
            matches = conn.execute(
                """SELECT w.id,c.name customer_name,p.name pet_name,p.breed,w.service_name,w.notes
                   FROM waitlist_requests w JOIN customers c ON c.id=w.customer_id JOIN pets p ON p.id=w.pet_id
                   WHERE w.organization_id=? AND w.status='WAITING' AND lower(w.service_name)=lower(?)
                     AND (w.preferred_groomer_user_id IS NULL OR w.preferred_groomer_user_id=?)
                     AND (w.earliest_at IS NULL OR w.earliest_at<=?) AND (w.latest_at IS NULL OR w.latest_at>=?)
                   ORDER BY CASE WHEN w.preferred_groomer_user_id=? THEN 0 ELSE 1 END,w.created_at LIMIT 10""",
                (org, appt["service_name"], appt["groomer_user_id"], appt["start_at"], appt["start_at"], appt["groomer_user_id"]),
            ).fetchall()
            audit(conn, org, u["id"], "grooming_appointment", appointment_id, "CANCELED", note or "Appointment canceled")
            return {"state": "CANCELED", "waitlist_matches": [dict(r) for r in matches]}
        return command_once(conn, org, p.command_id, do)


class GroomComplete(BaseModel):
    command_id: str
    checkout_note: str | None = None
    rebook_weeks: int | None = None


@router.post("/grooming/appointments/{appointment_id}/complete")
def complete_grooming(appointment_id: str, p: GroomComplete, request: Request):
    u = user_for(request)
    require_grooming(u)
    org = u["organization_id"]
    with connect() as conn:
        def do():
            appt = conn.execute("SELECT * FROM grooming_appointments WHERE id=? AND organization_id=?", (appointment_id, org)).fetchone()
            if not appt:
                raise HTTPException(404, "Appointment not found")
            if u["role"] not in ("ADMIN", "MANAGER", "COORDINATOR") and not (u["role"] == "GROOMER" and appt["groomer_user_id"] == u["id"]):
                raise HTTPException(403, "Only the assigned groomer or office staff can complete this appointment")
            now = utcnow()
            conn.execute("UPDATE grooming_appointments SET status='COMPLETED',checkout_note=?,completed_at=?,updated_at=? WHERE id=?", (p.checkout_note, now, now, appointment_id))
            weeks = p.rebook_weeks
            if weeks:
                due = (date.today() + timedelta(weeks=weeks)).isoformat()
                conn.execute("INSERT INTO rebooking_obligations(id,organization_id,customer_id,pet_id,service_name,due_date,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                             (new_id("rebook"), org, appt["customer_id"], appt["pet_id"], appt["service_name"], due, "DUE", now, now))
            return {"state": "COMPLETED"}
        return command_once(conn, org, p.command_id, do)
