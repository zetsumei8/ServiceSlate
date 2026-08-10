from __future__ import annotations

import base64
import ipaddress
import hashlib
import hmac
import json
import mimetypes
import os
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from pydantic import BaseModel

from .db import DATA_DIR, FILES_DIR, audit, connect, new_id, utcnow
from .mail_intake import MailAttachment, MailEnvelope, ingest_mail_message, parse_eml, process_service_day_reply
from . import outlook_bridge

router = APIRouter(prefix="/api/integrations")


# Provider descriptions are product metadata. External systems remain optional adapters.
PROVIDERS: dict[str, dict[str, Any]] = {
    "GOOGLE_DRIVE": {
        "name": "Google Drive",
        "category": "Company Storage",
        "license": "Uses your existing Google Workspace / Drive",
        "open_source": False,
        "mode": "company_storage",
        "description": "Preferred company storage for documents and off-site backups. Use an existing Google Drive for Desktop folder with almost no setup, or direct Shared Drive API access when the company authorizes it. The ServiceSlate database remains the source of truth.",
        "fields": ["mode", "local_folder", "file_subfolder", "backup_subfolder", "client_id", "shared_drive_id", "root_folder_id", "backup_folder_id", "redirect_base_url", "sync_files", "sync_backups"],
        "secret_fields": ["client_secret", "refresh_token"],
    },
    "MICROSOFT_GRAPH": {
        "name": "Microsoft 365 / New Outlook",
        "category": "Email & Calendar",
        "license": "Uses your existing Microsoft 365 account",
        "open_source": False,
        "mode": "primary_email_calendar",
        "description": "Supported mailbox and calendar connection for new Outlook and hosted Microsoft 365. Uses Microsoft Graph with least-privilege delegated access where possible.",
        "fields": ["tenant_id", "client_id", "redirect_base_url", "mailbox", "mail_folder", "calendar_id", "sync_mail", "sync_calendar"],
        "secret_fields": ["client_secret", "token_cache"],
    },
    "QUICKBOOKS": {
        "name": "QuickBooks",
        "category": "Accounting",
        "license": "Uses your existing QuickBooks product",
        "open_source": False,
        "mode": "accounting_adapter",
        "description": "Optional accounting handoff. QuickBooks Online can use OAuth; QuickBooks Desktop can use Intuit Web Connector when the hosted QuickBooks administrator allows it. Export-only remains available with no admin access.",
        "fields": ["mode", "environment", "client_id", "realm_id", "redirect_base_url", "service_item_id", "web_connector_url", "web_connector_username", "desktop_service_item_name"],
        "secret_fields": ["client_secret", "refresh_token", "web_connector_password"],
    },
    "CLAMAV": {
        "name": "ClamAV Upload Scan",
        "category": "Security",
        "license": "Open source",
        "open_source": True,
        "mode": "security",
        "description": "Optional local malware scan for uploaded files before public/hosted use.",
        "fields": ["executable"],
        "secret_fields": [],
    },
    "MOBILE_SMS": {
        "name": "Mobile carrier SMS",
        "category": "Messaging",
        "license": "Uses your phone/carrier",
        "open_source": False,
        "mode": "handoff",
        "description": "Use the paired phone's normal SMS service from a PC or phone. Human-approved; ServiceSlate never pretends a handoff was delivered.",
        "fields": [],
        "links": [
            {"label": "Windows messaging app", "url": "ms-chat:"},
            {"label": "Google Messages for web", "url": "https://messages.google.com/web/"},
            {"label": "KDE Connect", "url": "https://kdeconnect.kde.org/"},
        ],
    },
    "OUTLOOK": {
        "name": "Outlook Email",
        "category": "Email & Form Intake",
        "license": "Uses your existing Microsoft/Outlook account",
        "open_source": False,
        "mode": "primary_email",
        "description": "Default email path. On Windows with Classic Outlook, ServiceSlate can send through the signed-in Outlook profile and check a designated folder for FastField reports and customer confirmation replies without storing a mailbox password. Microsoft Graph remains the supported path for new Outlook/hosted deployment.",
        "fields": ["mode", "mailbox", "fastfield_folder", "fastfield_sender_filter", "fastfield_subject_filter", "request_read_receipt", "auto_check"],
        "advanced_fields": ["tenant_id", "client_id"],
        "secret_fields": ["token_cache"],
    },
    "SMTP": {
        "name": "SMTP Email",
        "category": "Messaging",
        "license": "Open protocol",
        "open_source": True,
        "mode": "transport",
        "description": "Send email through an existing mailbox or mail server using the standard SMTP protocol.",
        "fields": ["host", "port", "username", "from_email", "from_name", "security"],
        "secret_fields": ["password"],
    },
    "NTFY": {
        "name": "ntfy",
        "category": "Notifications",
        "license": "Open source / self-hostable",
        "open_source": True,
        "mode": "transport",
        "description": "Simple HTTP push notifications. Works with ntfy.sh or a self-hosted ntfy server.",
        "fields": ["base_url", "topic"],
        "secret_fields": ["token"],
    },
    "GOTIFY": {
        "name": "Gotify",
        "category": "Notifications",
        "license": "Open source / self-hostable",
        "open_source": True,
        "mode": "transport",
        "description": "Self-hosted push notification server using its REST API.",
        "fields": ["base_url"],
        "secret_fields": ["app_token"],
    },
    "APPRISE": {
        "name": "Apprise bridge",
        "category": "Messaging",
        "license": "Open source",
        "open_source": True,
        "mode": "bridge",
        "description": "One notification bridge for many open and paid services, including ntfy, Gotify, Twilio, Office 365, AWS SNS/SES, Discord, Telegram and more.",
        "fields": ["tag"],
        "secret_fields": ["urls"],
    },
    "TWILIO_SMS": {
        "name": "Twilio SMS",
        "category": "Messaging",
        "license": "Paid service adapter",
        "open_source": False,
        "mode": "paid_adapter",
        "description": "Optional automated SMS transport with delivery-status callbacks. Carrier-phone handoff remains available without a paid provider.",
        "fields": ["from_number"],
        "secret_fields": ["account_sid", "auth_token"],
    },
    "O365_OUTBOUND": {
        "name": "Microsoft 365 via Apprise",
        "category": "Messaging",
        "license": "Paid/existing service adapter",
        "open_source": False,
        "mode": "paid_adapter",
        "description": "Optional alternate Microsoft 365 notification transport through Apprise. Normal ServiceSlate email uses the Outlook connection above.",
        "fields": ["account_email", "tenant_id", "client_id"],
        "secret_fields": ["client_secret"],
    },
    "WEBHOOK": {
        "name": "Generic Webhook",
        "category": "Automation",
        "license": "Open protocol",
        "open_source": True,
        "mode": "transport",
        "description": "POST structured JSON to an organization-controlled automation endpoint.",
        "fields": ["url"],
        "secret_fields": ["bearer_token"],
    },
    "WEBDAV": {
        "name": "WebDAV File Storage",
        "category": "Storage",
        "license": "Open protocol",
        "open_source": True,
        "mode": "storage",
        "description": "Upload files/backups to a WebDAV server such as Nextcloud without locking ServiceSlate to one cloud vendor.",
        "fields": ["base_url", "username", "folder"],
        "secret_fields": ["password"],
    },
    "S3": {
        "name": "S3-Compatible Storage",
        "category": "Storage",
        "license": "Open-compatible protocol",
        "open_source": True,
        "mode": "storage",
        "description": "Object storage adapter for MinIO, AWS S3 and other S3-compatible services.",
        "fields": ["endpoint_url", "bucket", "region", "access_key_id", "prefix", "sync_files", "sync_backups", "mirror_files", "mirror_backups"],
        "secret_fields": ["secret_access_key"],
    },
    "CALDAV": {
        "name": "CalDAV Calendar",
        "category": "Calendar & Contacts",
        "license": "Open protocol",
        "open_source": True,
        "mode": "calendar",
        "description": "Push standard calendar events to an existing CalDAV collection, including Radicale and Nextcloud.",
        "fields": ["collection_url", "username"],
        "secret_fields": ["password"],
    },
    "CARDDAV": {
        "name": "CardDAV Contacts",
        "category": "Calendar & Contacts",
        "license": "Open protocol",
        "open_source": True,
        "mode": "contacts",
        "description": "Push standard contact cards to an existing CardDAV collection, including Radicale and Nextcloud.",
        "fields": ["collection_url", "username"],
        "secret_fields": ["password"],
    },
    "NOMINATIM": {
        "name": "Nominatim Geocoding",
        "category": "Location Planning",
        "license": "Open source",
        "open_source": True,
        "mode": "geocode",
        "description": "Manual address lookup through a self-hosted or approved Nominatim endpoint. Results are cached; no GPS tracking.",
        "fields": ["base_url", "user_agent", "allow_public_osm"],
    },
    "OSRM": {
        "name": "OSRM Travel Estimates",
        "category": "Location Planning",
        "license": "Open source / self-hostable",
        "open_source": True,
        "mode": "routing",
        "description": "Point-to-point travel-time estimates for scheduling. No live vehicle tracking, route replay or driver scoring.",
        "fields": ["base_url"],
    },
    "TESSERACT": {
        "name": "Tesseract OCR",
        "category": "Field Tools",
        "license": "Open source",
        "open_source": True,
        "mode": "ocr",
        "description": "Local OCR for equipment nameplates and documents. Extracted values are suggestions and always require human review.",
        "fields": ["executable", "language"],
    },
}


