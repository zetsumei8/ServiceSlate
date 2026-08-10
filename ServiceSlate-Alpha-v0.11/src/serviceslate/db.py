from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sqlite3
import zipfile
import shutil
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("SERVICESLATE_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "serviceslate.db"
DATABASE_URL = os.environ.get("SERVICESLATE_DATABASE_URL", "").strip()
FILES_DIR = DATA_DIR / "files"
BACKUPS_DIR = DATA_DIR / "backups"
CURRENT_SCHEMA_VERSION = "17"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def hash_password(password: str) -> str:
    """Create a password hash using the application's local authentication scheme."""
    return _hash_password(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = encoded.split("$", 2)
        if scheme != "scrypt":
            return False
        check = _hash_password(password, bytes.fromhex(salt_hex)).split("$", 2)[2]
        return secrets.compare_digest(check, digest_hex)
    except Exception:
        return False


def create_backup_archive(label: str | None = None) -> Path:
    """Create a consistent database + file backup for local SQLite or hosted PostgreSQL."""
    DATA_DIR.mkdir(parents=True, exist_ok=True); BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_label = "" if not label else "-" + "".join(c for c in label if c.isalnum() or c in ("-", "_"))[:40]
    path = BACKUPS_DIR / f"ServiceSlate-Backup-{stamp}{safe_label}.zip"
    temp_artifact: Path | None = None
    archive_name = "serviceslate.db"
    try:
        if database_backend() == "postgresql":
            temp_artifact = DATA_DIR / f".backup-{uuid.uuid4().hex}.sql"; archive_name = "serviceslate-postgresql.sql"
            pg_dump = shutil.which("pg_dump")
            if not pg_dump:
                raise RuntimeError("PostgreSQL backup requires pg_dump on the hosted ServiceSlate machine")
            proc = subprocess.run([pg_dump, "--no-owner", "--no-privileges", "--file", str(temp_artifact), DATABASE_URL], capture_output=True, text=True, timeout=300, check=False)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "pg_dump failed")
        else:
            temp_artifact = DATA_DIR / f".backup-{uuid.uuid4().hex}.db"
            src = sqlite3.connect(DB_PATH, timeout=15); dst = sqlite3.connect(temp_artifact)
            try: src.backup(dst)
            finally: dst.close(); src.close()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(temp_artifact, archive_name)
            manifest = {"database_backend": database_backend(), "schema_version": CURRENT_SCHEMA_VERSION, "created_at": utcnow()}
            zf.writestr("backup-manifest.json", json.dumps(manifest, indent=2))
            if FILES_DIR.exists():
                for f in FILES_DIR.rglob("*"):
                    if f.is_file(): zf.write(f, Path("files") / f.relative_to(FILES_DIR))
    finally:
        if temp_artifact: temp_artifact.unlink(missing_ok=True)
    return path


def ensure_automatic_backup() -> Path | None:
    """Create at most one automatic backup per day and keep the most recent 14."""
    if database_backend() == "sqlite" and not DB_PATH.exists():
        return None
    today = datetime.now().strftime("%Y%m%d")
    existing = sorted(BACKUPS_DIR.glob(f"ServiceSlate-Backup-{today}-*-Auto.zip")) if BACKUPS_DIR.exists() else []
    if existing:
        return existing[-1]
    path = create_backup_archive("Auto")
    autos = sorted(BACKUPS_DIR.glob("ServiceSlate-Backup-*-Auto.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
    retention_days = max(7, int(os.environ.get("SERVICESLATE_BACKUP_RETENTION_DAYS", "30")))
    cutoff = datetime.now().timestamp() - retention_days * 86400
    # Keep at least the seven newest recovery points even if their timestamps are unusual.
    keep = set(autos[:7])
    for old in autos[7:]:
        if old not in keep and old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    return path


def _schema_version_on_disk() -> str | None:
    if database_backend() == "postgresql":
        try:
            with connect() as conn:
                row = conn.execute("SELECT to_regclass('public.organizations') AS table_name").fetchone()
                if not row or not row["table_name"]: return None
                meta = conn.execute("SELECT to_regclass('public.schema_meta') AS table_name").fetchone()
                if not meta or not meta["table_name"]: return "legacy"
                version = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                return version["value"] if version else "legacy"
        except Exception:
            return None
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return None
    conn = sqlite3.connect(DB_PATH)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "organizations" not in tables: return None
        if "schema_meta" not in tables: return "legacy"
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        return row[0] if row else "legacy"
    finally: conn.close()


def database_backend() -> str:
    return "postgresql" if DATABASE_URL.lower().startswith(("postgresql://", "postgres://")) else "sqlite"


def _qmark_to_percent(sql: str) -> str:
    """Translate sqlite qmark parameters to psycopg format without touching quoted question marks."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.append(sql[i + 1]); i += 1
                else:
                    quote = None
        else:
            if ch in ("'", '"'):
                quote = ch; out.append(ch)
            elif ch == "?":
                out.append("%s")
            else:
                out.append(ch)
        i += 1
    return "".join(out)


def _postgres_sql(sql: str) -> str:
    cleaned = sql.strip()
    cleaned = cleaned.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    if cleaned.upper().startswith("INSERT OR IGNORE INTO"):
        cleaned = "INSERT INTO" + cleaned[len("INSERT OR IGNORE INTO"):]
        if " ON CONFLICT " not in cleaned.upper():
            semi = ";" if cleaned.endswith(";") else ""
            cleaned = cleaned[:-1] if semi else cleaned
            cleaned += " ON CONFLICT DO NOTHING" + semi
    return _qmark_to_percent(cleaned)


class HybridRow(dict):
    """PostgreSQL row compatible with sqlite3.Row's name and numeric access."""

    def __init__(self, names: Sequence[str], values: Sequence[Any]):
        super().__init__(zip(names, values, strict=False))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def hybrid_row_factory(cursor):
    names = [column.name for column in cursor.description]

    def make_row(values):
        return HybridRow(names, values)

    return make_row


class PostgresCompatConnection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql: str, params: tuple | list = ()):
        return self.raw.execute(_postgres_sql(sql), params)

    def executescript(self, script: str) -> None:
        # ServiceSlate schema DDL contains no procedural blocks; semicolon splitting is sufficient.
        for statement in script.split(";"):
            if statement.strip():
                self.raw.execute(_postgres_sql(statement))

    def commit(self) -> None: self.raw.commit()
    def rollback(self) -> None: self.raw.rollback()
    def close(self) -> None: self.raw.close()


@contextmanager
def connect() -> Iterator[Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    if database_backend() == "postgresql":
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - production dependency gate
            raise RuntimeError("PostgreSQL mode requires psycopg") from exc
        raw = psycopg.connect(DATABASE_URL, row_factory=hybrid_row_factory)
        conn: Any = PostgresCompatConnection(raw)
    else:
        raw = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA journal_mode = WAL")
        conn = raw
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = r'''
CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    profile TEXT NOT NULL CHECK(profile IN ('automotive_equipment','grooming')),
    is_demo INTEGER NOT NULL DEFAULT 0,
    timezone TEXT NOT NULL DEFAULT 'America/Los_Angeles',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    qualifications_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    website TEXT,
    business_type TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    data_origin TEXT NOT NULL DEFAULT 'manual',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_org_name ON customers(organization_id, name);

CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    name TEXT NOT NULL,
    address1 TEXT,
    city TEXT,
    state TEXT,
    postal_code TEXT,
    latitude REAL,
    longitude REAL,
    notes TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equipment (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    location_id TEXT NOT NULL REFERENCES locations(id),
    bay TEXT,
    category TEXT NOT NULL,
    manufacturer TEXT,
    model TEXT,
    serial_number TEXT,
    install_date TEXT,
    warranty_until TEXT,
    operational_status TEXT NOT NULL DEFAULT 'ACTIVE',
    notes TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_equipment_serial ON equipment(organization_id, serial_number);

CREATE TABLE IF NOT EXISTS vehicles (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    unit_number TEXT NOT NULL,
    description TEXT,
    branch TEXT,
    assigned_user_id TEXT REFERENCES users(id),
    availability TEXT NOT NULL DEFAULT 'AVAILABLE',
    take_home_approved INTEGER NOT NULL DEFAULT 0,
    soft_inventory_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    location_id TEXT NOT NULL REFERENCES locations(id),
    equipment_id TEXT REFERENCES equipment(id),
    job_number TEXT NOT NULL,
    job_type TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'NORMAL',
    owner_user_id TEXT REFERENCES users(id),
    next_action TEXT,
    due_date TEXT,
    estimated_minutes INTEGER NOT NULL DEFAULT 60,
    crew_min INTEGER NOT NULL DEFAULT 1,
    crew_recommended INTEGER NOT NULL DEFAULT 1,
    simultaneous_crew_minutes INTEGER NOT NULL DEFAULT 0,
    qualification_required TEXT,
    parts_status TEXT NOT NULL DEFAULT 'READY',
    blocked_reason TEXT,
    commitment_type TEXT NOT NULL DEFAULT 'FLEXIBLE_DAY',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(organization_id, job_number)
);
CREATE INDEX IF NOT EXISTS idx_jobs_org_status ON jobs(organization_id, status);

CREATE TABLE IF NOT EXISTS visits (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    technician_user_id TEXT NOT NULL REFERENCES users(id),
    helper_user_id TEXT REFERENCES users(id),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    commitment_type TEXT NOT NULL,
    execution_status TEXT NOT NULL DEFAULT 'PLANNED',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_visits_tech_start ON visits(organization_id, technician_user_id, start_at);

CREATE TABLE IF NOT EXISTS recommendations (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    equipment_id TEXT REFERENCES equipment(id),
    created_by_user_id TEXT NOT NULL REFERENCES users(id),
    summary TEXT NOT NULL,
    urgency TEXT NOT NULL DEFAULT 'RECOMMENDED',
    status TEXT NOT NULL DEFAULT 'NEW',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_submissions (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    technician_user_id TEXT NOT NULL REFERENCES users(id),
    state TEXT NOT NULL,
    complaint TEXT,
    finding TEXT,
    cause TEXT,
    correction TEXT,
    verification TEXT,
    internal_note TEXT,
    customer_report TEXT,
    outcome TEXT,
    measurements_json TEXT NOT NULL DEFAULT '[]',
    parts_json TEXT NOT NULL DEFAULT '[]',
    recommendations_json TEXT NOT NULL DEFAULT '[]',
    signature_name TEXT,
    submitted_at TEXT,
    reviewed_by_user_id TEXT REFERENCES users(id),
    reviewed_at TEXT,
    review_note TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    actor_user_id TEXT REFERENCES users(id),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    summary TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(organization_id, entity_type, entity_id, id DESC);

CREATE TABLE IF NOT EXISTS command_receipts (
    command_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pets (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    name TEXT NOT NULL,
    breed TEXT,
    weight_lbs REAL,
    coat_type TEXT,
    preferred_groomer_user_id TEXT REFERENCES users(id),
    safety_notes TEXT,
    recurring_weeks INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grooming_appointments (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    pet_id TEXT NOT NULL REFERENCES pets(id),
    groomer_user_id TEXT REFERENCES users(id),
    service_name TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    forms_complete INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
'''


EXTENDED_SCHEMA = r'''
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL,
    address1 TEXT, city TEXT, state TEXT, postal_code TEXT,
    latitude REAL, longitude REAL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    location_id TEXT REFERENCES locations(id),
    name TEXT NOT NULL, title TEXT, phone TEXT, email TEXT,
    preferred_channel TEXT NOT NULL DEFAULT 'PHONE',
    can_approve_estimates INTEGER NOT NULL DEFAULT 0,
    can_sign_work INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_customer ON contacts(customer_id, active);

CREATE TABLE IF NOT EXISTS service_catalog (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    profile TEXT NOT NULL,
    name TEXT NOT NULL, category TEXT,
    default_minutes INTEGER NOT NULL DEFAULT 60,
    default_crew INTEGER NOT NULL DEFAULT 1,
    qualification_required TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parts (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    sku TEXT, manufacturer TEXT, manufacturer_part_number TEXT,
    name TEXT NOT NULL, description TEXT, unit TEXT NOT NULL DEFAULT 'each',
    default_cost_cents INTEGER, min_stock REAL, active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parts_org_name ON parts(organization_id, name);

CREATE TABLE IF NOT EXISTS job_parts (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    part_id TEXT REFERENCES parts(id),
    description TEXT NOT NULL, quantity REAL NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'NEEDED',
    source TEXT NOT NULL DEFAULT 'office',
    vendor TEXT, expected_date TEXT, notes TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_parts_job ON job_parts(job_id, status);

CREATE TABLE IF NOT EXISTS vehicle_inventory (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    vehicle_id TEXT NOT NULL REFERENCES vehicles(id),
    part_id TEXT REFERENCES parts(id),
    item_name TEXT NOT NULL, expected_qty REAL, confirmed_qty REAL,
    state TEXT NOT NULL DEFAULT 'EXPECTED',
    last_confirmed_at TEXT, notes TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(vehicle_id, item_name)
);

CREATE TABLE IF NOT EXISTS remote_start_authorizations (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    technician_user_id TEXT NOT NULL REFERENCES users(id),
    vehicle_id TEXT REFERENCES vehicles(id),
    work_date TEXT NOT NULL,
    origin_label TEXT NOT NULL,
    latitude REAL, longitude REAL,
    reason TEXT NOT NULL,
    approved_by_user_id TEXT NOT NULL REFERENCES users(id),
    expected_return_label TEXT,
    status TEXT NOT NULL DEFAULT 'APPROVED',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id, technician_user_id, work_date)
);

CREATE TABLE IF NOT EXISTS followups (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT REFERENCES customers(id),
    job_id TEXT REFERENCES jobs(id),
    equipment_id TEXT REFERENCES equipment(id),
    recommendation_id TEXT REFERENCES recommendations(id),
    owner_user_id TEXT REFERENCES users(id),
    kind TEXT NOT NULL, summary TEXT NOT NULL,
    due_at TEXT, status TEXT NOT NULL DEFAULT 'PENDING',
    outcome TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_followups_org_due ON followups(organization_id, status, due_at);

CREATE TABLE IF NOT EXISTS recurring_plans (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    location_id TEXT REFERENCES locations(id),
    equipment_id TEXT REFERENCES equipment(id),
    service_name TEXT NOT NULL, frequency_months INTEGER NOT NULL,
    lead_days INTEGER NOT NULL DEFAULT 30,
    next_due_date TEXT NOT NULL, preferred_month INTEGER,
    owner_user_id TEXT REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    last_completed_date TEXT, notes TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recurring_due ON recurring_plans(organization_id, status, next_due_date);

CREATE TABLE IF NOT EXISTS estimates (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    job_id TEXT REFERENCES jobs(id),
    estimate_number TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'DRAFT',
    currency TEXT NOT NULL DEFAULT 'USD', subtotal_cents INTEGER NOT NULL DEFAULT 0,
    tax_cents INTEGER NOT NULL DEFAULT 0, total_cents INTEGER NOT NULL DEFAULT 0,
    assumptions TEXT, exclusions TEXT, expires_on TEXT,
    supersedes_id TEXT REFERENCES estimates(id),
    created_by_user_id TEXT REFERENCES users(id),
    sent_at TEXT, decided_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id, estimate_number, revision)
);
CREATE INDEX IF NOT EXISTS idx_estimates_customer ON estimates(customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS estimate_lines (
    id TEXT PRIMARY KEY,
    estimate_id TEXT NOT NULL REFERENCES estimates(id) ON DELETE CASCADE,
    line_type TEXT NOT NULL DEFAULT 'LABOR', description TEXT NOT NULL,
    quantity REAL NOT NULL DEFAULT 1, unit_price_cents INTEGER NOT NULL DEFAULT 0,
    amount_cents INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS authorizations (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id),
    job_id TEXT REFERENCES jobs(id), estimate_id TEXT REFERENCES estimates(id),
    kind TEXT NOT NULL, document_version TEXT NOT NULL DEFAULT '1',
    scope_text TEXT NOT NULL, signer_name TEXT NOT NULL, signer_title TEXT,
    method TEXT NOT NULL DEFAULT 'IN_PERSON', signature_text TEXT,
    authorized_at TEXT NOT NULL, revoked_at TEXT,
    created_by_user_id TEXT REFERENCES users(id), created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS form_templates (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    profile TEXT NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'PUBLISHED',
    schema_json TEXT NOT NULL DEFAULT '[]', wording TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS form_completions (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    template_id TEXT NOT NULL REFERENCES form_templates(id),
    template_version INTEGER NOT NULL,
    customer_id TEXT REFERENCES customers(id), job_id TEXT REFERENCES jobs(id),
    equipment_id TEXT REFERENCES equipment(id), pet_id TEXT REFERENCES pets(id),
    completed_by_name TEXT, values_json TEXT NOT NULL DEFAULT '{}',
    signature_text TEXT, state TEXT NOT NULL DEFAULT 'DRAFT',
    completed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_records (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id),
    entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'DOCUMENT',
    original_name TEXT NOT NULL, stored_name TEXT NOT NULL,
    mime_type TEXT, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
    created_by_user_id TEXT REFERENCES users(id), created_at TEXT NOT NULL,
    UNIQUE(organization_id, entity_type, entity_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_files_entity ON file_records(organization_id, entity_type, entity_id);

CREATE TABLE IF NOT EXISTS import_batches (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    source_type TEXT NOT NULL, filename TEXT, status TEXT NOT NULL DEFAULT 'STAGED',
    row_count INTEGER NOT NULL DEFAULT 0, ready_count INTEGER NOT NULL DEFAULT 0,
    issue_count INTEGER NOT NULL DEFAULT 0, created_by_user_id TEXT REFERENCES users(id),
    created_at TEXT NOT NULL, committed_at TEXT
);
CREATE TABLE IF NOT EXISTS import_rows (
    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL, raw_json TEXT NOT NULL, normalized_json TEXT NOT NULL,
    state TEXT NOT NULL, issue TEXT, matched_customer_id TEXT REFERENCES customers(id),
    created_record_id TEXT
);

CREATE TABLE IF NOT EXISTS tool_usage (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    user_id TEXT REFERENCES users(id), tool_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_usage_org ON tool_usage(organization_id,created_at DESC);

CREATE TABLE IF NOT EXISTS integration_status (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    provider TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'NOT_CONNECTED',
    last_success_at TEXT, last_error TEXT, settings_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, last_test_at TEXT,
    UNIQUE(organization_id, provider)
);

CREATE TABLE IF NOT EXISTS integration_deliveries (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    provider TEXT NOT NULL, communication_id TEXT REFERENCES communications(id),
    state TEXT NOT NULL, external_reference TEXT, error TEXT, sent_at TEXT, delivered_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_integration_deliveries_org ON integration_deliveries(organization_id,created_at DESC);

CREATE TABLE IF NOT EXISTS integration_entity_links (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    provider TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
    external_id TEXT NOT NULL, external_type TEXT, external_json TEXT NOT NULL DEFAULT '{}',
    last_synced_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id,provider,entity_type,entity_id),
    UNIQUE(organization_id,provider,external_type,external_id)
);
CREATE INDEX IF NOT EXISTS idx_integration_entity_links_org ON integration_entity_links(organization_id,provider,entity_type);

CREATE TABLE IF NOT EXISTS mail_intake_messages (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    provider TEXT NOT NULL, external_id TEXT NOT NULL, internet_message_id TEXT,
    sender TEXT, subject TEXT NOT NULL DEFAULT '', received_at TEXT, body_preview TEXT,
    source_kind TEXT NOT NULL DEFAULT 'GENERAL', state TEXT NOT NULL DEFAULT 'RECEIVED',
    import_batch_id TEXT REFERENCES import_batches(id), error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id,provider,external_id)
);
CREATE INDEX IF NOT EXISTS idx_mail_intake_org ON mail_intake_messages(organization_id,received_at DESC);

CREATE TABLE IF NOT EXISTS mail_sync_state (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    provider TEXT NOT NULL, folder TEXT NOT NULL DEFAULT 'Inbox', delta_link TEXT,
    last_synced_at TEXT, last_external_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id,provider,folder)
);

CREATE TABLE IF NOT EXISTS governance_policy (
    organization_id TEXT PRIMARY KEY REFERENCES organizations(id),
    deterministic_first INTEGER NOT NULL DEFAULT 1,
    ai_enabled INTEGER NOT NULL DEFAULT 0,
    ai_is_authority INTEGER NOT NULL DEFAULT 0,
    human_review_for_software_derived INTEGER NOT NULL DEFAULT 1,
    updated_by_user_id TEXT REFERENCES users(id), updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assistance_suggestions (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    kind TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT, field_name TEXT,
    suggested_value_json TEXT NOT NULL, method TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'AWAITING_HUMAN_REVIEW',
    created_by_user_id TEXT REFERENCES users(id), reviewed_by_user_id TEXT REFERENCES users(id),
    reviewed_at TEXT, final_value_json TEXT, review_note TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assistance_suggestions_org
    ON assistance_suggestions(organization_id,status,created_at DESC);

CREATE TABLE IF NOT EXISTS geocode_cache (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    query TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(organization_id,query)
);
CREATE TABLE IF NOT EXISTS route_cache (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    cache_key TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(organization_id,cache_key)
);

CREATE TABLE IF NOT EXISTS portal_links (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    token_hash TEXT NOT NULL UNIQUE, customer_id TEXT NOT NULL REFERENCES customers(id),
    job_id TEXT REFERENCES jobs(id), purpose TEXT NOT NULL, expires_at TEXT NOT NULL,
    consumed_at TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_day_confirmations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id), job_id TEXT NOT NULL REFERENCES jobs(id),
    visit_id TEXT NOT NULL REFERENCES visits(id), portal_link_id TEXT REFERENCES portal_links(id),
    scheduled_date TEXT NOT NULL, commitment_type TEXT NOT NULL,
    disclosure_version INTEGER NOT NULL DEFAULT 1, disclosure_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'AWAITING_CONFIRMATION',
    recipient TEXT, channel TEXT, viewed_at TEXT, confirmed_by_name TEXT, confirmed_at TEXT,
    reply_code TEXT, reply_received_at TEXT, change_request_note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_service_day_confirmations_job ON service_day_confirmations(organization_id,job_id,created_at DESC);

CREATE TABLE IF NOT EXISTS grooming_services (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL, default_minutes INTEGER NOT NULL, base_price_cents INTEGER,
    size_band TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS waitlist_requests (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id), pet_id TEXT NOT NULL REFERENCES pets(id),
    service_name TEXT NOT NULL, preferred_groomer_user_id TEXT REFERENCES users(id),
    earliest_at TEXT, latest_at TEXT, notes TEXT, status TEXT NOT NULL DEFAULT 'WAITING',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rebooking_obligations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id), pet_id TEXT NOT NULL REFERENCES pets(id),
    service_name TEXT, due_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'DUE',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inspection_templates (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    name TEXT NOT NULL, equipment_category TEXT, version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'PUBLISHED', procedure_reference TEXT,
    items_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
    UNIQUE(organization_id,name,version)
);

CREATE TABLE IF NOT EXISTS inspections (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    job_id TEXT REFERENCES jobs(id), equipment_id TEXT NOT NULL REFERENCES equipment(id),
    template_id TEXT NOT NULL REFERENCES inspection_templates(id), template_version INTEGER NOT NULL,
    inspector_user_id TEXT NOT NULL REFERENCES users(id), inspection_date TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'DRAFT', result TEXT,
    summary TEXT, customer_ack_name TEXT, next_due_date TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inspections_equipment ON inspections(organization_id,equipment_id,inspection_date DESC);

CREATE TABLE IF NOT EXISTS inspection_items (
    id TEXT PRIMARY KEY, inspection_id TEXT NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL, label TEXT NOT NULL, result TEXT NOT NULL,
    measurement_value REAL, measurement_unit TEXT, comment TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE(inspection_id,item_key)
);

CREATE TABLE IF NOT EXISTS communications (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id), contact_id TEXT REFERENCES contacts(id),
    job_id TEXT REFERENCES jobs(id), created_by_user_id TEXT REFERENCES users(id),
    channel TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'OUTBOUND',
    subject TEXT, body TEXT, status TEXT NOT NULL,
    outcome TEXT, occurred_at TEXT NOT NULL, created_at TEXT NOT NULL,
    service_day_confirmation_id TEXT REFERENCES service_day_confirmations(id)
);
CREATE INDEX IF NOT EXISTS idx_communications_customer ON communications(organization_id,customer_id,occurred_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    user_id TEXT REFERENCES users(id), kind TEXT NOT NULL, summary TEXT NOT NULL,
    entity_type TEXT, entity_id TEXT, read_at TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storage_objects (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    provider TEXT NOT NULL, local_kind TEXT NOT NULL, local_id TEXT, local_path TEXT,
    remote_id TEXT, remote_path TEXT, sha256 TEXT, state TEXT NOT NULL DEFAULT 'PENDING',
    last_error TEXT, synced_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id,provider,local_kind,local_id)
);

CREATE TABLE IF NOT EXISTS calendar_sync_state (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    provider TEXT NOT NULL, calendar_key TEXT NOT NULL DEFAULT 'default', delta_link TEXT,
    window_start TEXT, window_end TEXT, last_synced_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(organization_id,provider,calendar_key)
);

CREATE TABLE IF NOT EXISTS calendar_conflicts (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    provider TEXT NOT NULL, visit_id TEXT REFERENCES visits(id), external_event_id TEXT,
    kind TEXT NOT NULL, local_json TEXT NOT NULL DEFAULT '{}', external_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW', reviewed_by_user_id TEXT REFERENCES users(id),
    reviewed_at TEXT, resolution TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales_opportunities (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    customer_id TEXT NOT NULL REFERENCES customers(id), location_id TEXT REFERENCES locations(id),
    title TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'DISCOVERY', expected_value_cents INTEGER,
    probability_pct INTEGER NOT NULL DEFAULT 25, owner_user_id TEXT REFERENCES users(id),
    next_action TEXT, target_close_date TEXT, source TEXT, notes TEXT,
    version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_opportunities_org ON sales_opportunities(organization_id,stage,target_close_date);

CREATE TABLE IF NOT EXISTS equipment_orders (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    opportunity_id TEXT REFERENCES sales_opportunities(id), estimate_id TEXT REFERENCES estimates(id),
    customer_id TEXT NOT NULL REFERENCES customers(id), vendor TEXT, order_number TEXT,
    status TEXT NOT NULL DEFAULT 'PLANNED', ordered_at TEXT, expected_at TEXT, received_at TEXT,
    total_cents INTEGER, notes TEXT, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS installation_projects (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    opportunity_id TEXT REFERENCES sales_opportunities(id), equipment_order_id TEXT REFERENCES equipment_orders(id),
    customer_id TEXT NOT NULL REFERENCES customers(id), location_id TEXT NOT NULL REFERENCES locations(id),
    name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PLANNING', planned_start TEXT, planned_finish TEXT,
    actual_start TEXT, actual_finish TEXT, commissioning_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    project_manager_user_id TEXT REFERENCES users(id), notes TEXT, version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commissioning_records (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    project_id TEXT NOT NULL REFERENCES installation_projects(id), equipment_id TEXT REFERENCES equipment(id),
    job_id TEXT REFERENCES jobs(id), result TEXT NOT NULL, checklist_json TEXT NOT NULL DEFAULT '{}',
    customer_ack_name TEXT, performed_by_user_id TEXT NOT NULL REFERENCES users(id), performed_at TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legal_approvals (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    content_type TEXT NOT NULL, content_id TEXT NOT NULL, content_version TEXT NOT NULL,
    wording_hash TEXT NOT NULL, approved_by_user_id TEXT NOT NULL REFERENCES users(id),
    approved_at TEXT NOT NULL, note TEXT, UNIQUE(organization_id,content_type,content_id,content_version)
);

CREATE TABLE IF NOT EXISTS usability_sessions (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    tester_role TEXT NOT NULL, device_label TEXT, started_at TEXT NOT NULL, completed_at TEXT,
    task_results_json TEXT NOT NULL DEFAULT '[]', friction_notes TEXT, created_by_user_id TEXT REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS device_readiness_reports (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    user_id TEXT REFERENCES users(id), device_label TEXT, browser TEXT, capabilities_json TEXT NOT NULL DEFAULT '{}',
    offline_tested INTEGER NOT NULL DEFAULT 0, reconnect_tested INTEGER NOT NULL DEFAULT 0,
    camera_tested INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'NOT_TESTED',
    notes TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS internal_messages (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
    sender_user_id TEXT NOT NULL REFERENCES users(id), recipient_user_id TEXT REFERENCES users(id),
    body TEXT NOT NULL, linked_entity_type TEXT, linked_entity_id TEXT,
    created_at TEXT NOT NULL, read_at TEXT, archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_internal_messages_recipient ON internal_messages(organization_id,recipient_user_id,created_at DESC);

CREATE TABLE IF NOT EXISTS lan_presence (
    organization_id TEXT NOT NULL REFERENCES organizations(id), user_id TEXT NOT NULL REFERENCES users(id),
    device_id TEXT NOT NULL, device_label TEXT, last_seen_at TEXT NOT NULL,
    PRIMARY KEY(organization_id,user_id,device_id)
);
CREATE INDEX IF NOT EXISTS idx_lan_presence_seen ON lan_presence(organization_id,last_seen_at DESC);
'''


def _ensure_column(conn: Any, table: str, name: str, definition: str) -> None:
    if database_backend() == "postgresql":
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=? AND column_name=?",
            (table, name),
        ).fetchone()
        if not row:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        return
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def migrate_schema(conn: sqlite3.Connection) -> None:
    # V0.1 accidentally constrained one submission per technician/job/state.
    # Rebuild once so return visits and multiple approved work records remain possible.
    ws_sql_row = None
    if database_backend() == "sqlite":
        ws_sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='work_submissions'").fetchone()
    if ws_sql_row and "UNIQUE(job_id, technician_user_id, state)" in (ws_sql_row[0] or ""):
        conn.execute("ALTER TABLE work_submissions RENAME TO work_submissions_legacy")
        conn.execute("""CREATE TABLE work_submissions (
            id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
            job_id TEXT NOT NULL REFERENCES jobs(id), technician_user_id TEXT NOT NULL REFERENCES users(id),
            state TEXT NOT NULL, complaint TEXT, finding TEXT, cause TEXT, correction TEXT, verification TEXT,
            internal_note TEXT, customer_report TEXT, outcome TEXT, measurements_json TEXT NOT NULL DEFAULT '[]',
            parts_json TEXT NOT NULL DEFAULT '[]', recommendations_json TEXT NOT NULL DEFAULT '[]', signature_name TEXT, submitted_at TEXT,
            reviewed_by_user_id TEXT REFERENCES users(id), reviewed_at TEXT, review_note TEXT,
            version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        conn.execute("""INSERT INTO work_submissions(id,organization_id,job_id,technician_user_id,state,complaint,finding,cause,correction,verification,
            internal_note,customer_report,outcome,measurements_json,parts_json,recommendations_json,signature_name,submitted_at,reviewed_by_user_id,reviewed_at,review_note,version,created_at,updated_at)
            SELECT id,organization_id,job_id,technician_user_id,state,complaint,finding,cause,correction,verification,internal_note,customer_report,outcome,
            measurements_json,parts_json,'[]',signature_name,submitted_at,reviewed_by_user_id,reviewed_at,review_note,version,created_at,updated_at FROM work_submissions_legacy""")
        conn.execute("DROP TABLE work_submissions_legacy")
    conn.executescript(EXTENDED_SCHEMA)
    conn.execute("""CREATE TABLE IF NOT EXISTS quickbooks_desktop_queue (
        id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
        entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, request_kind TEXT NOT NULL, qbxml TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'QUEUED', external_id TEXT, response_xml TEXT, error TEXT,
        queued_by_user_id TEXT REFERENCES users(id), queued_at TEXT NOT NULL, sent_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL,
        UNIQUE(organization_id,entity_type,entity_id,request_kind,status)
    )""")
    _ensure_column(conn, "organizations", "profile_version", "TEXT NOT NULL DEFAULT '1'")
    _ensure_column(conn, "organizations", "settings_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "organizations", "setup_complete", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "organizations", "production_mode", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "organizations", "public_base_url", "TEXT")
    _ensure_column(conn, "organizations", "backup_retention_days", "INTEGER NOT NULL DEFAULT 30")
    _ensure_column(conn, "users", "failed_login_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "users", "locked_until", "TEXT")
    _ensure_column(conn, "users", "last_login_at", "TEXT")
    _ensure_column(conn, "users", "password_changed_at", "TEXT")
    _ensure_column(conn, "users", "mfa_enabled", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "users", "force_password_change", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "customers", "notes", "TEXT")
    _ensure_column(conn, "locations", "area_label", "TEXT")
    _ensure_column(conn, "locations", "access_notes", "TEXT")
    _ensure_column(conn, "equipment", "inspection_due_date", "TEXT")
    _ensure_column(conn, "equipment", "inspection_interval_months", "INTEGER")
    _ensure_column(conn, "equipment", "parent_equipment_id", "TEXT REFERENCES equipment(id)")
    _ensure_column(conn, "vehicles", "notes", "TEXT")
    _ensure_column(conn, "vehicles", "version", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "visits", "helper_end_at", "TEXT")
    _ensure_column(conn, "visits", "external_calendar_provider", "TEXT")
    _ensure_column(conn, "visits", "external_event_id", "TEXT")
    _ensure_column(conn, "visits", "calendar_sync_state", "TEXT NOT NULL DEFAULT 'LOCAL_ONLY'")
    _ensure_column(conn, "visits", "calendar_last_synced_at", "TEXT")
    _ensure_column(conn, "jobs", "review_due_at", "TEXT")
    _ensure_column(conn, "jobs", "purchase_order", "TEXT")
    _ensure_column(conn, "jobs", "billing_hold_reason", "TEXT")
    _ensure_column(conn, "jobs", "safety_status", "TEXT NOT NULL DEFAULT 'NORMAL'")
    _ensure_column(conn, "jobs", "data_origin", "TEXT NOT NULL DEFAULT 'live'")
    _ensure_column(conn, "jobs", "source_reference", "TEXT")
    _ensure_column(conn, "recommendations", "details", "TEXT")
    _ensure_column(conn, "recommendations", "customer_decision", "TEXT")
    _ensure_column(conn, "recommendations", "followup_due", "TEXT")
    _ensure_column(conn, "work_submissions", "signature_at", "TEXT")
    _ensure_column(conn, "work_submissions", "labor_minutes", "INTEGER")
    _ensure_column(conn, "work_submissions", "safety_restriction", "TEXT")
    _ensure_column(conn, "work_submissions", "recommendations_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "work_submissions", "data_origin", "TEXT NOT NULL DEFAULT 'live'")
    _ensure_column(conn, "work_submissions", "source_system", "TEXT")
    _ensure_column(conn, "work_submissions", "source_reference", "TEXT")
    _ensure_column(conn, "work_submissions", "performed_by_text", "TEXT")
    _ensure_column(conn, "work_submissions", "performed_at", "TEXT")
    _ensure_column(conn, "grooming_appointments", "checkout_note", "TEXT")
    _ensure_column(conn, "grooming_appointments", "completed_at", "TEXT")
    _ensure_column(conn, "integration_status", "enabled", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "integration_status", "last_test_at", "TEXT")
    _ensure_column(conn, "communications", "delivery_provider", "TEXT")
    _ensure_column(conn, "communications", "delivery_reference", "TEXT")
    _ensure_column(conn, "communications", "delivery_error", "TEXT")
    _ensure_column(conn, "communications", "delivery_attempted_at", "TEXT")
    _ensure_column(conn, "communications", "delivered_at", "TEXT")
    _ensure_column(conn, "communications", "service_day_confirmation_id", "TEXT REFERENCES service_day_confirmations(id)")
    _ensure_column(conn, "import_rows", "derivation_method", "TEXT NOT NULL DEFAULT 'DETERMINISTIC_STRUCTURED'")
    _ensure_column(conn, "import_rows", "review_state", "TEXT NOT NULL DEFAULT 'AWAITING_HUMAN_REVIEW'")
    _ensure_column(conn, "import_rows", "reviewed_by_user_id", "TEXT REFERENCES users(id)")
    _ensure_column(conn, "import_rows", "reviewed_at", "TEXT")
    _ensure_column(conn, "service_day_confirmations", "reply_code", "TEXT")
    _ensure_column(conn, "service_day_confirmations", "reply_received_at", "TEXT")
    _ensure_column(conn, "form_templates", "legal_review_required", "INTEGER NOT NULL DEFAULT 0")
    # Normalize legacy JSON truck stock into structured soft inventory once.
    for vehicle in conn.execute("SELECT id,organization_id,soft_inventory_json FROM vehicles WHERE soft_inventory_json IS NOT NULL AND soft_inventory_json NOT IN ('','{}')").fetchall():
        try:
            stock = json.loads(vehicle["soft_inventory_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            stock = {}
        if isinstance(stock, dict):
            for item_name, raw in stock.items():
                state = "EXPECTED"
                expected = None
                confirmed = None
                if isinstance(raw, (int, float)):
                    expected = float(raw)
                elif isinstance(raw, str):
                    state = raw.upper().replace(" " , "_")
                elif isinstance(raw, dict):
                    expected = raw.get("expected_qty") or raw.get("expected")
                    confirmed = raw.get("confirmed_qty") or raw.get("confirmed")
                    state = str(raw.get("state") or state).upper().replace(" ", "_")
                conn.execute("""INSERT OR IGNORE INTO vehicle_inventory
                    (id,organization_id,vehicle_id,item_name,expected_qty,confirmed_qty,state,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (new_id("vinv"), vehicle["organization_id"], vehicle["id"], str(item_name), expected, confirmed, state, utcnow(), utcnow()))
    # Ensure the customer-facing service-day notice exists for existing automotive organizations.
    service_day_wording = "Your appointment is reserved for the scheduled service day unless a specific arrival time or time window has been confirmed separately. Arrival timing may vary as field service work progresses throughout the day. If an unexpected circumstance requires a change to the scheduled date, we’ll contact you promptly with an update and coordinate the next available service day."
    for org_row in conn.execute("SELECT id FROM organizations WHERE profile='automotive_equipment'").fetchall():
        if not conn.execute("SELECT 1 FROM form_templates WHERE organization_id=? AND name='Service Day Confirmation' AND status='PUBLISHED' LIMIT 1", (org_row["id"],)).fetchone():
            conn.execute(
                "INSERT INTO form_templates(id,organization_id,profile,name,category,version,status,schema_json,wording,created_at) VALUES(?,?,?,?,?,1,'PUBLISHED',?,?,?)",
                (new_id("ft"), org_row["id"], "automotive_equipment", "Service Day Confirmation", "SCHEDULING", json.dumps([{"key":"confirmed_date","label":"Scheduled service day","type":"date"}]), service_day_wording, utcnow()),
            )
    for org_row in conn.execute("SELECT id FROM organizations").fetchall():
        conn.execute(
            """INSERT OR IGNORE INTO governance_policy(organization_id,deterministic_first,ai_enabled,ai_is_authority,human_review_for_software_derived,updated_at)
               VALUES(?,1,0,0,1,?)""",
            (org_row["id"], utcnow()),
        )
    connector_ids = ("GOOGLE_DRIVE","MICROSOFT_GRAPH","MOBILE_SMS","SMTP","NTFY","GOTIFY","APPRISE","TWILIO_SMS","O365_OUTBOUND","WEBHOOK","WEBDAV","S3","CALDAV","CARDDAV","NOMINATIM","OSRM","TESSERACT","CLAMAV","OUTLOOK","QUICKBOOKS")
    for org_row in conn.execute("SELECT id FROM organizations").fetchall():
        for provider in connector_ids:
            conn.execute("INSERT OR IGNORE INTO integration_status(id,organization_id,provider,state,updated_at,enabled) VALUES(?,?,?,?,?,1)",
                         (new_id("integration"), org_row["id"], provider, "AVAILABLE" if provider in ("MOBILE_SMS","OUTLOOK") else "NOT_CONNECTED", utcnow()))
        outlook = conn.execute("SELECT settings_json FROM integration_status WHERE organization_id=? AND provider='OUTLOOK'", (org_row["id"],)).fetchone()
        if outlook and (outlook["settings_json"] or "{}").strip() in ("", "{}"):
            conn.execute("UPDATE integration_status SET settings_json=?,updated_at=? WHERE organization_id=? AND provider='OUTLOOK'",
                         (json.dumps({"mode":"AUTO","mailbox":"","fastfield_folder":"Inbox","fastfield_sender_filter":"","fastfield_subject_filter":"FastField"}), utcnow(), org_row["id"]))
    conn.execute("INSERT INTO schema_meta(key,value) VALUES('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CURRENT_SCHEMA_VERSION,))


def seed_profile_defaults(conn: sqlite3.Connection, organization_id: str, profile: str) -> None:
    """Install a small, safe starter configuration for a newly created organization."""
    now = utcnow()
    conn.execute(
        """INSERT OR IGNORE INTO governance_policy(organization_id,deterministic_first,ai_enabled,ai_is_authority,human_review_for_software_derived,updated_at)
           VALUES(?,1,0,0,1,?)""",
        (organization_id, now),
    )
    if profile == "automotive_equipment":
        defaults = [
            ("Service Call", "Service", 90, 1, "GENERAL"),
            ("Annual Lift Inspection", "Inspection", 120, 1, "LIFT"),
            ("Equipment Installation", "Installation", 360, 2, "INSTALL"),
            ("Preventive Maintenance", "Maintenance", 120, 1, "GENERAL"),
        ]
        for name, category, minutes, crew, qual in defaults:
            conn.execute(
                """INSERT INTO service_catalog(id,organization_id,profile,name,category,default_minutes,default_crew,qualification_required,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (new_id("svc"), organization_id, profile, name, category, minutes, crew, qual, now, now),
            )
        forms = [
            ("Customer Work Acknowledgement", "AUTHORIZATION", [{"key":"po","label":"PO / authorization number","type":"text"},{"key":"notes","label":"Customer notes","type":"long_text"}], "I acknowledge the work described above."),
            ("Service Day Confirmation", "SCHEDULING", [{"key":"confirmed_date","label":"Scheduled service day","type":"date"}], "Your appointment is reserved for the scheduled service day unless a specific arrival time or time window has been confirmed separately. Arrival timing may vary as field service work progresses throughout the day. If an unexpected circumstance requires a change to the scheduled date, we’ll contact you promptly with an update and coordinate the next available service day."),
            ("Lift Inspection Field Record", "INSPECTION", [{"key":"result","label":"Inspection result","type":"choice","options":["Pass","Deficiencies noted"]},{"key":"deficiencies","label":"Deficiencies","type":"long_text"}], "Company inspection record. Applicable standards and procedures must be identified by the inspector."),
        ]
        for name, category, schema, wording in forms:
            conn.execute(
                "INSERT INTO form_templates(id,organization_id,profile,name,category,version,status,schema_json,wording,created_at) VALUES(?,?,?,?,?,1,'PUBLISHED',?,?,?)",
                (new_id("ft"), organization_id, profile, name, category, json.dumps(schema), wording, now),
            )
        inspection_items = [
            {"key":"visual_condition","label":"General visual condition","measurement_unit":None},
            {"key":"controls","label":"Controls operate as expected","measurement_unit":None},
            {"key":"safety_devices","label":"Safety devices checked per company procedure","measurement_unit":None},
            {"key":"leaks","label":"Hydraulic / pneumatic leak check","measurement_unit":None},
            {"key":"mounting","label":"Mounting / anchoring condition","measurement_unit":None},
            {"key":"operational_test","label":"Operational test completed","measurement_unit":None},
        ]
        conn.execute(
            """INSERT INTO inspection_templates(id,organization_id,name,equipment_category,version,status,procedure_reference,items_json,created_at)
               VALUES(?,?,?,?,1,'PUBLISHED',?,?,?)""",
            (new_id("it"), organization_id, "Vehicle Lift Annual Inspection", "Vehicle Lift",
             "Use the organization's current approved lift-inspection procedure and applicable manufacturer/industry references; this template does not itself certify legal compliance.",
             json.dumps(inspection_items), now),
        )
    elif profile == "grooming":
        for name, minutes, price in (("Full Groom",120,9500),("Bath & Tidy",75,6500),("De-Shed",150,11000),("Nail Service",30,2500)):
            conn.execute(
                "INSERT INTO grooming_services(id,organization_id,name,default_minutes,base_price_cents,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (new_id("gsvc"), organization_id, name, minutes, price, now, now),
            )
        schema = [{"key":"health","label":"Health or safety changes?","type":"long_text"},{"key":"emergency","label":"Emergency contact","type":"text"}]
        conn.execute(
            "INSERT INTO form_templates(id,organization_id,profile,name,category,version,status,schema_json,wording,created_at) VALUES(?,?,?,?,?,1,'PUBLISHED',?,?,?)",
            (new_id("ft"), organization_id, profile, "Pet Intake & Safety", "INTAKE", json.dumps(schema), "Please disclose information relevant to safe grooming.", now),
        )


def seed_extended_demo(conn: sqlite3.Connection) -> None:
    now = utcnow()
    auto = "org_auto_demo"
    grooming = "org_groom_demo"
    # Do nothing until base demo has been created.
    if not conn.execute("SELECT 1 FROM organizations WHERE id=?", (auto,)).fetchone():
        return
    conn.executemany(
        "INSERT OR IGNORE INTO branches(id,organization_id,name,address1,city,state,postal_code,latitude,longitude,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("br_fresno",auto,"Fresno","1234 Demo Way","Fresno","CA","93722",36.7378,-119.7871,now,now),
            ("br_sac",auto,"Sacramento","850 Demo Commerce Dr","North Highlands","CA","95660",38.6716,-121.3708,now,now),
        ],
    )
    services=[
        ("svc_call",auto,"automotive_equipment","Service Call","Service",90,1,"GENERAL"),
        ("svc_lift_inspect",auto,"automotive_equipment","Annual Lift Inspection","Inspection",120,1,"LIFT"),
        ("svc_install",auto,"automotive_equipment","Equipment Installation","Installation",360,2,"INSTALL"),
        ("svc_pm",auto,"automotive_equipment","Preventive Maintenance","Maintenance",120,1,"GENERAL"),
    ]
    for sid,org,profile,name,cat,minutes,crew,qual in services:
        conn.execute("INSERT OR IGNORE INTO service_catalog(id,organization_id,profile,name,category,default_minutes,default_crew,qualification_required,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(sid,org,profile,name,cat,minutes,crew,qual,now,now))
    contacts=[
        ("ct_future",auto,"c_future","l_future","Rosa Martinez","Service Manager","(559) 555-1001","rosa@example.com",1,1),
        ("ct_clovis",auto,"c_clovis","l_clovis","Derek Hall","Fixed Operations","(559) 555-1002","derek@example.com",1,1),
        ("ct_valley",auto,"c_valley","l_valley","Kim Nguyen","Shop Manager","(559) 555-1003","kim@example.com",0,1),
    ]
    for r in contacts:
        conn.execute("INSERT OR IGNORE INTO contacts(id,organization_id,customer_id,location_id,name,title,phone,email,can_approve_estimates,can_sign_work,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(*r,now,now))
    parts=[
        ("part_hose",auto,"HYD-HOSE-01","Challenger","CL-HOSE-01","Hydraulic hose assembly",18500,2),
        ("part_pad",auto,"LIFT-PAD","Universal",None,"Lift pad",3200,8),
        ("part_cyl",auto,"TCX-CYL","Hunter","RP11-1234","Clamp cylinder",26500,1),
        ("part_contactor",auto,"CTR-30A","Generic",None,"30A contactor",4800,2),
    ]
    for pid,org,sku,mfg,mpn,name,cost,minstock in parts:
        conn.execute("INSERT OR IGNORE INTO parts(id,organization_id,sku,manufacturer,manufacturer_part_number,name,default_cost_cents,min_stock,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(pid,org,sku,mfg,mpn,name,cost,minstock,now,now))
    conn.execute("INSERT OR IGNORE INTO job_parts(id,organization_id,job_id,part_id,description,quantity,status,vendor,expected_date,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",("jp_cyl",auto,"j_parts","part_cyl","Clamp cylinder",1,"ORDERED","Hunter","2026-08-12",now,now))
    vi=[
        ("vi12a",auto,"veh12","part_pad","Lift pads",4,2,"LOW"),
        ("vi12b",auto,"veh12",None,"Hydraulic fittings",12,None,"LIKELY_AVAILABLE"),
        ("vi8a",auto,"veh8","part_pad","Lift pads",4,4,"CONFIRMED"),
        ("vi5a",auto,"veh5",None,"Alignment cables",2,2,"CONFIRMED"),
    ]
    for r in vi:
        conn.execute("INSERT OR IGNORE INTO vehicle_inventory(id,organization_id,vehicle_id,part_id,item_name,expected_qty,confirmed_qty,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(*r,now,now))
    recur=[
        ("rp_future_lift",auto,"c_future","l_future","e_cl10a_1","Annual Lift Inspection",12,45,"2027-02-18","u_coord"),
        ("rp_clovis_align",auto,"c_clovis","l_clovis","e_hunter_1","Preventive Maintenance",6,30,"2026-10-15","u_coord"),
    ]
    for rid,org,cid,lid,eid,svc,freq,lead,due,owner in recur:
        conn.execute("INSERT OR IGNORE INTO recurring_plans(id,organization_id,customer_id,location_id,equipment_id,service_name,frequency_months,lead_days,next_due_date,owner_user_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(rid,org,cid,lid,eid,svc,freq,lead,due,owner,now,now))
    conn.execute("INSERT OR IGNORE INTO followups(id,organization_id,customer_id,job_id,equipment_id,owner_user_id,kind,summary,due_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",("fu_future",auto,"c_future","j_new","e_cl10a_1","u_coord","CUSTOMER_CONTACT","Confirm symptoms and bay availability","2026-08-08T09:00:00-07:00","PENDING",now,now))
    conn.execute("INSERT OR IGNORE INTO recommendations(id,organization_id,job_id,equipment_id,created_by_user_id,summary,urgency,status,details,followup_due,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",("rec_arm",auto,"j_hist2","e_cl10a_1","u_chris","Monitor arm restraint wear","SOON","DEFERRED","Wear noted during inspection; recheck at next service.","2026-11-15",now))
    conn.execute("INSERT OR IGNORE INTO estimates(id,organization_id,customer_id,job_id,estimate_number,revision,status,subtotal_cents,tax_cents,total_cents,assumptions,exclusions,expires_on,created_by_user_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",("est_101",auto,"c_madera","j_two","EST-26101",1,"APPROVED",146500,0,146500,"Existing electrical disconnect is serviceable.","Concrete repair not included.","2026-08-30","u_coord",now,now))
    lines=[
        ("el1","est_101","PART","Crossbeam assembly",1,112500,112500,1),
        ("el2","est_101","LABOR","Two-technician replacement labor",4,8500,34000,2),
    ]
    for row in lines:
        conn.execute("INSERT OR IGNORE INTO estimate_lines(id,estimate_id,line_type,description,quantity,unit_price_cents,amount_cents,sort_order) VALUES(?,?,?,?,?,?,?,?)",row)
    conn.execute("INSERT OR IGNORE INTO authorizations(id,organization_id,customer_id,job_id,estimate_id,kind,scope_text,signer_name,signer_title,method,signature_text,authorized_at,created_by_user_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",("auth_101",auto,"c_madera","j_two","est_101","ESTIMATE_APPROVAL","Approve EST-26101 revision 1 for crossbeam replacement.","Pat Demo","Service Manager","IN_PERSON","Pat Demo","2026-08-05T16:00:00-07:00","u_coord",now))
    form_defs=[
        ("ft_work",auto,"automotive_equipment","Customer Work Acknowledgement","AUTHORIZATION",1,[{"key":"po","label":"PO / authorization number","type":"text"},{"key":"notes","label":"Customer notes","type":"long_text"}],"I acknowledge the work described above."),
        ("ft_lift",auto,"automotive_equipment","Lift Inspection Field Record","INSPECTION",1,[{"key":"result","label":"Inspection result","type":"choice","options":["Pass","Deficiencies noted"]},{"key":"deficiencies","label":"Deficiencies","type":"long_text"}],"Company inspection record. Applicable standards and procedures must be identified by the inspector."),
        ("gft_intake",grooming,"grooming","Pet Intake & Safety","INTAKE",1,[{"key":"health","label":"Health or safety changes?","type":"long_text"},{"key":"emergency","label":"Emergency contact","type":"text"}],"Please disclose information relevant to safe grooming."),
    ]
    for fid,org,profile,name,cat,ver,schema,wording in form_defs:
        conn.execute("INSERT OR IGNORE INTO form_templates(id,organization_id,profile,name,category,version,schema_json,wording,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(fid,org,profile,name,cat,ver,json.dumps(schema),wording,now))
    inspection_items = [
        {"key":"visual_condition","label":"General visual condition","measurement_unit":None},
        {"key":"controls","label":"Controls operate as expected","measurement_unit":None},
        {"key":"safety_devices","label":"Safety devices checked per company procedure","measurement_unit":None},
        {"key":"leaks","label":"Hydraulic / pneumatic leak check","measurement_unit":None},
        {"key":"mounting","label":"Mounting / anchoring condition","measurement_unit":None},
        {"key":"operational_test","label":"Operational test completed","measurement_unit":None},
    ]
    conn.execute("""INSERT OR IGNORE INTO inspection_templates
        (id,organization_id,name,equipment_category,version,status,procedure_reference,items_json,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        ("it_lift_general",auto,"Vehicle Lift Annual Inspection","Vehicle Lift",1,"PUBLISHED",
         "Use the organization's current approved lift-inspection procedure and applicable manufacturer/industry references; this template does not itself certify legal compliance.",
         json.dumps(inspection_items),now))
    gservices=[("gs_full",grooming,"Full Groom",120,9500), ("gs_bath",grooming,"Bath & Tidy",75,6500), ("gs_deshed",grooming,"De-Shed",150,11000)]
    for r in gservices:
        conn.execute("INSERT OR IGNORE INTO grooming_services(id,organization_id,name,default_minutes,base_price_cents,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(*r,now,now))
    conn.execute("INSERT OR IGNORE INTO waitlist_requests(id,organization_id,customer_id,pet_id,service_name,preferred_groomer_user_id,earliest_at,latest_at,notes,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",("wl1",grooming,"gc2","p2","Bath & Tidy","u_gsam","2026-08-08T08:00:00-07:00","2026-08-14T17:00:00-07:00","Any afternoon cancellation works.","WAITING",now,now))
    conn.execute("INSERT OR IGNORE INTO rebooking_obligations(id,organization_id,customer_id,pet_id,service_name,due_date,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",("rb1",grooming,"gc1","p1","Full Groom","2026-09-18","DUE",now,now))
    for provider in ("GOOGLE_DRIVE","MICROSOFT_GRAPH","MOBILE_SMS","SMTP","NTFY","GOTIFY","APPRISE","TWILIO_SMS","O365_OUTBOUND","WEBHOOK","WEBDAV","S3","CALDAV","CARDDAV","NOMINATIM","OSRM","TESSERACT","CLAMAV","OUTLOOK","QUICKBOOKS"):
        conn.execute("INSERT OR IGNORE INTO integration_status(id,organization_id,provider,state,updated_at,enabled) VALUES(?,?,?,?,?,1)",(f"int_{auto}_{provider.lower()}",auto,provider,"AVAILABLE" if provider in ("MOBILE_SMS","OUTLOOK") else "NOT_CONNECTED",now))
    conn.execute("INSERT OR IGNORE INTO notifications(id,organization_id,user_id,kind,summary,entity_type,entity_id,created_at) VALUES(?,?,?,?,?,?,?,?)",("n1",auto,"u_coord","PARTS","Clamp cylinder is still ordered for WO-26104","job","j_parts",now))


def init_db() -> None:
    prior_version = _schema_version_on_disk()
    if prior_version is not None and prior_version != CURRENT_SCHEMA_VERSION:
        create_backup_archive(f"Pre-Migration-v{prior_version}")
    production = os.environ.get("SERVICESLATE_PRODUCTION_MODE", "0") == "1"
    seed_demos = os.environ.get("SERVICESLATE_SEED_DEMO", "0" if production else "1") == "1"
    with connect() as conn:
        conn.executescript(SCHEMA)
        migrate_schema(conn)
        count = conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        if count == 0 and seed_demos:
            seed_demo(conn)
        if seed_demos:
            seed_extended_demo(conn)
    if count or seed_demos:
        ensure_automatic_backup()


def audit(
    conn: sqlite3.Connection,
    organization_id: str,
    actor_user_id: str | None,
    entity_type: str,
    entity_id: str,
    action: str,
    summary: str,
    before: Any | None = None,
    after: Any | None = None,
) -> None:
    conn.execute(
        """INSERT INTO audit_events
        (organization_id, actor_user_id, entity_type, entity_id, action, summary,
         before_json, after_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            organization_id,
            actor_user_id,
            entity_type,
            entity_id,
            action,
            summary,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
            utcnow(),
        ),
    )


def command_once(
    conn: sqlite3.Connection,
    organization_id: str,
    command_id: str,
    fn,
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT result_json FROM command_receipts WHERE command_id=? AND organization_id=?",
        (command_id, organization_id),
    ).fetchone()
    if existing:
        return json.loads(existing["result_json"])
    result = fn()
    conn.execute(
        "INSERT INTO command_receipts(command_id, organization_id, result_json, created_at) VALUES(?,?,?,?)",
        (command_id, organization_id, json.dumps(result), utcnow()),
    )
    return result


def seed_demo(conn: sqlite3.Connection) -> None:
    now = utcnow()
    pwd = _hash_password("ServiceSlateDemo!26")
    auto = "org_auto_demo"
    grooming = "org_groom_demo"
    sandbox = "org_sandbox"
    conn.executemany(
        "INSERT INTO organizations(id,name,profile,is_demo,created_at) VALUES(?,?,?,?,?)",
        [
            (auto, "Summit Automotive Equipment", "automotive_equipment", 1, now),
            (grooming, "Copper & Coat Grooming", "grooming", 1, now),
            (sandbox, "ServiceSlate Sandbox", "automotive_equipment", 1, now),
        ],
    )

    users = [
        ("u_coord", auto, "coordinator@demo.example.com", "Alex Morgan", "COORDINATOR", ["Scheduling"]),
        ("u_manager", auto, "manager@demo.example.com", "Morgan Reed", "MANAGER", ["All"]),
        ("u_chris", auto, "tech.chris@demo.example.com", "Chris Patel", "TECHNICIAN", ["LIFT", "HVAC", "INSTALL"]),
        ("u_jordan", auto, "tech.jordan@demo.example.com", "Jordan Lee", "TECHNICIAN", ["LIFT", "GENERAL"]),
        ("u_taylor", auto, "tech.taylor@demo.example.com", "Taylor Smith", "TECHNICIAN", ["ELECTRICAL", "ALIGNMENT"]),
        ("u_billing", auto, "billing@demo.example.com", "Casey Brooks", "BILLING", []),
        ("u_gadmin", grooming, "groomadmin@demo.example.com", "Riley Chen", "ADMIN", []),
        ("u_reception", grooming, "reception@demo.example.com", "Jamie Torres", "RECEPTION", []),
        ("u_gsam", grooming, "groomer.sam@demo.example.com", "Samantha Lee", "GROOMER", ["SMALL", "MEDIUM", "BREED_CUT"]),
        ("u_gtaylor", grooming, "groomer.taylor@demo.example.com", "Taylor Smith", "GROOMER", ["LARGE", "DESHED"]),
        ("u_sandbox", sandbox, "sandbox@demo.example.com", "Sandbox Admin", "ADMIN", ["All"]),
    ]
    conn.executemany(
        """INSERT INTO users(id,organization_id,email,name,role,password_hash,qualifications_json,created_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        [(a,b,c,d,e,pwd,json.dumps(f),now) for a,b,c,d,e,f in users],
    )

    auto_customers = [
        ("c_future", "Future Ford of Clovis", "(559) 297-6300", "service@example.com", "https://www.futurefordclovis.com", "Automotive Dealership"),
        ("c_clovis", "Clovis Toyota Service", "(559) 555-0102", "service@clovistoyota.example.com", None, "Automotive Dealership"),
        ("c_valley", "Valley Fleet Services", "(559) 555-0103", "ops@valleyfleet.example.com", None, "Fleet Service"),
        ("c_north", "North Fresno Motors", "(559) 555-0104", "shop@northfresno.example.com", None, "Automotive Dealership"),
        ("c_madera", "Madera Auto Center", "(559) 555-0105", "service@maderaauto.example.com", None, "Automotive Repair"),
        ("c_central", "Central Valley Collision", "(559) 555-0106", "manager@cvcollision.example.com", None, "Body Shop"),
    ]
    for cid, name, phone, email, website, btype in auto_customers:
        conn.execute(
            """INSERT INTO customers(id,organization_id,name,phone,email,website,business_type,data_origin,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (cid, auto, name, phone, email, website, btype, "demo", now, now),
        )

    locs = [
        ("l_future", "c_future", "Main Service Department", "920 W Shaw Ave", "Clovis", "CA", "93612", 36.8084, -119.7237, "Check in with service manager."),
        ("l_clovis", "c_clovis", "Main Shop", "895 W Shaw Ave", "Clovis", "CA", "93612", 36.8087, -119.7248, None),
        ("l_valley", "c_valley", "Fleet Yard", "2490 N Minnewawa Ave", "Fresno", "CA", "93727", 36.7738, -119.7070, None),
        ("l_north", "c_north", "Service Shop", "7250 N Palm Ave", "Fresno", "CA", "93711", 36.8390, -119.8070, None),
        ("l_madera", "c_madera", "Service Shop", "1200 S Madera Ave", "Madera", "CA", "93637", 36.9450, -120.0600, None),
        ("l_central", "c_central", "Collision Center", "3100 E Jensen Ave", "Fresno", "CA", "93706", 36.7064, -119.7711, None),
    ]
    for row in locs:
        conn.execute(
            """INSERT INTO locations(id,organization_id,customer_id,name,address1,city,state,postal_code,latitude,longitude,notes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row[0], auto, *row[1:], now, now),
        )

    equip = [
        ("e_cl10a_1", "c_future", "l_future", "Bay 7", "Vehicle Lift", "Challenger", "CL10A", "DEMO-CL10A-004", "2022-03-15", "2027-03-15", "Platform sync history; see prior work."),
        ("e_hunter_1", "c_clovis", "l_clovis", "Alignment Bay", "Alignment System", "Hunter", "HawkEye Elite", "DEMO-HE-008", "2023-06-02", None, None),
        ("e_tc_1", "c_valley", "l_valley", "Tire Bay", "Tire Changer", "Hunter", "TCX57", "DEMO-TCX-010", "2021-09-10", None, None),
        ("e_lift_2", "c_madera", "l_madera", "Bay 2", "Vehicle Lift", "Challenger", "CL12", "DEMO-CL12-220", "2020-11-01", None, None),
    ]
    for eid,cid,lid,bay,cat,mfg,model,serial,installed,warranty,notes in equip:
        conn.execute(
            """INSERT INTO equipment(id,organization_id,customer_id,location_id,bay,category,manufacturer,model,serial_number,install_date,warranty_until,notes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid,auto,cid,lid,bay,cat,mfg,model,serial,installed,warranty,notes,now,now),
        )

    # Historical approved work, useful for field context.
    hist_jobs = [
        ("j_hist1","WO-24091","c_future","l_future","e_cl10a_1","Repair","Left platform lowering faster than right","CLOSED","NORMAL","u_jordan","Closed", "2025-11-05",90,1,1,0,"LIFT","READY","FLEXIBLE_DAY"),
        ("j_hist2","WO-26032","c_future","l_future","e_cl10a_1","Inspection","Annual lift inspection","CLOSED","NORMAL","u_chris","Closed", "2026-02-18",120,1,1,0,"LIFT","READY","DAY_COMMITMENT"),
    ]
    for r in hist_jobs:
        conn.execute(
            """INSERT INTO jobs(id,organization_id,job_number,customer_id,location_id,equipment_id,job_type,description,status,priority,owner_user_id,next_action,due_date,estimated_minutes,crew_min,crew_recommended,simultaneous_crew_minutes,qualification_required,parts_status,commitment_type,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r[0],auto,r[1],r[2],r[3],r[4],r[5],r[6],r[7],r[8],r[9],r[10],r[11],r[12],r[13],r[14],r[15],r[16],r[17],r[18],now,now),
        )
    conn.execute(
        """INSERT INTO work_submissions(id,organization_id,job_id,technician_user_id,state,complaint,finding,cause,correction,verification,customer_report,outcome,measurements_json,parts_json,signature_name,submitted_at,reviewed_by_user_id,reviewed_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("ws_hist1",auto,"j_hist1","u_jordan","LOCKED","Left runway lowered first","Platforms out of sync","Synchronization adjustment required","Set both platforms on same final lock with valves open, then closed valves to synchronize","Cycled unit five times; platforms remained synchronized","Adjusted platform synchronization and verified operation","COMPLETED",json.dumps([{"name":"Platform height difference","before":2.75,"after":0.25,"unit":"in"}]),json.dumps([]),"Demo Customer","2025-11-05T17:00:00+00:00","u_coord","2025-11-05T18:00:00+00:00",now,now),
    )

    jobs = [
        ("j_new","WO-26101","c_future","l_future","e_cl10a_1","Service Call","Lift intermittently out of level","NEW_REQUEST","HIGH",None,"Review request",str(date.today()),120,1,1,0,"LIFT","READY",None,"FLEXIBLE_DAY"),
        ("j_unsched","WO-26102","c_clovis","l_clovis","e_hunter_1","Repair","Alignment camera communication fault","APPROVED_UNSCHEDULED","NORMAL","u_coord","Schedule technician",str(date.today()+timedelta(days=1)),150,1,1,0,"ALIGNMENT","READY",None,"FLEXIBLE_DAY"),
        ("j_two","WO-26103","c_madera","l_madera","e_lift_2","Repair","Replace crossbeam assembly","APPROVED_UNSCHEDULED","HIGH","u_coord","Schedule two-tech crew",str(date.today()+timedelta(days=1)),240,2,2,60,"LIFT","READY",None,"DAY_COMMITMENT"),
        ("j_parts","WO-26104","c_valley","l_valley","e_tc_1","Repair","Replace clamp cylinder","BLOCKED","NORMAL","u_coord","Wait for cylinder",str(date.today()+timedelta(days=2)),120,1,1,0,"GENERAL","ORDERED","Part ordered","FLEXIBLE_DAY"),
        ("j_ready","WO-26105","c_central","l_central",None,"Service Call","Shop air equipment service","WORK_COMPLETE","NORMAL","u_coord","Review technician paperwork",str(date.today()),90,1,1,0,"GENERAL","READY",None,"DAY_COMMITMENT"),
        ("j_sched_future","WO-26106","c_future","l_future","e_cl10a_1","Inspection","Annual lift inspection - second lift","SCHEDULED","NORMAL","u_chris","Perform scheduled work",str(date.today()+timedelta(days=1)),120,1,1,0,"LIFT","READY",None,"DAY_COMMITMENT"),
        ("j_sched_clovis","WO-26107","c_clovis","l_clovis","e_hunter_1","Preventive Maintenance","Alignment rack preventive service","SCHEDULED","NORMAL","u_taylor","Perform scheduled work",str(date.today()+timedelta(days=1)),120,1,1,0,"ALIGNMENT","READY",None,"FLEXIBLE_DAY"),
        ("j_sched_valley","WO-26108","c_valley","l_valley","e_tc_1","Inspection","Tire changer inspection","SCHEDULED","NORMAL","u_jordan","Perform scheduled work",str(date.today()+timedelta(days=1)),90,1,1,0,"GENERAL","READY",None,"DAY_COMMITMENT"),
    ]
    for r in jobs:
        conn.execute(
            """INSERT INTO jobs(id,organization_id,job_number,customer_id,location_id,equipment_id,job_type,description,status,priority,owner_user_id,next_action,due_date,estimated_minutes,crew_min,crew_recommended,simultaneous_crew_minutes,qualification_required,parts_status,blocked_reason,commitment_type,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r[0],auto,r[1],r[2],r[3],r[4],r[5],r[6],r[7],r[8],r[9],r[10],r[11],r[12],r[13],r[14],r[15],r[16],r[17],r[18],r[19],now,now),
        )

    tomorrow = date.today() + timedelta(days=1)
    def dt(h,m=0): return f"{tomorrow.isoformat()}T{h:02d}:{m:02d}:00-07:00"
    sched = [
        ("v1","j_sched_future","u_chris",dt(8),dt(10),"DAY_COMMITMENT"),
        ("v2","j_sched_clovis","u_taylor",dt(8),dt(10),"FLEXIBLE_DAY"),
        ("v3","j_sched_valley","u_jordan",dt(8),dt(9,30),"DAY_COMMITMENT"),
    ]
    for vid,jid,uid,s,e,commit in sched:
        conn.execute(
            "INSERT INTO visits(id,organization_id,job_id,technician_user_id,start_at,end_at,commitment_type,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (vid,auto,jid,uid,s,e,commit,now,now),
        )

    # Existing clean submission waiting coordinator review.
    conn.execute(
        """INSERT INTO work_submissions(id,organization_id,job_id,technician_user_id,state,complaint,finding,cause,correction,verification,internal_note,customer_report,outcome,measurements_json,parts_json,signature_name,submitted_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("ws_ready",auto,"j_ready","u_jordan","SUBMITTED","Customer reported pressure drop","Found loose fitting at regulator","Loose compression fitting","Re-seated and tightened fitting","Pressure held after 15 minute test","No further issues observed","Repaired shop air connection and verified operation","COMPLETED",json.dumps([]),json.dumps([{"part":"Compression fitting","qty":1}]),"Demo Customer",utcnow(),now,now),
    )

    conn.executemany(
        "INSERT INTO vehicles(id,organization_id,unit_number,description,branch,assigned_user_id,availability,take_home_approved,soft_inventory_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("veh12",auto,"12","Service truck","Fresno","u_chris","AVAILABLE",0,json.dumps({"Lift pads":"LIKELY_AVAILABLE","Hydraulic fittings":"LOW"}),now,now),
            ("veh8",auto,"8","Service truck","Fresno","u_jordan","AVAILABLE",0,json.dumps({"Lift pads":"CONFIRMED","Hydraulic fittings":"LIKELY_AVAILABLE"}),now,now),
            ("veh5",auto,"5","Electrical/alignment truck","Fresno","u_taylor","AVAILABLE",0,json.dumps({"Alignment cables":"CONFIRMED"}),now,now),
        ],
    )

    # Grooming seed.
    g_customers = [
        ("gc1","Jessica Carter","(559) 555-0201","jessica@example.com"),
        ("gc2","Michael Tran","(559) 555-0202","michael@example.com"),
        ("gc3","Amanda White","(559) 555-0203","amanda@example.com"),
    ]
    for cid,name,phone,email in g_customers:
        conn.execute(
            "INSERT INTO customers(id,organization_id,name,phone,email,business_type,data_origin,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (cid,grooming,name,phone,email,"Pet Owner","demo",now,now),
        )
    pets = [
        ("p1","gc1","Luna","Golden Retriever",58,"Double coat","u_gsam",None,6),
        ("p2","gc2","Buddy","Shih Tzu",14,"Silky","u_gsam","Nervous around dryers",8),
        ("p3","gc3","Milo","German Shepherd",82,"Double coat","u_gtaylor",None,6),
    ]
    for p in pets:
        conn.execute(
            "INSERT INTO pets(id,organization_id,customer_id,name,breed,weight_lbs,coat_type,preferred_groomer_user_id,safety_notes,recurring_weeks,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (p[0],grooming,*p[1:],now,now),
        )
    gt = date.today()+timedelta(days=1)
    gapps = [
        ("ga1","gc1","p1","u_gsam","Full Groom",f"{gt}T09:00:00-07:00",f"{gt}T11:00:00-07:00","CONFIRMED",1),
        ("ga2","gc2","p2","u_gsam","Bath & Tidy",f"{gt}T11:30:00-07:00",f"{gt}T12:30:00-07:00","AWAITING_FORMS",0),
        ("ga3","gc3","p3","u_gtaylor","De-Shed",f"{gt}T13:00:00-07:00",f"{gt}T15:30:00-07:00","CONFIRMED",1),
    ]
    for a in gapps:
        conn.execute(
            "INSERT INTO grooming_appointments(id,organization_id,customer_id,pet_id,groomer_user_id,service_name,start_at,end_at,status,forms_complete,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (a[0],grooming,*a[1:],now,now),
        )

    # Seed initial audit traces for history.
    audit(conn, auto, "u_jordan", "job", "j_hist1", "WORK_COMPLETED", "Adjusted lift synchronization and verified five cycles.")
    audit(conn, auto, "u_coord", "job", "j_hist1", "APPROVED", "Coordinator approved technician paperwork.")
    audit(conn, auto, "u_chris", "job", "j_hist2", "INSPECTION_COMPLETED", "Annual lift inspection completed.")