@dataclass
class SecretVault:
    """Small encrypted local connector vault.

    The key is local to this installation and intentionally excluded from normal exports.
    This protects secrets from accidental disclosure in the SQLite database/export; it is
    not a substitute for an enterprise OS/HSM secret store on a production server.
    """

    key_path: Path = DATA_DIR / ".integration-key"
    vault_path: Path = DATA_DIR / ".integration-secrets"

    def _fernet(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - installation gate
            raise RuntimeError("Connector secret storage requires the cryptography package") from exc
        env_key = os.environ.get("SERVICESLATE_MASTER_KEY", "").strip()
        if env_key:
            try:
                return Fernet(env_key.encode("ascii"))
            except Exception as exc:
                raise RuntimeError("SERVICESLATE_MASTER_KEY must be a valid Fernet key") from exc
        if os.environ.get("SERVICESLATE_PRODUCTION_MODE", "0") == "1" and os.environ.get("SERVICESLATE_ALLOW_LOCAL_SECRET_KEY", "0") != "1":
            raise RuntimeError("Production ServiceSlate requires SERVICESLATE_MASTER_KEY or an explicitly approved local-key exception")
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.key_path.exists():
            key = Fernet.generate_key()
            self.key_path.write_bytes(key)
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
        return Fernet(self.key_path.read_bytes())

    def _load(self) -> dict[str, Any]:
        if not self.vault_path.exists():
            return {}
        try:
            raw = self._fernet().decrypt(self.vault_path.read_bytes())
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _save(self, values: dict[str, Any]) -> None:
        token = self._fernet().encrypt(json.dumps(values, sort_keys=True).encode("utf-8"))
        self.vault_path.write_bytes(token)
        try:
            os.chmod(self.vault_path, 0o600)
        except OSError:
            pass

    def set_provider(self, organization_id: str, provider: str, values: dict[str, str]) -> None:
        data = self._load()
        key = f"{organization_id}:{provider}"
        current = data.get(key, {})
        for name, value in values.items():
            if value:
                current[name] = value
        data[key] = current
        self._save(data)

    def get_provider(self, organization_id: str, provider: str) -> dict[str, str]:
        return dict(self._load().get(f"{organization_id}:{provider}", {}))

    def clear_provider(self, organization_id: str, provider: str) -> None:
        data = self._load(); data.pop(f"{organization_id}:{provider}", None); self._save(data)


VAULT = SecretVault()


def _user(request: Request) -> dict[str, Any]:
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
        raise HTTPException(401, "Sign in required")
    return dict(row)


def _admin(user: dict[str, Any]) -> None:
    if user["role"] not in ("ADMIN", "MANAGER"):
        raise HTTPException(403, "Only an administrator or manager can change connections")


def _provider_row(org: str, provider: str) -> tuple[dict[str, Any], dict[str, str]]:
    if provider not in PROVIDERS:
        raise HTTPException(404, "Connection type not found")
    with connect() as conn:
        row = conn.execute("SELECT * FROM integration_status WHERE organization_id=? AND provider=?", (org, provider)).fetchone()
    settings = json.loads(row["settings_json"] or "{}") if row else {}
    secrets = VAULT.get_provider(org, provider)
    return settings, secrets


def _http_request(url: str, *, method: str = "GET", data: bytes | None = None,
                  headers: dict[str, str] | None = None, timeout: float = 12) -> tuple[int, bytes, dict[str, str]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Outbound URL must be an absolute HTTP(S) address without embedded credentials")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise RuntimeError("Outbound URL host could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise RuntimeError("Outbound URL must resolve only to public internet addresses")
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(_NoRedirect())
        opener.addheaders = []
        with opener.open(req, timeout=timeout) as res:  # noqa: S310 - validated configured integration URL
            return res.status, res.read(), dict(res.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:1000]
        raise RuntimeError(f"HTTP {exc.code}: {body.decode('utf-8', errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection failed: {exc.reason}") from exc


def _auth_header(username: str | None, password: str | None) -> dict[str, str]:
    if not username:
        return {}
    token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _smtp(settings: dict[str, Any], secrets: dict[str, str], *, to: str, subject: str, body: str) -> str:
    host = str(settings.get("host") or "").strip()
    if not host:
        raise RuntimeError("SMTP host is not configured")
    port = int(settings.get("port") or (465 if settings.get("security") == "SSL" else 587))
    security = str(settings.get("security") or "STARTTLS").upper()
    username = str(settings.get("username") or "")
    msg = EmailMessage()
    msg["From"] = f"{settings.get('from_name')} <{settings.get('from_email')}>" if settings.get("from_name") else str(settings.get("from_email") or username)
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    context = ssl.create_default_context()
    if security == "SSL":
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
    try:
        if security == "STARTTLS":
            server.starttls(context=context)
        if username:
            server.login(username, secrets.get("password", ""))
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return str(msg.get("Message-ID") or "sent")


def _twilio_send(settings: dict[str, Any], secrets: dict[str, str], *, to: str, body: str, status_callback: str | None = None) -> str:
    sid = secrets.get("account_sid", "").strip(); token = secrets.get("auth_token", "").strip(); from_no = str(settings.get("from_number") or "").strip()
    if not all((sid, token, from_no, to)):
        raise RuntimeError("Twilio account, from number and recipient are required")
    values = {"To": to, "From": from_no, "Body": body}
    if status_callback: values["StatusCallback"] = status_callback
    data = urllib.parse.urlencode(values).encode()
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    status, raw, _ = _http_request(f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json", method="POST", data=data, headers={"Authorization":f"Basic {auth}","Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"}, timeout=20)
    payload = json.loads(raw or b"{}")
    if status >= 300 or not payload.get("sid"):
        raise RuntimeError(payload.get("message") or f"Twilio returned HTTP {status}")
    return str(payload["sid"])


def _twilio_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    raw = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), raw.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _apprise_url(provider: str, settings: dict[str, Any], secrets: dict[str, str], target: str | None = None) -> list[str]:
    if provider == "APPRISE":
        raw = secrets.get("urls", "")
        return [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]
    if provider == "TWILIO_SMS":
        sid = urllib.parse.quote(secrets.get("account_sid", ""), safe="")
        token = urllib.parse.quote(secrets.get("auth_token", ""), safe="")
        from_no = str(settings.get("from_number") or "").replace("+", "")
        to_no = (target or "").replace("+", "")
        if not all((sid, token, from_no, to_no)):
            raise RuntimeError("Twilio account, from number and recipient are required")
        return [f"twilio://{sid}:{token}@{from_no}/{to_no}"]
    if provider == "O365_OUTBOUND":
        tenant = urllib.parse.quote(str(settings.get("tenant_id") or ""), safe="")
        email = urllib.parse.quote(str(settings.get("account_email") or ""), safe="@")
        client = urllib.parse.quote(str(settings.get("client_id") or ""), safe="")
        secret = urllib.parse.quote(secrets.get("client_secret", ""), safe="")
        recipient = urllib.parse.quote(target or "", safe="@")
        if not all((tenant, email, client, secret)):
            raise RuntimeError("Microsoft 365 tenant, account, client ID and secret are required")
        base = f"o365://{tenant}:{email}/{client}/{secret}"
        return [base + (f"/{recipient}" if recipient else "")]
    return []


def _apprise_send(provider: str, settings: dict[str, Any], secrets: dict[str, str], *, title: str, body: str, target: str | None = None) -> str:
    try:
        import apprise
    except ImportError as exc:
        raise RuntimeError("Apprise is not installed in this build") from exc
    urls = _apprise_url(provider, settings, secrets, target)
    if not urls:
        raise RuntimeError("No notification destination is configured")
    obj = apprise.Apprise()
    for url in urls:
        if not obj.add(url):
            raise RuntimeError("One of the notification destinations is not valid")
    ok = obj.notify(title=title, body=body, tag=settings.get("tag") or None)
    if not ok:
        raise RuntimeError("Notification provider did not accept the message")
    return "accepted"


def _ntfy(settings: dict[str, Any], secrets: dict[str, str], *, title: str, body: str) -> str:
    base = str(settings.get("base_url") or "https://ntfy.sh").rstrip("/")
    topic = str(settings.get("topic") or "").strip("/")
    if not topic:
        raise RuntimeError("ntfy topic is required")
    headers = {"Title": title, "User-Agent": "ServiceSlate/0.9"}
    if secrets.get("token"):
        headers["Authorization"] = f"Bearer {secrets['token']}"
    status, raw, _ = _http_request(f"{base}/{urllib.parse.quote(topic)}", method="POST", data=body.encode("utf-8"), headers=headers)
    if status >= 300:
        raise RuntimeError(f"ntfy returned HTTP {status}")
    try:
        return str(json.loads(raw or b"{}").get("id") or "accepted")
    except json.JSONDecodeError:
        return "accepted"


def _gotify(settings: dict[str, Any], secrets: dict[str, str], *, title: str, body: str) -> str:
    base = str(settings.get("base_url") or "").rstrip("/")
    token = secrets.get("app_token", "")
    if not base or not token:
        raise RuntimeError("Gotify URL and application token are required")
    payload = json.dumps({"title": title, "message": body, "priority": 5}).encode()
    status, raw, _ = _http_request(f"{base}/message", method="POST", data=payload,
                                   headers={"Content-Type": "application/json", "X-Gotify-Key": token})
    if status >= 300:
        raise RuntimeError(f"Gotify returned HTTP {status}")
    try:
        return str(json.loads(raw).get("id") or "accepted")
    except json.JSONDecodeError:
        return "accepted"


def _webhook(settings: dict[str, Any], secrets: dict[str, str], payload: dict[str, Any]) -> str:
    url = str(settings.get("url") or "")
    if not url:
        raise RuntimeError("Webhook URL is required")
    headers = {"Content-Type": "application/json", "User-Agent": "ServiceSlate/0.9"}
    if secrets.get("bearer_token"):
        headers["Authorization"] = f"Bearer {secrets['bearer_token']}"
    status, raw, response_headers = _http_request(url, method="POST", data=json.dumps(payload).encode(), headers=headers)
    if status >= 300:
        raise RuntimeError(f"Webhook returned HTTP {status}")
    return response_headers.get("X-Request-Id") or "accepted"


def _dav_put(collection_url: str, username: str | None, password: str | None, filename: str, content: bytes, content_type: str) -> str:
    url = collection_url.rstrip("/") + "/" + urllib.parse.quote(filename)
    headers = {"Content-Type": content_type, "User-Agent": "ServiceSlate/0.9", **_auth_header(username, password)}
    status, _, response_headers = _http_request(url, method="PUT", data=content, headers=headers)
    if status >= 300:
        raise RuntimeError(f"DAV server returned HTTP {status}")
    return response_headers.get("ETag") or filename


def _s3_test(settings: dict[str, Any], secrets: dict[str, str]) -> str:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("S3 support requires boto3") from exc
    client = boto3.client(
        "s3", endpoint_url=settings.get("endpoint_url") or None,
        region_name=settings.get("region") or None,
        aws_access_key_id=settings.get("access_key_id") or None,
        aws_secret_access_key=secrets.get("secret_access_key") or None,
    )
    bucket = str(settings.get("bucket") or "")
    if not bucket:
        raise RuntimeError("Bucket is required")
    client.head_bucket(Bucket=bucket)
    return bucket


def _status_update(org: str, provider: str, state: str, *, error: str | None = None, success: bool = False) -> None:
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """INSERT INTO integration_status(id,organization_id,provider,state,last_success_at,last_error,settings_json,updated_at,last_test_at)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(organization_id,provider) DO UPDATE SET state=excluded.state,last_error=excluded.last_error,
               last_success_at=CASE WHEN ? THEN excluded.last_success_at ELSE integration_status.last_success_at END,
               updated_at=excluded.updated_at,last_test_at=excluded.last_test_at""",
            (new_id("integration"), org, provider, state, now if success else None, error, "{}", now, now, 1 if success else 0),
        )


class ConfigureIntegration(BaseModel):
    enabled: bool = True
    settings: dict[str, Any] = {}
    secrets: dict[str, str] = {}


class TestMessage(BaseModel):
    target: str | None = None
    title: str = "ServiceSlate connection test"
    body: str = "Your ServiceSlate connection is working."


@router.get("/catalog")
def catalog(request: Request):
    u = _user(request)
    with connect() as conn:
        rows = {r["provider"]: dict(r) for r in conn.execute("SELECT * FROM integration_status WHERE organization_id=?", (u["organization_id"],)).fetchall()}
    result = []
    for pid, meta in PROVIDERS.items():
        row = rows.get(pid, {})
        settings = json.loads(row.get("settings_json") or "{}") if row else {}
        secrets = VAULT.get_provider(u["organization_id"], pid)
        result.append({
            "id": pid, **meta,
            "state": row.get("state") or ("AVAILABLE" if meta["mode"] == "handoff" else "NOT_CONNECTED"),
            "enabled": bool(row.get("enabled", 1)),
            "configured": bool(settings or secrets) or meta["mode"] == "handoff",
            "settings": settings,
            "secret_fields_set": sorted(k for k, v in secrets.items() if v),
            "last_success_at": row.get("last_success_at"),
            "last_error": row.get("last_error"),
            "last_test_at": row.get("last_test_at"),
        })
    return result


@router.put("/{provider}")
def configure(provider: str, p: ConfigureIntegration, request: Request):
    u = _user(request); _admin(u); provider = provider.upper()
    meta = PROVIDERS.get(provider)
    if not meta:
        raise HTTPException(404, "Connection type not found")
    allowed = set(meta.get("fields", [])) | set(meta.get("advanced_fields", []))
    settings = {k: v for k, v in p.settings.items() if k in allowed}
    secret_allowed = set(meta.get("secret_fields", []))
    secrets = {k: v for k, v in p.secrets.items() if k in secret_allowed and v}
    if secrets:
        VAULT.set_provider(u["organization_id"], provider, secrets)
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """INSERT INTO integration_status(id,organization_id,provider,state,settings_json,updated_at,enabled)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(organization_id,provider) DO UPDATE SET
               state=excluded.state,settings_json=excluded.settings_json,updated_at=excluded.updated_at,enabled=excluded.enabled,last_error=NULL""",
            (new_id("integration"),u["organization_id"],provider,"CONFIGURED" if p.enabled else "DISABLED",json.dumps(settings),now,1 if p.enabled else 0),
        )
        audit(conn,u["organization_id"],u["id"],"integration",provider,"CONFIGURED",f"Updated {meta['name']} connection settings")
    return {"ok": True, "provider": provider, "state": "CONFIGURED" if p.enabled else "DISABLED"}


@router.delete("/{provider}")
def disconnect(provider: str, request: Request):
    u = _user(request); _admin(u); provider = provider.upper()
    if provider not in PROVIDERS:
        raise HTTPException(404, "Connection type not found")
    VAULT.clear_provider(u["organization_id"], provider)
    with connect() as conn:
        conn.execute("DELETE FROM integration_status WHERE organization_id=? AND provider=?", (u["organization_id"], provider))
        audit(conn,u["organization_id"],u["id"],"integration",provider,"DISCONNECTED",f"Disconnected {PROVIDERS[provider]['name']}")
    return {"ok": True}


def run_test(provider: str, settings: dict[str, Any], secrets: dict[str, str], p: TestMessage, org: str | None = None) -> str:
    if provider == "SMTP":
        if not p.target:
            raise RuntimeError("Enter a test email address")
        return _smtp(settings, secrets, to=p.target, subject=p.title, body=p.body)
    if provider == "NTFY":
        return _ntfy(settings, secrets, title=p.title, body=p.body)
    if provider == "GOTIFY":
        return _gotify(settings, secrets, title=p.title, body=p.body)
    if provider == "TWILIO_SMS":
        if not p.target: raise RuntimeError("Enter a test mobile number")
        return _twilio_send(settings,secrets,to=p.target,body=p.body)
    if provider in ("APPRISE", "O365_OUTBOUND"):
        return _apprise_send(provider, settings, secrets, title=p.title, body=p.body, target=p.target)
    if provider == "WEBHOOK":
        return _webhook(settings, secrets, {"type":"connection_test","title":p.title,"body":p.body,"created_at":utcnow()})
    if provider == "WEBDAV":
        name = f"ServiceSlate-connection-test-{uuid.uuid4().hex[:8]}.txt"
        return _dav_put(str(settings.get("base_url") or "") + "/" + str(settings.get("folder") or "").strip("/"), settings.get("username"), secrets.get("password"), name, p.body.encode(), "text/plain")
    if provider == "CALDAV":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        uid = f"serviceslate-test-{uuid.uuid4().hex}@local"
        ics = f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//ServiceSlate//EN\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTAMP:{stamp}\r\nDTSTART:{stamp}\r\nDTEND:{stamp}\r\nSUMMARY:{p.title}\r\nDESCRIPTION:{p.body}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        return _dav_put(str(settings.get("collection_url") or ""), settings.get("username"), secrets.get("password"), f"{uid}.ics", ics.encode(), "text/calendar; charset=utf-8")
    if provider == "CARDDAV":
        uid = f"serviceslate-test-{uuid.uuid4().hex}"
        vcf = f"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:ServiceSlate Connection Test\r\nUID:{uid}\r\nNOTE:{p.body}\r\nEND:VCARD\r\n"
        return _dav_put(str(settings.get("collection_url") or ""), settings.get("username"), secrets.get("password"), f"{uid}.vcf", vcf.encode(), "text/vcard; charset=utf-8")
    if provider == "S3":
        return _s3_test(settings, secrets)
    if provider == "NOMINATIM":
        base = str(settings.get("base_url") or "").rstrip("/")
        if not base:
            raise RuntimeError("Configure a Nominatim endpoint")
        if base == "https://nominatim.openstreetmap.org" and not bool(settings.get("allow_public_osm")):
            raise RuntimeError("Public OSM Nominatim must be explicitly enabled after reviewing its usage policy")
        params = urllib.parse.urlencode({"q":"1600 Amphitheatre Parkway, Mountain View, CA","format":"jsonv2","limit":1})
        headers = {"User-Agent": str(settings.get("user_agent") or "ServiceSlate/0.9 contact-admin")}
        status, raw, _ = _http_request(f"{base}/search?{params}", headers=headers)
        if status >= 300 or not json.loads(raw or b"[]"):
            raise RuntimeError("Nominatim did not return a test result")
        return "lookup-ok"
    if provider == "OSRM":
        base = str(settings.get("base_url") or "").rstrip("/")
        if not base:
            raise RuntimeError("Configure an OSRM endpoint")
        status, raw, _ = _http_request(f"{base}/route/v1/driving/-122.084,37.422;-122.081,37.426?overview=false")
        data = json.loads(raw or b"{}")
        if status >= 300 or data.get("code") != "Ok":
            raise RuntimeError("OSRM did not return a valid route estimate")
        return "route-ok"
    if provider == "TESSERACT":
        exe = str(settings.get("executable") or shutil.which("tesseract") or "")
        if not exe or not Path(exe).exists() and not shutil.which(exe):
            raise RuntimeError("Tesseract executable was not found")
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10, check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "Tesseract could not start")
        return (proc.stdout.splitlines() or ["tesseract"])[0]
    if provider == "GOOGLE_DRIVE":
        from .cloud_connectors import google_drive_test
        return google_drive_test(org or "", settings, secrets)
    if provider == "MICROSOFT_GRAPH":
        from .cloud_connectors import _graph_access_token
        _graph_access_token(org or "")
        return "microsoft-graph-ready"
    if provider == "QUICKBOOKS":
        mode = str(settings.get("mode") or "EXPORT_ONLY").upper()
        if mode == "EXPORT_ONLY": return "export-only-ready"
        if mode == "QBO":
            from .cloud_connectors import qbo_request
            qbo_request(org or "", "GET", "companyinfo/" + str(settings.get("realm_id") or ""))
            return "quickbooks-online-ready"
        if mode == "DESKTOP_WEB_CONNECTOR":
            url = str(settings.get("web_connector_url") or "").strip()
            if not url: raise RuntimeError("Hosted Web Connector URL is required")
            if not url.lower().startswith("https://"): raise RuntimeError("Web Connector URL must use HTTPS")
            if not str(settings.get("web_connector_username") or "").strip(): raise RuntimeError("Web Connector username is required")
            if not secrets.get("web_connector_password"): raise RuntimeError("Web Connector password is required")
            if not str(settings.get("desktop_service_item_name") or "").strip(): raise RuntimeError("QuickBooks Desktop service item name is required")
            return "web-connector-ready"
        raise RuntimeError("Unknown QuickBooks mode")
    if provider == "CLAMAV":
        exe = str(settings.get("executable") or shutil.which("clamscan") or "")
        if not exe: raise RuntimeError("ClamAV clamscan was not found")
        proc = subprocess.run([exe,"--version"],capture_output=True,text=True,timeout=10,check=False)
        if proc.returncode != 0: raise RuntimeError(proc.stderr.strip() or "ClamAV could not start")
        return (proc.stdout.splitlines() or ["ClamAV"])[0]
    if provider == "OUTLOOK":
        result = outlook_bridge.status()
        if not result.get("available"):
            raise RuntimeError(result.get("reason") or "Classic Outlook is not available")
        return "outlook-ready"
    if provider == "MOBILE_SMS":
        return "handoff-ready"
    raise RuntimeError("This provider does not have a connection test")


def scan_upload_if_configured(org: str, content: bytes, filename: str) -> dict[str, Any]:
    """Scan an upload with ClamAV when configured; production may require a scanner."""
    settings, _ = _provider_row(org, "CLAMAV")
    with connect() as conn:
        row = conn.execute("SELECT state FROM integration_status WHERE organization_id=? AND provider='CLAMAV'", (org,)).fetchone()
    configured = bool(settings) or bool(row and row["state"] in ("CONNECTED", "CONFIGURED"))
    required = os.environ.get("SERVICESLATE_REQUIRE_UPLOAD_SCAN", "0") == "1"
    if not configured:
        if required:
            raise RuntimeError("Upload scanning is required on this ServiceSlate host, but ClamAV is not configured")
        return {"scanned": False, "status": "NOT_CONFIGURED"}
    exe = str(settings.get("executable") or shutil.which("clamscan") or "")
    if not exe:
        if required:
            raise RuntimeError("ClamAV is configured but clamscan was not found")
        return {"scanned": False, "status": "UNAVAILABLE"}
    suffix = Path(filename or "upload.bin").suffix[:16]
    with tempfile.NamedTemporaryFile(prefix="serviceslate-upload-", suffix=suffix, delete=False) as tmp:
        tmp.write(content); tmp_path = Path(tmp.name)
    try:
        proc = subprocess.run([exe, "--no-summary", str(tmp_path)], capture_output=True, text=True, timeout=60, check=False)
        output = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode == 1:
            raise RuntimeError("Upload was blocked because the malware scanner reported a threat")
        if proc.returncode != 0:
            if required:
                raise RuntimeError("Upload scanner could not verify this file")
            return {"scanned": False, "status": "ERROR", "detail": output[:300]}
        return {"scanned": True, "status": "CLEAN"}
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/{provider}/test")
def test_connection(provider: str, p: TestMessage, request: Request):
    u = _user(request); _admin(u); provider = provider.upper()
    if provider not in PROVIDERS:
        raise HTTPException(404, "Connection type not found")
    settings, secrets = _provider_row(u["organization_id"], provider)
    try:
        ref = run_test(provider, settings, secrets, p, u["organization_id"])
        _status_update(u["organization_id"], provider, "CONNECTED", success=True)
        return {"ok": True, "provider": provider, "reference": ref}
    except Exception as exc:
        msg = str(exc)[:500]
        _status_update(u["organization_id"], provider, "NEEDS_ATTENTION", error=msg)
        raise HTTPException(400, msg) from exc


class DeliverCommunication(BaseModel):
    provider: str


@router.post("/communications/{communication_id}/deliver")
def deliver_communication(communication_id: str, p: DeliverCommunication, request: Request):
    u = _user(request)
    if u["role"] not in ("ADMIN","MANAGER","COORDINATOR","BILLING","RECEPTION"):
        raise HTTPException(403, "You do not have permission to send customer communications")
    provider = p.provider.upper()
    if provider not in PROVIDERS:
        raise HTTPException(404, "Connection type not found")
    with connect() as conn:
        row = conn.execute(
            """SELECT m.*,c.email customer_email,c.phone customer_phone,ct.email contact_email,ct.phone contact_phone
               FROM communications m JOIN customers c ON c.id=m.customer_id
               LEFT JOIN contacts ct ON ct.id=m.contact_id
               WHERE m.id=? AND m.organization_id=?""",
            (communication_id,u["organization_id"]),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Communication not found")
    if row["direction"] != "OUTBOUND" or row["status"] not in ("PREPARED","FAILED"):
        raise HTTPException(409, "Only prepared outbound messages can be delivered")
    settings, secrets = _provider_row(u["organization_id"], provider)
    target = (row["contact_email"] or row["customer_email"]) if row["channel"] == "EMAIL" else (row["contact_phone"] or row["customer_phone"])
    if not target:
        raise HTTPException(400, f"No {row['channel'].lower()} destination is available")
    title = row["subject"] or "ServiceSlate message"
    body = row["body"] or ""
    try:
        if provider == "MICROSOFT_GRAPH" and row["channel"] == "EMAIL":
            from .cloud_connectors import graph_send_email
            ref = graph_send_email(u["organization_id"], to=target, subject=title, body=body)
        elif provider == "OUTLOOK" and row["channel"] == "EMAIL":
            ref = outlook_bridge.send_email(
                to=target, subject=title, body=body,
                request_read_receipt=bool(settings.get("request_read_receipt", True)),
            )
        elif provider == "SMTP" and row["channel"] == "EMAIL":
            ref = _smtp(settings,secrets,to=target,subject=title,body=body)
        elif provider == "TWILIO_SMS" and row["channel"] == "SMS":
            with connect() as conn:
                orgrow=conn.execute("SELECT public_base_url FROM organizations WHERE id=?",(u["organization_id"],)).fetchone()
            public_base=str(orgrow["public_base_url"] or "").rstrip("/") if orgrow else ""
            callback=(public_base + "/api/integrations/twilio/status") if public_base else None
            ref = _twilio_send(settings,secrets,to=target,body=body,status_callback=callback)
        elif provider in ("APPRISE","NTFY","GOTIFY","O365_OUTBOUND","WEBHOOK"):
            if provider == "NTFY": ref = _ntfy(settings,secrets,title=title,body=body)
            elif provider == "GOTIFY": ref = _gotify(settings,secrets,title=title,body=body)
            elif provider == "WEBHOOK": ref = _webhook(settings,secrets,{"type":"customer_communication","target":target,"channel":row["channel"],"subject":title,"body":body,"communication_id":communication_id})
            else: ref = _apprise_send(provider,settings,secrets,title=title,body=body,target=target)
        else:
            raise RuntimeError("That provider cannot automatically deliver this message")
    except Exception as exc:
        now = utcnow()
        with connect() as conn:
            conn.execute("UPDATE communications SET status='FAILED',delivery_provider=?,delivery_error=?,delivery_attempted_at=? WHERE id=?", (provider,str(exc)[:500],now,communication_id))
            conn.execute("INSERT INTO integration_deliveries(id,organization_id,provider,communication_id,state,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (new_id("delivery"),u["organization_id"],provider,communication_id,"FAILED",str(exc)[:500],now,now))
        raise HTTPException(400, str(exc)) from exc
    now = utcnow()
    with connect() as conn:
        conn.execute("UPDATE communications SET status='SENT',delivery_provider=?,delivery_reference=?,delivery_error=NULL,delivery_attempted_at=?,delivered_at=NULL WHERE id=?", (provider,ref,now,communication_id))
        conn.execute("INSERT INTO integration_deliveries(id,organization_id,provider,communication_id,state,external_reference,sent_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (new_id("delivery"),u["organization_id"],provider,communication_id,"SENT",ref,now,now,now))
        audit(conn,u["organization_id"],u["id"],"communication",communication_id,"SENT",f"Sent using {PROVIDERS[provider]['name']}")
    return {"ok":True,"status":"SENT","provider":provider,"reference":ref}


@router.post("/twilio/status")
async def twilio_status(request: Request):
    form = await request.form(); params={str(k):str(v) for k,v in form.items()}
    sid=params.get("MessageSid") or params.get("SmsSid") or ""; status=(params.get("MessageStatus") or params.get("SmsStatus") or "").lower()
    if not sid: raise HTTPException(400,"Missing Twilio message reference")
    with connect() as conn:
        delivery=conn.execute("SELECT * FROM integration_deliveries WHERE provider='TWILIO_SMS' AND external_reference=? ORDER BY created_at DESC",(sid,)).fetchone()
    if not delivery: raise HTTPException(404,"Twilio message not found")
    secrets_map=VAULT.get_provider(delivery["organization_id"],"TWILIO_SMS"); token=secrets_map.get("auth_token","")
    signature=request.headers.get("X-Twilio-Signature","")
    if not token or not signature:
        raise HTTPException(403, "Twilio signature verification is required")
    with connect() as conn:
        orgrow=conn.execute("SELECT public_base_url FROM organizations WHERE id=?",(delivery["organization_id"],)).fetchone()
    if not orgrow or not str(orgrow["public_base_url"] or "").startswith("https://"):
        raise HTTPException(403, "Twilio callback requires the configured HTTPS public address")
    url=str(orgrow["public_base_url"]).rstrip("/")+"/api/integrations/twilio/status"
    expected=_twilio_signature(url,params,token)
    if not hmac.compare_digest(signature,expected): raise HTTPException(403,"Twilio signature did not verify")
    mapped="DELIVERED" if status=="delivered" else "FAILED" if status in {"failed","undelivered"} else "SENT"
    now=utcnow()
    with connect() as conn:
        conn.execute("UPDATE integration_deliveries SET state=?,delivered_at=CASE WHEN ?='DELIVERED' THEN ? ELSE delivered_at END,error=CASE WHEN ?='FAILED' THEN ? ELSE error END,updated_at=? WHERE id=?",(mapped,mapped,now,mapped,params.get("ErrorMessage") or params.get("ErrorCode"),now,delivery["id"]))
        if delivery["communication_id"]:
            conn.execute("UPDATE communications SET status=?,delivered_at=CASE WHEN ?='DELIVERED' THEN ? ELSE delivered_at END,delivery_error=CASE WHEN ?='FAILED' THEN ? ELSE delivery_error END WHERE id=?",(mapped,mapped,now,mapped,params.get("ErrorMessage") or params.get("ErrorCode"),delivery["communication_id"]))
    return {"ok":True}


@router.post("/outlook/import-eml")
async def outlook_import_eml(request: Request, file: UploadFile = File(...)):
    u = _user(request)
    if u["role"] not in ("ADMIN", "MANAGER", "COORDINATOR", "RECEPTION"):
        raise HTTPException(403, "You do not have permission to import service mailbox messages")
    raw = await file.read()
    if len(raw) > 35 * 1024 * 1024:
        raise HTTPException(413, "Email file is too large")
    try:
        envelope = parse_eml(raw)
    except Exception as exc:
        raise HTTPException(400, "That file is not a readable Outlook email (.eml)") from exc
    reply = process_service_day_reply(u["organization_id"], envelope)
    if reply and reply.get("matched"):
        return {"type": "SERVICE_DAY_REPLY", **reply}
    return {"type": "FASTFIELD", **ingest_mail_message(u["organization_id"], u["id"], "OUTLOOK_EML", envelope)}


@router.get("/outlook/status")
def outlook_status(request: Request):
    u = _user(request)
    settings, _ = _provider_row(u["organization_id"], "OUTLOOK")
    result = outlook_bridge.status()
    return {**result, "folder": settings.get("fastfield_folder") or "Inbox", "configured": bool(settings)}


@router.post("/outlook/scan")
def outlook_scan(request: Request):
    u = _user(request)
    if u["role"] not in ("ADMIN", "MANAGER", "COORDINATOR", "RECEPTION"):
        raise HTTPException(403, "You do not have permission to check the service mailbox")
    settings, _ = _provider_row(u["organization_id"], "OUTLOOK")
    try:
        result = outlook_bridge.scan(
            u["organization_id"],
            u["id"],
            folder_name=str(settings.get("fastfield_folder") or "Inbox"),
            fastfield_sender_filter=str(settings.get("fastfield_sender_filter") or ""),
            fastfield_subject_filter=str(settings.get("fastfield_subject_filter") or "FastField"),
        )
        _status_update(u["organization_id"], "OUTLOOK", "CONNECTED", success=True)
        return result
    except Exception as exc:
        _status_update(u["organization_id"], "OUTLOOK", "NEEDS_ATTENTION", error=str(exc)[:500])
        raise HTTPException(400, str(exc)) from exc


class GeocodeRequest(BaseModel):
    address: str


@router.post("/nominatim/geocode")
def geocode(p: GeocodeRequest, request: Request):
    u = _user(request)
    settings, _ = _provider_row(u["organization_id"], "NOMINATIM")
    address = p.address.strip()
    if not address:
        raise HTTPException(400, "Enter an address")
    with connect() as conn:
        cached = conn.execute("SELECT response_json FROM geocode_cache WHERE organization_id=? AND lower(query)=lower(?)", (u["organization_id"],address)).fetchone()
        if cached:
            return {"cached":True,"results":json.loads(cached[0])}
    base = str(settings.get("base_url") or "").rstrip("/")
    if not base:
        raise HTTPException(400, "Configure a Nominatim endpoint first")
    if base == "https://nominatim.openstreetmap.org" and not bool(settings.get("allow_public_osm")):
        raise HTTPException(400, "Public OSM Nominatim is disabled until an administrator explicitly enables it")
    params = urllib.parse.urlencode({"q":address,"format":"jsonv2","addressdetails":1,"limit":5})
    try:
        _, raw, _ = _http_request(f"{base}/search?{params}", headers={"User-Agent":str(settings.get("user_agent") or "ServiceSlate/0.9")})
        results = json.loads(raw or b"[]")
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    with connect() as conn:
        conn.execute("INSERT INTO geocode_cache(id,organization_id,query,response_json,created_at) VALUES(?,?,?,?,?) ON CONFLICT(organization_id,query) DO UPDATE SET response_json=excluded.response_json,created_at=excluded.created_at",
                     (new_id("geo"),u["organization_id"],address,json.dumps(results),utcnow()))
    return {"cached":False,"results":results}


class RouteEstimateRequest(BaseModel):
    from_lat: float; from_lon: float; to_lat: float; to_lon: float


@router.post("/osrm/estimate")
def route_estimate(p: RouteEstimateRequest, request: Request):
    u = _user(request)
    settings, _ = _provider_row(u["organization_id"], "OSRM")
    base = str(settings.get("base_url") or "").rstrip("/")
    if not base:
        raise HTTPException(400, "Configure an OSRM endpoint first")
    key = f"{p.from_lat:.5f},{p.from_lon:.5f}:{p.to_lat:.5f},{p.to_lon:.5f}"
    with connect() as conn:
        cached = conn.execute("SELECT response_json FROM route_cache WHERE organization_id=? AND cache_key=?", (u["organization_id"],key)).fetchone()
        if cached:
            return {"cached":True,**json.loads(cached[0])}
    url = f"{base}/route/v1/driving/{p.from_lon},{p.from_lat};{p.to_lon},{p.to_lat}?overview=false&steps=false"
    try:
        _, raw, _ = _http_request(url)
        data = json.loads(raw or b"{}")
        if data.get("code") != "Ok" or not data.get("routes"):
            raise RuntimeError("No route estimate returned")
        route = data["routes"][0]
        result = {"duration_seconds":route.get("duration"),"distance_meters":route.get("distance")}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    with connect() as conn:
        conn.execute("INSERT INTO route_cache(id,organization_id,cache_key,response_json,created_at) VALUES(?,?,?,?,?) ON CONFLICT(organization_id,cache_key) DO UPDATE SET response_json=excluded.response_json,created_at=excluded.created_at",
                     (new_id("route"),u["organization_id"],key,json.dumps(result),utcnow()))
    return {"cached":False,**result}


@router.post("/tesseract/ocr")
async def tesseract_ocr(request: Request, file: UploadFile = File(...)):
    u = _user(request)
    settings, _ = _provider_row(u["organization_id"], "TESSERACT")
    exe = str(settings.get("executable") or shutil.which("tesseract") or "")
    if not exe:
        raise HTTPException(400, "Tesseract is not configured or installed")
    language = str(settings.get("language") or "eng")
    suffix = Path(file.filename or "image.png").suffix or ".png"
    content = await file.read()
    if len(content) > 12 * 1024 * 1024:
        raise HTTPException(413, "Image is too large for local OCR")
    with tempfile.TemporaryDirectory(prefix="serviceslate-ocr-") as td:
        src = Path(td) / ("input" + suffix)
        src.write_bytes(content)
        proc = subprocess.run([exe,str(src),"stdout","-l",language,"--psm","6"],capture_output=True,text=True,timeout=30,check=False)
        if proc.returncode != 0:
            raise HTTPException(400, proc.stderr.strip() or "OCR could not read this image")
    # OCR is advisory: never writes model/serial directly to equipment.
    return {"text":proc.stdout.strip(),"requires_review":True}
