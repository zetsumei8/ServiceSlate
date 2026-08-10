from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import shutil
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from pydantic import BaseModel

from .db import BACKUPS_DIR, DATA_DIR, FILES_DIR, audit, connect, create_backup_archive, database_backend, new_id, utcnow
from .integrations import PROVIDERS, VAULT, _http_request, _provider_row, _status_update, _user
from .mail_intake import MailAttachment, MailEnvelope, ingest_mail_message, process_service_day_reply

router = APIRouter(prefix="/api/cloud")


def _admin_or_manager(user: dict[str, Any]) -> None:
    if user["role"] not in ("ADMIN", "MANAGER"):
        raise HTTPException(403, "Manager or administrator permission required")


def organization_database_backup_is_safe_for_offsite(org: str) -> tuple[bool, str | None]:
    """Prevent a shared-host database from being copied into one tenant's Drive.

    A local single-company install may mirror its complete database backup. On a
    multi-company host, database backups are host-level artifacts and must stay
    under host-managed backup policy. Demo organizations do not count as live
    tenants because their data is fictional.
    """
    with connect() as conn:
        rows = conn.execute("SELECT id FROM organizations WHERE COALESCE(is_demo,0)=0").fetchall()
    live_ids = {str(row["id"]) for row in rows}
    if len(live_ids) <= 1 and (not live_ids or org in live_ids):
        return True, None
    return False, "Shared hosting contains more than one live organization. Use the host-managed off-site database backup instead of copying the whole database into one organization's storage."


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

def _google_token(org: str, settings: dict[str, Any], secrets_map: dict[str, str]) -> str:
    refresh_token = secrets_map.get("refresh_token")
    client_id = str(settings.get("client_id") or "")
    client_secret = secrets_map.get("client_secret") or ""
    if not refresh_token or not client_id:
        raise RuntimeError("Google Drive authorization is not complete")
    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    status, raw, _ = _http_request(
        "https://oauth2.googleapis.com/token",
        method="POST",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status >= 300:
        raise RuntimeError("Google authorization could not be refreshed")
    token = json.loads(raw or b"{}").get("access_token")
    if not token:
        raise RuntimeError("Google did not return an access token")
    return str(token)


def _google_headers(token: str, content_type: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "ServiceSlate/0.9"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _google_api(settings: dict[str, Any], secrets_map: dict[str, str], path: str) -> dict[str, Any]:
    token = _google_token("", settings, secrets_map)
    status, raw, _ = _http_request(
        "https://www.googleapis.com/drive/v3/" + path.lstrip("/"),
        headers=_google_headers(token),
    )
    if status >= 300:
        raise RuntimeError(f"Google Drive returned HTTP {status}")
    return json.loads(raw or b"{}")


def _google_multipart_upload(settings: dict[str, Any], secrets_map: dict[str, str], path: Path, parent_id: str | None, remote_name: str | None = None) -> dict[str, Any]:
    token = _google_token("", settings, secrets_map)
    boundary = "serviceslate_" + secrets.token_hex(12)
    metadata: dict[str, Any] = {"name": remote_name or path.name}
    if parent_id:
        metadata["parents"] = [parent_id]
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = b"".join([
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
        json.dumps(metadata).encode(),
        b"\r\n",
        f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode(),
        path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    query = urllib.parse.urlencode({"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id,name,webViewLink,md5Checksum"})
    status, raw, _ = _http_request(
        f"https://www.googleapis.com/upload/drive/v3/files?{query}",
        method="POST",
        data=body,
        headers=_google_headers(token, f"multipart/related; boundary={boundary}"),
        timeout=60,
    )
    if status >= 300:
        raise RuntimeError(f"Google Drive upload returned HTTP {status}")
    return json.loads(raw or b"{}")


def google_drive_test(org: str, settings: dict[str, Any], secrets_map: dict[str, str]) -> str:
    mode = str(settings.get("mode") or "LOCAL_FOLDER").upper()
    if mode == "LOCAL_FOLDER":
        folder = Path(str(settings.get("local_folder") or "")).expanduser()
        if not folder:
            raise RuntimeError("Choose the Google Drive folder on this computer")
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".serviceslate-write-test"
        probe.write_text("ServiceSlate connection test", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return str(folder)
    token = _google_token(org, settings, secrets_map)
    drive_id = str(settings.get("shared_drive_id") or "").strip()
    if drive_id:
        url = f"https://www.googleapis.com/drive/v3/drives/{urllib.parse.quote(drive_id)}?fields=id,name"
    else:
        url = "https://www.googleapis.com/drive/v3/about?fields=user"
    status, raw, _ = _http_request(url, headers=_google_headers(token))
    if status >= 300:
        raise RuntimeError("Google Drive connection could not be verified")
    data = json.loads(raw or b"{}")
    return str(data.get("name") or data.get("id") or data.get("user", {}).get("displayName") or "connected")


def sync_path_to_google_drive(org: str, local_path: Path, *, local_kind: str, local_id: str, target: str = "files") -> dict[str, Any]:
    settings, secrets_map = _provider_row(org, "GOOGLE_DRIVE")
    remote_name = local_path.name
    if local_kind == "file":
        with connect() as conn:
            record = conn.execute("SELECT original_name FROM file_records WHERE id=? AND organization_id=?", (local_id, org)).fetchone()
        if record and record["original_name"]:
            # Keep Drive human-readable while making same-named uploads collision-safe.
            clean = Path(str(record["original_name"])).name
            remote_name = f"{local_id[:12]} - {clean}"
    mode = str(settings.get("mode") or "LOCAL_FOLDER").upper()
    if not local_path.exists():
        raise RuntimeError("Local file no longer exists")
    remote_id = None
    if mode == "LOCAL_FOLDER":
        root = Path(str(settings.get("local_folder") or "")).expanduser()
        if not str(root):
            raise RuntimeError("Google Drive folder is not configured")
        sub = str(settings.get("backup_subfolder") or "ServiceSlate Backups") if target == "backups" else str(settings.get("file_subfolder") or "ServiceSlate Files")
        dest_dir = root / sub
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / remote_name
        shutil.copy2(local_path, dest)
        remote_path = str(dest)
        remote_id = remote_path
    else:
        parent = str(settings.get("backup_folder_id") or "") if target == "backups" else str(settings.get("root_folder_id") or "")
        result = _google_multipart_upload(settings, secrets_map, local_path, parent or None, remote_name=remote_name)
        remote_id = str(result.get("id") or "")
        remote_path = str(result.get("webViewLink") or result.get("name") or remote_id)
    digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
    now = utcnow()
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM storage_objects WHERE organization_id=? AND provider='GOOGLE_DRIVE' AND local_kind=? AND local_id=?",
            (org, local_kind, local_id),
        ).fetchone()
        sid = existing["id"] if existing else new_id("store")
        if existing:
            conn.execute("UPDATE storage_objects SET local_path=?,remote_id=?,remote_path=?,sha256=?,state='SYNCED',last_error=NULL,synced_at=?,updated_at=? WHERE id=?",
                         (str(local_path), remote_id, remote_path, digest, now, now, sid))
        else:
            conn.execute("INSERT INTO storage_objects(id,organization_id,provider,local_kind,local_id,local_path,remote_id,remote_path,sha256,state,synced_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'SYNCED',?,?,?)",
                         (sid, org, "GOOGLE_DRIVE", local_kind, local_id, str(local_path), remote_id, remote_path, digest, now, now, now))
    return {"state": "SYNCED", "remote_id": remote_id, "remote_path": remote_path}


def sync_path_to_s3(org: str, local_path: Path, *, local_kind: str, local_id: str, target: str = "files") -> dict[str, Any]:
    settings, secrets_map = _provider_row(org, "S3")
    bucket = str(settings.get("bucket") or os.environ.get("SERVICESLATE_DEFAULT_S3_BUCKET", "")).strip()
    if not bucket:
        raise RuntimeError("S3-compatible storage bucket is not configured")
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("S3-compatible storage requires boto3") from exc
    prefix = str(settings.get("prefix") or "ServiceSlate").strip("/")
    folder = "backups" if target == "backups" else "files"
    key = "/".join(x for x in (prefix, org, folder, local_path.name) if x)
    client = boto3.client(
        "s3",
        endpoint_url=str(settings.get("endpoint_url") or os.environ.get("SERVICESLATE_DEFAULT_S3_ENDPOINT", "")) or None,
        region_name=str(settings.get("region") or os.environ.get("SERVICESLATE_DEFAULT_S3_REGION", "")) or None,
        aws_access_key_id=str(settings.get("access_key_id") or os.environ.get("SERVICESLATE_DEFAULT_S3_ACCESS_KEY", "")) or None,
        aws_secret_access_key=secrets_map.get("secret_access_key") or os.environ.get("SERVICESLATE_DEFAULT_S3_SECRET_KEY") or None,
    )
    client.upload_file(str(local_path), bucket, key)
    digest = hashlib.sha256(local_path.read_bytes()).hexdigest(); now = utcnow()
    remote_path = f"s3://{bucket}/{key}"
    with connect() as conn:
        existing = conn.execute("SELECT id FROM storage_objects WHERE organization_id=? AND provider='S3' AND local_kind=? AND local_id=?", (org, local_kind, local_id)).fetchone()
        sid = existing["id"] if existing else new_id("store")
        if existing:
            conn.execute("UPDATE storage_objects SET local_path=?,remote_id=?,remote_path=?,sha256=?,state='SYNCED',last_error=NULL,synced_at=?,updated_at=? WHERE id=?", (str(local_path), key, remote_path, digest, now, now, sid))
        else:
            conn.execute("INSERT INTO storage_objects(id,organization_id,provider,local_kind,local_id,local_path,remote_id,remote_path,sha256,state,synced_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'SYNCED',?,?,?)", (sid, org, "S3", local_kind, local_id, str(local_path), key, remote_path, digest, now, now, now))
    return {"state":"SYNCED","remote_id":key,"remote_path":remote_path}


def restore_company_file(org: str, local_id: str, destination: Path) -> dict[str, Any] | None:
    """Restore a missing local file from a previously confirmed storage mirror.

    Local files remain the normal fast path. Hosted object storage/Drive is a
    durable mirror and can repopulate the local cache without changing record
    identity or history.
    """
    with connect() as conn:
        mirrors = [dict(r) for r in conn.execute(
            "SELECT * FROM storage_objects WHERE organization_id=? AND local_kind='file' AND local_id=? AND state='SYNCED' ORDER BY CASE provider WHEN 'S3' THEN 0 ELSE 1 END, synced_at DESC",
            (org, local_id),
        ).fetchall()]
    for mirror in mirrors:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if mirror["provider"] == "S3":
                settings, secrets_map = _provider_row(org, "S3")
                import boto3
                bucket = str(settings.get("bucket") or os.environ.get("SERVICESLATE_DEFAULT_S3_BUCKET", "")).strip()
                if not bucket or not mirror.get("remote_id"):
                    continue
                client = boto3.client(
                    "s3",
                    endpoint_url=str(settings.get("endpoint_url") or os.environ.get("SERVICESLATE_DEFAULT_S3_ENDPOINT", "")) or None,
                    region_name=str(settings.get("region") or os.environ.get("SERVICESLATE_DEFAULT_S3_REGION", "")) or None,
                    aws_access_key_id=str(settings.get("access_key_id") or os.environ.get("SERVICESLATE_DEFAULT_S3_ACCESS_KEY", "")) or None,
                    aws_secret_access_key=secrets_map.get("secret_access_key") or os.environ.get("SERVICESLATE_DEFAULT_S3_SECRET_KEY") or None,
                )
                client.download_file(bucket, str(mirror["remote_id"]), str(destination))
            elif mirror["provider"] == "GOOGLE_DRIVE":
                settings, secrets_map = _provider_row(org, "GOOGLE_DRIVE")
                mode = str(settings.get("mode") or "LOCAL_FOLDER").upper()
                if mode == "LOCAL_FOLDER":
                    remote = Path(str(mirror.get("remote_path") or ""))
                    if not remote.exists():
                        continue
                    shutil.copy2(remote, destination)
                else:
                    token = _google_token(org, settings, secrets_map)
                    remote_id = str(mirror.get("remote_id") or "")
                    if not remote_id:
                        continue
                    status, raw, _ = _http_request(
                        f"https://www.googleapis.com/drive/v3/files/{urllib.parse.quote(remote_id, safe='')}?alt=media&supportsAllDrives=true",
                        headers=_google_headers(token),
                        timeout=60,
                    )
                    if status >= 300:
                        continue
                    destination.write_bytes(raw)
            else:
                continue
            expected = str(mirror.get("sha256") or "")
            actual = hashlib.sha256(destination.read_bytes()).hexdigest()
            if expected and actual != expected:
                destination.unlink(missing_ok=True)
                continue
            return {"provider": mirror["provider"], "state": "RESTORED", "path": str(destination)}
        except Exception:
            destination.unlink(missing_ok=True)
            continue
    return None


def mirror_company_file(org: str, local_path: Path, *, local_kind: str, local_id: str) -> list[dict[str, Any]]:
    """Best-effort mirror to company-approved storage. Core local data remains valid on mirror failure."""
    results: list[dict[str, Any]] = []
    for provider in ("GOOGLE_DRIVE", "S3"):
        settings, _ = _provider_row(org, provider)
        enabled = bool(settings.get("sync_files")) if provider == "GOOGLE_DRIVE" else bool(settings.get("sync_files") or settings.get("mirror_files") or os.environ.get("SERVICESLATE_DEFAULT_S3_SYNC_FILES", "0") == "1")
        if not enabled:
            continue
        try:
            if provider == "GOOGLE_DRIVE":
                result = sync_path_to_google_drive(org, local_path, local_kind=local_kind, local_id=local_id, target="files")
            else:
                result = sync_path_to_s3(org, local_path, local_kind=local_kind, local_id=local_id, target="files")
            results.append({"provider":provider,**result})
        except Exception as exc:
            now=utcnow()
            with connect() as conn:
                existing=conn.execute("SELECT id FROM storage_objects WHERE organization_id=? AND provider=? AND local_kind=? AND local_id=?",(org,provider,local_kind,local_id)).fetchone()
                sid=existing["id"] if existing else new_id("store")
                if existing:
                    conn.execute("UPDATE storage_objects SET state='NEEDS_ATTENTION',last_error=?,updated_at=? WHERE id=?",(str(exc)[:500],now,sid))
                else:
                    conn.execute("INSERT INTO storage_objects(id,organization_id,provider,local_kind,local_id,local_path,state,last_error,created_at,updated_at) VALUES(?,?,?,?,?,?,'NEEDS_ATTENTION',?,?,?)",(sid,org,provider,local_kind,local_id,str(local_path),str(exc)[:500],now,now))
            results.append({"provider":provider,"state":"NEEDS_ATTENTION","error":str(exc)[:500]})
    return results


class DriveAuthorize(BaseModel):
    client_id: str
    client_secret: str | None = None


@router.get("/google-drive/shared-drives")
def google_shared_drives(request: Request):
    u = _user(request); _admin_or_manager(u)
    settings, secrets_map = _provider_row(u["organization_id"], "GOOGLE_DRIVE")
    token = _google_token(u["organization_id"], settings, secrets_map)
    status, raw, _ = _http_request(
        "https://www.googleapis.com/drive/v3/drives?pageSize=100&fields=drives(id,name)",
        headers=_google_headers(token),
    )
    if status >= 300:
        raise HTTPException(400, "Could not list Google Shared Drives")
    return json.loads(raw or b"{}").get("drives", [])


@router.get("/google-drive/authorize-url")
def google_authorize_url(request: Request):
    u = _user(request); _admin_or_manager(u)
    settings, _ = _provider_row(u["organization_id"], "GOOGLE_DRIVE")
    client_id = str(settings.get("client_id") or "")
    if not client_id:
        raise HTTPException(400, "Add the Google OAuth client ID first")
    base = str(settings.get("redirect_base_url") or request.base_url).rstrip("/")
    redirect_uri = base + "/api/cloud/google-drive/oauth/callback"
    state = secrets.token_urlsafe(24)
    request.session["google_oauth_state"] = state
    request.session["google_oauth_org"] = u["organization_id"]
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/drive",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params), "redirect_uri": redirect_uri}


@router.get("/google-drive/oauth/callback")
def google_oauth_callback(request: Request, code: str, state: str):
    if not state or state != request.session.get("google_oauth_state"):
        raise HTTPException(400, "Google authorization state did not match")
    org = request.session.get("google_oauth_org")
    if not org:
        raise HTTPException(400, "Google authorization session expired")
    settings, secrets_map = _provider_row(org, "GOOGLE_DRIVE")
    client_id = str(settings.get("client_id") or "")
    client_secret = secrets_map.get("client_secret") or ""
    base = str(settings.get("redirect_base_url") or request.base_url).rstrip("/")
    redirect_uri = base + "/api/cloud/google-drive/oauth/callback"
    payload = urllib.parse.urlencode({"client_id":client_id,"client_secret":client_secret,"code":code,"grant_type":"authorization_code","redirect_uri":redirect_uri}).encode()
    try:
        _, raw, _ = _http_request("https://oauth2.googleapis.com/token", method="POST", data=payload, headers={"Content-Type":"application/x-www-form-urlencoded"})
        token = json.loads(raw or b"{}")
        refresh = token.get("refresh_token")
        if not refresh:
            raise RuntimeError("Google did not return a refresh token")
        VAULT.set_provider(org, "GOOGLE_DRIVE", {"refresh_token": str(refresh)})
        _status_update(org, "GOOGLE_DRIVE", "CONNECTED", success=True)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "message": "Google Drive connected. You can close this tab and return to ServiceSlate."}


@router.post("/google-drive/backup-now")
def google_backup_now(request: Request):
    u = _user(request); _admin_or_manager(u)
    allowed, reason = organization_database_backup_is_safe_for_offsite(u["organization_id"])
    if not allowed:
        raise HTTPException(409, reason)
    path = create_backup_archive("GoogleDrive")
    try:
        result = sync_path_to_google_drive(u["organization_id"], path, local_kind="backup", local_id=path.name, target="backups")
        audit_record = f"Backed up {path.name} to Google Drive"
        with connect() as conn:
            audit(conn, u["organization_id"], u["id"], "backup", path.name, "OFFSITE_SYNCED", audit_record)
        return {"backup": path.name, **result}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/google-drive/file/{file_id}")
def google_sync_file(file_id: str, request: Request):
    u = _user(request)
    with connect() as conn:
        row = conn.execute("SELECT * FROM file_records WHERE id=? AND organization_id=?", (file_id, u["organization_id"])).fetchone()
    if not row:
        raise HTTPException(404, "File not found")
    path = FILES_DIR / row["stored_name"]
    try:
        return sync_path_to_google_drive(u["organization_id"], path, local_kind="file", local_id=file_id, target="files")
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# Microsoft Graph / new Outlook / calendar sync
# ---------------------------------------------------------------------------

GRAPH_SCOPES = ["Mail.Read", "Mail.Send", "Calendars.ReadWrite", "offline_access", "User.Read"]


def _graph_cache(org: str):
    try:
        import msal
    except ImportError as exc:
        raise RuntimeError("Microsoft Graph support requires msal") from exc
    secrets_map = VAULT.get_provider(org, "MICROSOFT_GRAPH")
    cache = msal.SerializableTokenCache()
    if secrets_map.get("token_cache"):
        cache.deserialize(secrets_map["token_cache"])
    return msal, cache


def _graph_app(org: str):
    settings, secrets_map = _provider_row(org, "MICROSOFT_GRAPH")
    msal, cache = _graph_cache(org)
    client_id = str(settings.get("client_id") or "")
    tenant = str(settings.get("tenant_id") or "organizations")
    if not client_id:
        raise RuntimeError("Microsoft Graph client ID is not configured")
    authority = f"https://login.microsoftonline.com/{tenant}"
    if secrets_map.get("client_secret"):
        app = msal.ConfidentialClientApplication(client_id, authority=authority, client_credential=secrets_map["client_secret"], token_cache=cache)
    else:
        app = msal.PublicClientApplication(client_id, authority=authority, token_cache=cache)
    return app, cache, settings


def _save_graph_cache(org: str, cache) -> None:
    if cache.has_state_changed:
        VAULT.set_provider(org, "MICROSOFT_GRAPH", {"token_cache": cache.serialize()})


def _graph_access_token(org: str) -> str:
    app, cache, _ = _graph_app(org)
    accounts = app.get_accounts()
    result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0] if accounts else None)
    _save_graph_cache(org, cache)
    if not result or not result.get("access_token"):
        raise RuntimeError("Microsoft sign-in is required")
    return str(result["access_token"])


def _graph_request(org: str, method: str, path_or_url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    token = _graph_access_token(org)
    url = path_or_url if path_or_url.startswith("https://") else "https://graph.microsoft.com/v1.0/" + path_or_url.lstrip("/")
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization":f"Bearer {token}","Accept":"application/json","User-Agent":"ServiceSlate/0.9"}
    if data is not None: headers["Content-Type"] = "application/json"
    status, raw, _ = _http_request(url, method=method, data=data, headers=headers, timeout=30)
    if status >= 300:
        raise RuntimeError(f"Microsoft Graph returned HTTP {status}")
    return json.loads(raw or b"{}") if raw else {}


_GRAPH_DEVICE_FLOWS: dict[str, dict[str, Any]] = {}


@router.post("/microsoft/start")
def microsoft_device_start(request: Request):
    u = _user(request); _admin_or_manager(u)
    app, _, _ = _graph_app(u["organization_id"])
    flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
    if "user_code" not in flow:
        raise HTTPException(400, flow.get("error_description") or "Microsoft sign-in could not start")
    _GRAPH_DEVICE_FLOWS[u["organization_id"]] = flow
    return {"user_code":flow["user_code"],"verification_uri":flow.get("verification_uri") or flow.get("verification_uri_complete"),"message":flow.get("message"),"expires_in":flow.get("expires_in")}


@router.post("/microsoft/finish")
def microsoft_device_finish(request: Request):
    u = _user(request); _admin_or_manager(u)
    flow = _GRAPH_DEVICE_FLOWS.get(u["organization_id"])
    if not flow:
        raise HTTPException(400, "Start Microsoft sign-in first")
    app, cache, _ = _graph_app(u["organization_id"])
    result = app.acquire_token_by_device_flow(flow, exit_condition=lambda f: True)
    if not result or not result.get("access_token"):
        raise HTTPException(409, result.get("error_description") or "Microsoft sign-in has not completed yet")
    _save_graph_cache(u["organization_id"], cache)
    _status_update(u["organization_id"], "MICROSOFT_GRAPH", "CONNECTED", success=True)
    _GRAPH_DEVICE_FLOWS.pop(u["organization_id"], None)
    return {"ok": True, "account": (result.get("id_token_claims") or {}).get("preferred_username")}


def graph_send_email(org: str, *, to: str, subject: str, body: str) -> str:
    _graph_request(org, "POST", "/me/sendMail", {"message":{"subject":subject,"body":{"contentType":"Text","content":body},"toRecipients":[{"emailAddress":{"address":to}}]},"saveToSentItems":True})
    return "graph-sendmail"


def _graph_mail_folder_id(org: str, folder_name: str) -> str:
    data = _graph_request(org, "GET", "/me/mailFolders?$top=100&$select=id,displayName")
    for folder in data.get("value", []):
        if str(folder.get("displayName", "")).lower() == folder_name.lower():
            return str(folder["id"])
    if folder_name.lower() == "inbox":
        return "inbox"
    raise RuntimeError(f"Outlook folder '{folder_name}' was not found")


def graph_scan_mail(org: str, actor_user_id: str) -> dict[str, Any]:
    settings, _ = _provider_row(org, "MICROSOFT_GRAPH")
    folder_name = str(settings.get("mail_folder") or "Inbox")
    folder_id = _graph_mail_folder_id(org, folder_name)
    with connect() as conn:
        sync = conn.execute("SELECT * FROM mail_sync_state WHERE organization_id=? AND provider='MICROSOFT_GRAPH' AND folder=?", (org, folder_name)).fetchone()
    url = sync["delta_link"] if sync and sync["delta_link"] else f"/me/mailFolders/{urllib.parse.quote(folder_id, safe='')}/messages/delta?$select=id,internetMessageId,subject,from,receivedDateTime,bodyPreview,body,hasAttachments"
    messages: list[dict[str, Any]] = []
    last_delta = None
    while url:
        page = _graph_request(org, "GET", url)
        messages.extend(page.get("value", []))
        url = page.get("@odata.nextLink")
        last_delta = page.get("@odata.deltaLink") or last_delta
    results=[]; ff=0; replies=0
    for msg in messages:
        if msg.get("@removed"):
            continue
        external_id=str(msg.get("id") or "")
        if not external_id: continue
        with connect() as conn:
            if conn.execute("SELECT 1 FROM mail_intake_messages WHERE organization_id=? AND provider='MICROSOFT_GRAPH' AND external_id=?", (org,external_id)).fetchone():
                continue
        sender=((msg.get("from") or {}).get("emailAddress") or {}).get("address") or ""
        attachments=[]
        if msg.get("hasAttachments"):
            att_data=_graph_request(org,"GET",f"/me/messages/{urllib.parse.quote(external_id,safe='')}/attachments?$select=id,name,contentType,contentBytes,@odata.type")
            for att in att_data.get("value",[]):
                if att.get("contentBytes"):
                    try: content=base64.b64decode(att["contentBytes"])
                    except Exception: continue
                    attachments.append(MailAttachment(filename=str(att.get("name") or "attachment.bin"), content_type=str(att.get("contentType") or "application/octet-stream"), content=content))
        env=MailEnvelope(external_id=external_id,internet_message_id=str(msg.get("internetMessageId") or ""),sender=str(sender),subject=str(msg.get("subject") or ""),received_at=str(msg.get("receivedDateTime") or ""),body_text=str((msg.get("body") or {}).get("content") or msg.get("bodyPreview") or ""),attachments=attachments)
        reply=process_service_day_reply(org,env)
        if reply and reply.get("matched"):
            results.append({"type":"SERVICE_DAY_REPLY",**reply}); replies+=1
        else:
            result=ingest_mail_message(org,actor_user_id,"MICROSOFT_GRAPH",env)
            results.append({"type":"FASTFIELD",**result})
            if result.get("batch_id"): ff+=1
    now=utcnow()
    with connect() as conn:
        existing=conn.execute("SELECT id FROM mail_sync_state WHERE organization_id=? AND provider='MICROSOFT_GRAPH' AND folder=?",(org,folder_name)).fetchone()
        if existing:
            conn.execute("UPDATE mail_sync_state SET delta_link=?,last_synced_at=?,updated_at=? WHERE id=?",(last_delta,now,now,existing["id"]))
        else:
            conn.execute("INSERT INTO mail_sync_state(id,organization_id,provider,folder,delta_link,last_synced_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(new_id("mailsync"),org,"MICROSOFT_GRAPH",folder_name,last_delta,now,now,now))
    return {"processed":len(results),"fastfield":ff,"service_day_replies":replies,"results":results}


@router.post("/microsoft/mail/scan")
def graph_mail_scan(request: Request):
    u=_user(request)
    if u["role"] not in ("ADMIN","MANAGER","COORDINATOR","RECEPTION"):
        raise HTTPException(403,"Mailbox permission required")
    try:
        result=graph_scan_mail(u["organization_id"],u["id"])
        _status_update(u["organization_id"],"MICROSOFT_GRAPH","CONNECTED",success=True)
        return result
    except Exception as exc:
        _status_update(u["organization_id"],"MICROSOFT_GRAPH","NEEDS_ATTENTION",error=str(exc)[:500])
        raise HTTPException(400,str(exc)) from exc


def _visit_event_payload(row: dict[str, Any]) -> dict[str, Any]:
    def dt(value: str) -> dict[str,str]:
        parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
        return {"dateTime":parsed.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),"timeZone":"UTC"}
    return {
        "subject":f"ServiceSlate {row['job_number']} — {row['customer_name']}",
        "body":{"contentType":"Text","content":f"ServiceSlate work order {row['job_number']}\n{row['description']}\nServiceSlate Visit ID: {row['id']}"},
        "start":dt(row["start_at"]),"end":dt(row["end_at"]),
        "location":{"displayName":row.get("location_name") or ""},
        "categories":["ServiceSlate"],
    }


def graph_calendar_sync(org: str, actor_user_id: str) -> dict[str, Any]:
    now=datetime.now(timezone.utc); start=(now-timedelta(days=30)).isoformat(); end=(now+timedelta(days=180)).isoformat()
    with connect() as conn:
        visits=[dict(r) for r in conn.execute("""SELECT v.*,j.job_number,j.description,c.name customer_name,l.name location_name
            FROM visits v JOIN jobs j ON j.id=v.job_id JOIN customers c ON c.id=j.customer_id JOIN locations l ON l.id=j.location_id
            WHERE v.organization_id=? AND v.execution_status!='CANCELED' AND v.start_at>=? AND v.start_at<=?""",(org,start,end)).fetchall()]
    pushed=0
    for row in visits:
        payload=_visit_event_payload(row)
        try:
            if row.get("external_event_id") and row.get("external_calendar_provider")=="MICROSOFT_GRAPH":
                _graph_request(org,"PATCH",f"/me/events/{urllib.parse.quote(row['external_event_id'],safe='')}",payload)
                event_id=row["external_event_id"]
            else:
                created=_graph_request(org,"POST","/me/events",payload); event_id=str(created.get("id") or "")
            if event_id:
                with connect() as conn:
                    conn.execute("UPDATE visits SET external_calendar_provider='MICROSOFT_GRAPH',external_event_id=?,calendar_sync_state='SYNCED',calendar_last_synced_at=?,updated_at=? WHERE id=?",(event_id,utcnow(),utcnow(),row["id"]))
                pushed+=1
        except Exception as exc:
            with connect() as conn:
                conn.execute("UPDATE visits SET calendar_sync_state='NEEDS_ATTENTION',updated_at=? WHERE id=?",(utcnow(),row["id"]))
            raise RuntimeError(f"Calendar sync failed for {row['job_number']}: {exc}") from exc
    # Pull external changes for the same fixed window. External edits become reviewable conflicts, never silent schedule changes.
    with connect() as conn:
        state=conn.execute("SELECT * FROM calendar_sync_state WHERE organization_id=? AND provider='MICROSOFT_GRAPH' AND calendar_key='default'",(org,)).fetchone()
    url=state["delta_link"] if state and state["delta_link"] and state["window_start"]==start[:10] else f"/me/calendarView/delta?startDateTime={urllib.parse.quote(start)}&endDateTime={urllib.parse.quote(end)}"
    events=[]; delta=None
    while url:
        page=_graph_request(org,"GET",url); events.extend(page.get("value",[])); url=page.get("@odata.nextLink"); delta=page.get("@odata.deltaLink") or delta
    conflicts=0
    for event in events:
        eid=str(event.get("id") or "")
        if not eid: continue
        with connect() as conn:
            visit=conn.execute("SELECT * FROM visits WHERE organization_id=? AND external_event_id=?",(org,eid)).fetchone()
            if not visit: continue
            if event.get("@removed"):
                kind="EXTERNAL_DELETE"; external={"removed":True}
            else:
                ext_start=((event.get("start") or {}).get("dateTime") or "")
                ext_end=((event.get("end") or {}).get("dateTime") or "")
                # Graph values may omit a timezone offset. Compare normalized wall values conservatively.
                local_start=visit["start_at"][:19]; local_end=visit["end_at"][:19]
                if ext_start[:19]==local_start and ext_end[:19]==local_end: continue
                kind="EXTERNAL_TIME_CHANGE"; external={"start":event.get("start"),"end":event.get("end"),"subject":event.get("subject")}
            exists=conn.execute("SELECT 1 FROM calendar_conflicts WHERE organization_id=? AND external_event_id=? AND status='NEEDS_REVIEW'",(org,eid)).fetchone()
            if not exists:
                conn.execute("INSERT INTO calendar_conflicts(id,organization_id,provider,visit_id,external_event_id,kind,local_json,external_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'NEEDS_REVIEW',?,?)",
                             (new_id("calconf"),org,"MICROSOFT_GRAPH",visit["id"],eid,kind,json.dumps({"start":visit["start_at"],"end":visit["end_at"]}),json.dumps(external),utcnow(),utcnow()))
                conflicts+=1
    with connect() as conn:
        existing=conn.execute("SELECT id FROM calendar_sync_state WHERE organization_id=? AND provider='MICROSOFT_GRAPH' AND calendar_key='default'",(org,)).fetchone(); n=utcnow()
        if existing: conn.execute("UPDATE calendar_sync_state SET delta_link=?,window_start=?,window_end=?,last_synced_at=?,updated_at=? WHERE id=?",(delta,start[:10],end[:10],n,n,existing["id"]))
        else: conn.execute("INSERT INTO calendar_sync_state(id,organization_id,provider,calendar_key,delta_link,window_start,window_end,last_synced_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(new_id("calsync"),org,"MICROSOFT_GRAPH","default",delta,start[:10],end[:10],n,n,n))
        audit(conn,org,actor_user_id,"calendar","MICROSOFT_GRAPH","SYNCED",f"Synced {pushed} ServiceSlate visits; {conflicts} external change(s) need review")
    return {"pushed":pushed,"conflicts_created":conflicts}


@router.post("/microsoft/calendar/sync")
def microsoft_calendar_sync(request: Request):
    u=_user(request)
    if u["role"] not in ("ADMIN","MANAGER","COORDINATOR"):
        raise HTTPException(403,"Calendar sync permission required")
    try: return graph_calendar_sync(u["organization_id"],u["id"])
    except Exception as exc: raise HTTPException(400,str(exc)) from exc


@router.get("/microsoft/calendar/conflicts")
def microsoft_calendar_conflicts(request: Request):
    u=_user(request)
    with connect() as conn:
        rows=conn.execute("SELECT * FROM calendar_conflicts WHERE organization_id=? AND status='NEEDS_REVIEW' ORDER BY created_at DESC",(u["organization_id"],)).fetchall()
    return [dict(r) for r in rows]


class ResolveCalendarConflict(BaseModel):
    resolution: str


@router.post("/microsoft/calendar/conflicts/{conflict_id}/resolve")
def resolve_calendar_conflict(conflict_id: str,p:ResolveCalendarConflict,request:Request):
    u=_user(request)
    if u["role"] not in ("ADMIN","MANAGER","COORDINATOR"): raise HTTPException(403)
    resolution=p.resolution.upper()
    if resolution not in ("KEEP_SERVICESLATE","ACCEPT_EXTERNAL"): raise HTTPException(400,"Choose Keep ServiceSlate or Accept Outlook change")
    with connect() as conn:
        row=conn.execute("SELECT * FROM calendar_conflicts WHERE id=? AND organization_id=? AND status='NEEDS_REVIEW'",(conflict_id,u["organization_id"])).fetchone()
        if not row: raise HTTPException(404,"Calendar conflict not found")
        if resolution=="ACCEPT_EXTERNAL" and row["kind"]=="EXTERNAL_TIME_CHANGE":
            ext=json.loads(row["external_json"] or "{}")
            start=((ext.get("start") or {}).get("dateTime") or ""); end=((ext.get("end") or {}).get("dateTime") or "")
            if not start or not end: raise HTTPException(400,"External event did not include a complete time")
            conn.execute("UPDATE visits SET start_at=?,end_at=?,version=version+1,calendar_sync_state='SYNCED',updated_at=? WHERE id=?",(start,end,utcnow(),row["visit_id"]))
        elif resolution=="ACCEPT_EXTERNAL" and row["kind"]=="EXTERNAL_DELETE":
            raise HTTPException(409,"Deleting a ServiceSlate visit requires the normal cancellation workflow; Outlook cannot silently cancel it")
        conn.execute("UPDATE calendar_conflicts SET status='RESOLVED',reviewed_by_user_id=?,reviewed_at=?,resolution=?,updated_at=? WHERE id=?",(u["id"],utcnow(),resolution,utcnow(),conflict_id))
        audit(conn,u["organization_id"],u["id"],"calendar_conflict",conflict_id,"RESOLVED",resolution)
    return {"ok":True,"resolution":resolution}


# ---------------------------------------------------------------------------
# QuickBooks activation helpers. QBO can be live; Desktop remains admin-gated.
# ---------------------------------------------------------------------------


def _qbo_token(org: str, settings: dict[str, Any], secrets_map: dict[str,str]) -> str:
    refresh=secrets_map.get("refresh_token"); cid=str(settings.get("client_id") or ""); secret=secrets_map.get("client_secret") or ""
    if not all((refresh,cid,secret)): raise RuntimeError("QuickBooks Online authorization is incomplete")
    basic=base64.b64encode(f"{cid}:{secret}".encode()).decode()
    data=urllib.parse.urlencode({"grant_type":"refresh_token","refresh_token":refresh}).encode()
    status,raw,_=_http_request("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",method="POST",data=data,headers={"Authorization":f"Basic {basic}","Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"})
    if status>=300: raise RuntimeError("QuickBooks token refresh failed")
    payload=json.loads(raw or b"{}"); token=payload.get("access_token")
    if payload.get("refresh_token"): VAULT.set_provider(org,"QUICKBOOKS",{"refresh_token":str(payload["refresh_token"])})
    if not token: raise RuntimeError("QuickBooks did not return an access token")
    return str(token)


def qbo_request(org:str,method:str,path:str,payload:dict[str,Any]|None=None)->dict[str,Any]:
    settings,secrets_map=_provider_row(org,"QUICKBOOKS"); realm=str(settings.get("realm_id") or "")
    if not realm: raise RuntimeError("QuickBooks company ID is not configured")
    token=_qbo_token(org,settings,secrets_map); base="https://quickbooks.api.intuit.com" if str(settings.get("environment") or "production")=="production" else "https://sandbox-quickbooks.api.intuit.com"
    data=json.dumps(payload).encode() if payload is not None else None
    clean_path=path.lstrip("/")
    sep="&" if "?" in clean_path else "?"
    status,raw,_=_http_request(f"{base}/v3/company/{realm}/{clean_path}{sep}minorversion=75",method=method,data=data,headers={"Authorization":f"Bearer {token}","Accept":"application/json","Content-Type":"application/json"},timeout=30)
    if status>=300: raise RuntimeError(f"QuickBooks returned HTTP {status}")
    return json.loads(raw or b"{}") if raw else {}


_QBO_SCOPES = "com.intuit.quickbooks.accounting"

@router.get("/quickbooks/qbo/authorize-url")
def qbo_authorize_url(request: Request):
    u=_user(request); _admin_or_manager(u)
    settings,secrets_map=_provider_row(u["organization_id"],"QUICKBOOKS")
    client_id=str(settings.get("client_id") or "");
    if not client_id: raise HTTPException(400,"Add the QuickBooks client ID first")
    base=str(settings.get("redirect_base_url") or request.base_url).rstrip("/")
    redirect_uri=base+"/api/cloud/quickbooks/qbo/oauth/callback"
    state=secrets.token_urlsafe(24); request.session["qbo_oauth_state"]=state; request.session["qbo_oauth_org"]=u["organization_id"]
    params={"client_id":client_id,"response_type":"code","scope":_QBO_SCOPES,"redirect_uri":redirect_uri,"state":state}
    return {"url":"https://appcenter.intuit.com/connect/oauth2?"+urllib.parse.urlencode(params),"redirect_uri":redirect_uri}


@router.get("/quickbooks/qbo/oauth/callback")
def qbo_oauth_callback(request: Request, code: str, state: str, realmId: str):
    if state != request.session.get("qbo_oauth_state"): raise HTTPException(400,"QuickBooks authorization state did not match")
    org=request.session.get("qbo_oauth_org")
    if not org: raise HTTPException(400,"QuickBooks authorization session expired")
    settings,secrets_map=_provider_row(org,"QUICKBOOKS")
    client_id=str(settings.get("client_id") or ""); client_secret=secrets_map.get("client_secret") or ""
    if not client_id or not client_secret: raise HTTPException(400,"QuickBooks client credentials are incomplete")
    base=str(settings.get("redirect_base_url") or request.base_url).rstrip("/"); redirect_uri=base+"/api/cloud/quickbooks/qbo/oauth/callback"
    basic=base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    payload=urllib.parse.urlencode({"grant_type":"authorization_code","code":code,"redirect_uri":redirect_uri}).encode()
    try:
        _,raw,_=_http_request("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",method="POST",data=payload,headers={"Authorization":f"Basic {basic}","Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"})
        tokens=json.loads(raw or b"{}")
        refresh=str(tokens.get("refresh_token") or "")
        if not refresh: raise RuntimeError("QuickBooks did not return a refresh token")
        VAULT.set_provider(org,"QUICKBOOKS",{"refresh_token":refresh})
        settings={**settings,"mode":"QBO","realm_id":realmId,"redirect_base_url":base}
        now=utcnow()
        with connect() as conn:
            conn.execute("UPDATE integration_status SET settings_json=?,state='CONNECTED',last_error=NULL,last_success_at=?,updated_at=? WHERE organization_id=? AND provider='QUICKBOOKS'",(json.dumps(settings),now,now,org))
    except Exception as exc:
        raise HTTPException(400,str(exc)) from exc
    return {"ok":True,"message":"QuickBooks connected. You can close this tab and return to ServiceSlate."}


@router.get("/quickbooks/qbo/items")
def qbo_items(request: Request):
    u=_user(request); _admin_or_manager(u)
    try:
        data=qbo_request(u["organization_id"],"GET","query?query="+urllib.parse.quote("select * from Item where Active = true maxresults 100"))
    except Exception as exc: raise HTTPException(400,str(exc)) from exc
    items=((data.get("QueryResponse") or {}).get("Item") or [])
    return [{"id":x.get("Id"),"name":x.get("Name"),"type":x.get("Type")} for x in items]


def _qbo_customer_for(org: str, customer: dict[str,Any]) -> str:
    with connect() as conn:
        link=conn.execute("SELECT external_id FROM integration_entity_links WHERE organization_id=? AND provider='QUICKBOOKS' AND entity_type='customer' AND entity_id=?",(org,customer["id"])).fetchone()
    if link: return str(link["external_id"])
    escaped=str(customer["name"]).replace("'","\\'")
    data=qbo_request(org,"GET","query?query="+urllib.parse.quote(f"select * from Customer where DisplayName = '{escaped}' maxresults 1"))
    found=((data.get("QueryResponse") or {}).get("Customer") or [])
    if found: external=str(found[0]["Id"])
    else:
        payload={"DisplayName":customer["name"],"PrimaryEmailAddr":{"Address":customer.get("email") or ""},"PrimaryPhone":{"FreeFormNumber":customer.get("phone") or ""}}
        result=qbo_request(org,"POST","customer",payload); external=str((result.get("Customer") or {}).get("Id") or "")
    if not external: raise RuntimeError("QuickBooks customer could not be created or matched")
    now=utcnow()
    with connect() as conn:
        conn.execute("INSERT INTO integration_entity_links(id,organization_id,provider,entity_type,entity_id,external_id,external_type,last_synced_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?, ?,?,?) ON CONFLICT(organization_id,provider,entity_type,entity_id) DO UPDATE SET external_id=excluded.external_id,last_synced_at=excluded.last_synced_at,updated_at=excluded.updated_at",(new_id("link"),org,"QUICKBOOKS","customer",customer["id"],external,"Customer",now,now,now))
    return external


class QboInvoicePush(BaseModel):
    command_id: str

@router.post("/quickbooks/qbo/jobs/{job_id}/invoice")
def qbo_push_job_invoice(job_id: str, p: QboInvoicePush, request: Request):
    u=_user(request); _admin_or_manager(u); org=u["organization_id"]
    with connect() as conn:
        job=conn.execute("SELECT j.*,c.name customer_name,c.email,c.phone FROM jobs j JOIN customers c ON c.id=j.customer_id WHERE j.id=? AND j.organization_id=?",(job_id,org)).fetchone()
        if not job: raise HTTPException(404,"Job not found")
        estimate=conn.execute("SELECT * FROM estimates WHERE organization_id=? AND job_id=? AND status='APPROVED' ORDER BY revision DESC LIMIT 1",(org,job_id)).fetchone()
        existing=conn.execute("SELECT external_id FROM integration_entity_links WHERE organization_id=? AND provider='QUICKBOOKS' AND entity_type='job_invoice' AND entity_id=?",(org,job_id)).fetchone()
        if existing: return {"ok":True,"already_created":True,"quickbooks_invoice_id":existing["external_id"]}
    settings,_=_provider_row(org,"QUICKBOOKS"); item_id=str(settings.get("service_item_id") or "")
    if not item_id: raise HTTPException(400,"Choose the QuickBooks service item in Tools & Connections before creating invoices")
    amount_cents=int(estimate["total_cents"] if estimate else 0)
    if amount_cents <= 0: raise HTTPException(400,"An approved estimate with a positive total is required for this handoff")
    customer_id=_qbo_customer_for(org,dict(job))
    payload={"CustomerRef":{"value":customer_id},"DocNumber":job["job_number"],"PrivateNote":f"Created from ServiceSlate {job['job_number']}","Line":[{"Amount":round(amount_cents/100,2),"Description":job["description"],"DetailType":"SalesItemLineDetail","SalesItemLineDetail":{"ItemRef":{"value":item_id},"Qty":1,"UnitPrice":round(amount_cents/100,2)}}]}
    try: result=qbo_request(org,"POST","invoice",payload)
    except Exception as exc: raise HTTPException(400,str(exc)) from exc
    invoice=(result.get("Invoice") or {}); external=str(invoice.get("Id") or "")
    if not external: raise HTTPException(400,"QuickBooks did not return an invoice ID")
    now=utcnow()
    with connect() as conn:
        conn.execute("INSERT INTO integration_entity_links(id,organization_id,provider,entity_type,entity_id,external_id,external_type,external_json,last_synced_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(new_id("link"),org,"QUICKBOOKS","job_invoice",job_id,external,"Invoice",json.dumps({"DocNumber":invoice.get("DocNumber")}),now,now,now))
        audit(conn,org,u["id"],"job",job_id,"QUICKBOOKS_INVOICE_CREATED",f"QuickBooks invoice {invoice.get('DocNumber') or external}")
    return {"ok":True,"quickbooks_invoice_id":external,"doc_number":invoice.get("DocNumber")}


@router.get("/quickbooks/desktop/qwc")
def quickbooks_qwc(request: Request):
    u = _user(request); _admin_or_manager(u)
    settings, secrets_map = _provider_row(u["organization_id"], "QUICKBOOKS")
    if str(settings.get("mode") or "").upper() != "DESKTOP_WEB_CONNECTOR":
        raise HTTPException(409, "Choose QuickBooks Desktop Web Connector in Tools & Connections first")
    public = str(settings.get("web_connector_url") or "").strip().rstrip("/")
    username = str(settings.get("web_connector_username") or "serviceslate").strip()
    password = secrets_map.get("web_connector_password", "")
    service_item = str(settings.get("desktop_service_item_name") or "").strip()
    if not public:
        raise HTTPException(400, "A hosted ServiceSlate URL is required before QuickBooks Desktop Web Connector can be activated")
    if not public.lower().startswith("https://"):
        raise HTTPException(400, "QuickBooks Desktop Web Connector requires the hosted ServiceSlate URL to use HTTPS")
    if not username or not password:
        raise HTTPException(400, "Set a Web Connector username and password first")
    if not service_item:
        raise HTTPException(400, "Choose the QuickBooks Desktop service item name first")
    app_id = u["organization_id"]
    qwc = (
        '<?xml version="1.0"?>\n'
        '<QBWCXML>'
        '<AppName>ServiceSlate</AppName>'
        f'<AppID>{xml_escape(app_id)}</AppID>'
        f'<AppURL>{xml_escape(public)}/api/cloud/quickbooks/desktop/soap</AppURL>'
        '<AppDescription>ServiceSlate human-approved operational-to-accounting handoff</AppDescription>'
        f'<AppSupport>{xml_escape(public)}</AppSupport>'
        f'<UserName>{xml_escape(username)}</UserName>'
        '<OwnerID>{90A44FB7-33D9-4815-AC85-AC86A7E7D1EB}</OwnerID>'
        '<FileID>{57F3B9B1-86F1-4FCC-B1EE-566DE1813D20}</FileID>'
        '<QBType>QBFS</QBType>'
        '<Scheduler><RunEveryNMinutes>15</RunEveryNMinutes></Scheduler>'
        '</QBWCXML>'
    )
    from fastapi.responses import Response
    return Response(qwc, media_type="application/xml", headers={"Content-Disposition": "attachment; filename=ServiceSlate.qwc"})


class DesktopInvoiceQueue(BaseModel):
    confirm: bool = False


def _desktop_invoice_qbxml(org: str, job_id: str, service_item_name: str) -> tuple[str, str]:
    with connect() as conn:
        job = conn.execute(
            """SELECT j.*, c.name customer_name FROM jobs j
               JOIN customers c ON c.id=j.customer_id
               WHERE j.id=? AND j.organization_id=?""",
            (job_id, org),
        ).fetchone()
        if not job:
            raise HTTPException(404, "Job not found")
        estimate = conn.execute(
            "SELECT * FROM estimates WHERE organization_id=? AND job_id=? AND status='APPROVED' ORDER BY revision DESC LIMIT 1",
            (org, job_id),
        ).fetchone()
        if not estimate or int(estimate["total_cents"] or 0) <= 0:
            raise HTTPException(400, "An approved estimate with a positive total is required before sending an invoice to QuickBooks")
        lines = conn.execute(
            "SELECT * FROM estimate_lines WHERE estimate_id=? ORDER BY sort_order, id",
            (estimate["id"],),
        ).fetchall()
    line_xml = []
    for line in lines:
        amount = int(line["amount_cents"] or 0)
        if amount <= 0:
            continue
        line_xml.append(
            "<InvoiceLineAdd>"
            f"<ItemRef><FullName>{xml_escape(service_item_name)}</FullName></ItemRef>"
            f"<Desc>{xml_escape(str(line['description'] or job['description'] or 'Service'))}</Desc>"
            "<Quantity>1</Quantity>"
            f"<Rate>{amount / 100:.2f}</Rate>"
            "</InvoiceLineAdd>"
        )
    if not line_xml:
        total = int(estimate["total_cents"] or 0)
        line_xml.append(
            "<InvoiceLineAdd>"
            f"<ItemRef><FullName>{xml_escape(service_item_name)}</FullName></ItemRef>"
            f"<Desc>{xml_escape(str(job['description'] or 'Service'))}</Desc>"
            "<Quantity>1</Quantity>"
            f"<Rate>{total / 100:.2f}</Rate>"
            "</InvoiceLineAdd>"
        )
    qbxml = (
        '<?xml version="1.0"?>'
        '<?qbxml version="13.0"?>'
        '<QBXML><QBXMLMsgsRq onError="stopOnError">'
        f'<InvoiceAddRq requestID="{xml_escape(job_id)}"><InvoiceAdd>'
        f'<CustomerRef><FullName>{xml_escape(str(job["customer_name"]))}</FullName></CustomerRef>'
        f'<RefNumber>{xml_escape(str(job["job_number"]))}</RefNumber>'
        f'<Memo>ServiceSlate {xml_escape(str(job["job_number"]))}</Memo>'
        + "".join(line_xml)
        + '</InvoiceAdd></InvoiceAddRq></QBXMLMsgsRq></QBXML>'
    )
    return qbxml, str(estimate["id"])


@router.post("/quickbooks/desktop/jobs/{job_id}/queue-invoice")
def queue_quickbooks_desktop_invoice(job_id: str, p: DesktopInvoiceQueue, request: Request):
    u = _user(request); _admin_or_manager(u); org = u["organization_id"]
    if not p.confirm:
        raise HTTPException(400, "Confirm the QuickBooks invoice handoff first")
    settings, _ = _provider_row(org, "QUICKBOOKS")
    if str(settings.get("mode") or "").upper() != "DESKTOP_WEB_CONNECTOR":
        raise HTTPException(409, "QuickBooks Desktop Web Connector is not selected")
    service_item = str(settings.get("desktop_service_item_name") or "").strip()
    if not service_item:
        raise HTTPException(400, "Set the QuickBooks Desktop service item name first")
    with connect() as conn:
        linked = conn.execute(
            "SELECT external_id FROM integration_entity_links WHERE organization_id=? AND provider='QUICKBOOKS' AND entity_type='job_invoice' AND entity_id=?",
            (org, job_id),
        ).fetchone()
        if linked:
            return {"ok": True, "already_created": True, "quickbooks_invoice_id": linked["external_id"]}
        existing = conn.execute(
            "SELECT * FROM quickbooks_desktop_queue WHERE organization_id=? AND entity_type='job_invoice' AND entity_id=? AND status IN ('QUEUED','SENT') ORDER BY queued_at DESC LIMIT 1",
            (org, job_id),
        ).fetchone()
        if existing:
            return {"ok": True, "already_queued": True, "queue_id": existing["id"], "status": existing["status"]}
    qbxml, estimate_id = _desktop_invoice_qbxml(org, job_id, service_item)
    now = utcnow(); queue_id = new_id("qbq")
    with connect() as conn:
        conn.execute(
            """INSERT INTO quickbooks_desktop_queue(
                id,organization_id,entity_type,entity_id,request_kind,qbxml,status,queued_by_user_id,queued_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (queue_id, org, "job_invoice", job_id, "INVOICE_ADD", qbxml, "QUEUED", u["id"], now, now),
        )
        audit(conn, org, u["id"], "job", job_id, "QUICKBOOKS_DESKTOP_INVOICE_QUEUED", f"Approved estimate {estimate_id} queued for QuickBooks Desktop")
    return {"ok": True, "queue_id": queue_id, "status": "QUEUED"}


_QBWC_TICKETS: dict[str, str] = {}
_QBWC_LAST_ERROR: dict[str, str] = {}


def _soap_local(root: ET.Element, name: str) -> str:
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == name:
            return element.text or ""
    return ""


def _qbwc_authenticate(username: str, password: str) -> tuple[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT organization_id,settings_json FROM integration_status WHERE provider='QUICKBOOKS'").fetchall()
    for row in rows:
        try:
            settings = json.loads(row["settings_json"] or "{}")
        except Exception:
            continue
        if str(settings.get("mode") or "").upper() != "DESKTOP_WEB_CONNECTOR":
            continue
        configured_user = str(settings.get("web_connector_username") or "serviceslate")
        if not hmac.compare_digest(configured_user, username):
            continue
        expected = VAULT.get_provider(str(row["organization_id"]), "QUICKBOOKS").get("web_connector_password", "")
        if expected and hmac.compare_digest(expected, password):
            ticket = secrets.token_urlsafe(32)
            _QBWC_TICKETS[ticket] = str(row["organization_id"])
            return ticket, ""
    return "", "nvu"


def _soap_envelope(inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<soap:Body>{inner}</soap:Body></soap:Envelope>'
    )


def _soap_response(method: str, result_xml: str) -> str:
    return _soap_envelope(
        f'<{method}Response xmlns="http://developer.intuit.com/">'
        f'<{method}Result>{result_xml}</{method}Result>'
        f'</{method}Response>'
    )


def _qb_response_result(response_xml: str) -> tuple[bool, str | None, str | None]:
    try:
        root = ET.fromstring(response_xml)
    except ET.ParseError:
        return False, None, "QuickBooks returned unreadable XML"
    rs = None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].endswith("Rs"):
            rs = element
            break
    if rs is None:
        return False, None, "QuickBooks response did not contain a result"
    status_code = str(rs.attrib.get("statusCode") or "")
    status_message = str(rs.attrib.get("statusMessage") or "")
    if status_code not in ("", "0"):
        return False, None, status_message or f"QuickBooks error {status_code}"
    txn = None
    for element in rs.iter():
        if element.tag.rsplit("}", 1)[-1] == "TxnID" and element.text:
            txn = element.text.strip()
            break
    return True, txn, None


@router.post("/quickbooks/desktop/soap")
async def quickbooks_desktop_soap(request: Request):
    from fastapi.responses import Response
    try:
        raw = await request.body()
        root = ET.fromstring(raw)
    except ET.ParseError:
        return Response(_soap_envelope('<soap:Fault><faultcode>Client</faultcode><faultstring>Invalid XML</faultstring></soap:Fault>'), media_type="text/xml")
    body_method = None
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1]
        if local in {"serverVersion","clientVersion","authenticate","sendRequestXML","receiveResponseXML","getLastError","connectionError","closeConnection"}:
            body_method = local; break
    if not body_method:
        return Response(_soap_envelope('<soap:Fault><faultcode>Client</faultcode><faultstring>Unsupported Web Connector method</faultstring></soap:Fault>'), media_type="text/xml")

    if body_method == "serverVersion":
        xml = _soap_response(body_method, "0.9.0")
    elif body_method == "clientVersion":
        xml = _soap_response(body_method, "")
    elif body_method == "authenticate":
        username = _soap_local(root, "strUserName")
        password = _soap_local(root, "strPassword")
        ticket, company = _qbwc_authenticate(username, password)
        result = f'<string>{xml_escape(ticket)}</string><string>{xml_escape(company)}</string>'
        xml = _soap_response(body_method, result)
    else:
        ticket = _soap_local(root, "ticket")
        org = _QBWC_TICKETS.get(ticket)
        if not org:
            if body_method == "getLastError":
                xml = _soap_response(body_method, xml_escape(_QBWC_LAST_ERROR.get(ticket, "Web Connector session expired; authenticate again")))
            elif body_method == "closeConnection":
                xml = _soap_response(body_method, "Session already closed")
            else:
                xml = _soap_response(body_method, "")
        elif body_method == "sendRequestXML":
            with connect() as conn:
                row = conn.execute(
                    "SELECT * FROM quickbooks_desktop_queue WHERE organization_id=? AND status='QUEUED' ORDER BY queued_at,id LIMIT 1",
                    (org,),
                ).fetchone()
                if row:
                    conn.execute("UPDATE quickbooks_desktop_queue SET status='SENT',sent_at=?,updated_at=? WHERE id=?", (utcnow(), utcnow(), row["id"]))
            xml = _soap_response(body_method, xml_escape(str(row["qbxml"])) if row else "")
        elif body_method == "receiveResponseXML":
            response_xml = _soap_local(root, "response")
            hresult = _soap_local(root, "hresult")
            message = _soap_local(root, "message")
            with connect() as conn:
                row = conn.execute(
                    "SELECT * FROM quickbooks_desktop_queue WHERE organization_id=? AND status='SENT' ORDER BY sent_at,id LIMIT 1",
                    (org,),
                ).fetchone()
                if row:
                    ok, txn_id, error = _qb_response_result(response_xml) if not hresult else (False, None, message or hresult)
                    now = utcnow()
                    if ok:
                        conn.execute(
                            "UPDATE quickbooks_desktop_queue SET status='COMPLETED',external_id=?,response_xml=?,error=NULL,completed_at=?,updated_at=? WHERE id=?",
                            (txn_id, response_xml, now, now, row["id"]),
                        )
                        if txn_id:
                            conn.execute(
                                """INSERT INTO integration_entity_links(id,organization_id,provider,entity_type,entity_id,external_id,external_type,external_json,last_synced_at,created_at,updated_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                                   ON CONFLICT(organization_id,provider,entity_type,entity_id) DO UPDATE SET external_id=excluded.external_id,external_json=excluded.external_json,last_synced_at=excluded.last_synced_at,updated_at=excluded.updated_at""",
                                (new_id("link"),org,"QUICKBOOKS",row["entity_type"],row["entity_id"],txn_id,"Invoice",json.dumps({"source":"DESKTOP_WEB_CONNECTOR"}),now,now,now),
                            )
                        audit(conn, org, row["queued_by_user_id"], "job", row["entity_id"], "QUICKBOOKS_DESKTOP_INVOICE_CREATED", f"QuickBooks Desktop invoice {txn_id or 'accepted'}")
                    else:
                        error = error or "QuickBooks rejected the request"
                        conn.execute(
                            "UPDATE quickbooks_desktop_queue SET status='ERROR',response_xml=?,error=?,completed_at=?,updated_at=? WHERE id=?",
                            (response_xml, error, now, now, row["id"]),
                        )
                        _QBWC_LAST_ERROR[ticket] = error
                remaining = conn.execute("SELECT COUNT(*) AS n FROM quickbooks_desktop_queue WHERE organization_id=? AND status='QUEUED'", (org,)).fetchone()["n"]
            xml = _soap_response(body_method, "50" if remaining else "100")
        elif body_method == "getLastError":
            xml = _soap_response(body_method, xml_escape(_QBWC_LAST_ERROR.get(ticket, "")))
        elif body_method == "connectionError":
            msg = _soap_local(root, "message") or _soap_local(root, "hresult") or "QuickBooks connection error"
            _QBWC_LAST_ERROR[ticket] = msg
            xml = _soap_response(body_method, "done")
        elif body_method == "closeConnection":
            _QBWC_TICKETS.pop(ticket, None)
            _QBWC_LAST_ERROR.pop(ticket, None)
            xml = _soap_response(body_method, "ServiceSlate Web Connector session closed")
        else:
            xml = _soap_response(body_method, "")
    return Response(xml, media_type="text/xml; charset=utf-8")


# ---------------------------------------------------------------------------
# Public business enrichment (deterministic; no silent commits)
# ---------------------------------------------------------------------------

class EnrichmentRequest(BaseModel):
    business_name: str
    address: str | None = None
    website: str | None = None


def _website_public_facts(url:str)->dict[str,Any]:
    if not url: return {}
    if not url.startswith(("http://","https://")): url="https://"+url
    try:
        status,raw,_=_http_request(url,headers={"User-Agent":"ServiceSlate/0.9 business-enrichment"},timeout=10)
        if status>=300: return {}
        text=raw.decode("utf-8",errors="ignore")[:500000]
    except Exception: return {}
    import re
    title_match=re.search(r"<title[^>]*>(.*?)</title>",text,re.I|re.S)
    phones=re.findall(r"(?:tel:|telephone[^>]*[\"']?\s*[:=]\s*[\"'])(\+?[0-9().\-\s]{7,})",text,re.I)
    emails=re.findall(r"mailto:([^\"'?>\s]+)",text,re.I)
    return {"website":url,"page_title":re.sub(r"\s+"," ",title_match.group(1)).strip() if title_match else None,"phones":list(dict.fromkeys(x.strip() for x in phones))[:5],"emails":list(dict.fromkeys(emails))[:5]}


@router.post("/business-enrichment")
def business_enrichment(p:EnrichmentRequest,request:Request):
    u=_user(request)
    candidates=[]
    query=" ".join(x for x in (p.business_name,p.address) if x)
    try:
        settings,_=_provider_row(u["organization_id"],"NOMINATIM")
        base=str(settings.get("base_url") or "").rstrip("/")
        if base:
            params=urllib.parse.urlencode({"q":query,"format":"jsonv2","addressdetails":1,"limit":5})
            if base!="https://nominatim.openstreetmap.org" or bool(settings.get("allow_public_osm")):
                _,raw,_=_http_request(f"{base}/search?{params}",headers={"User-Agent":str(settings.get("user_agent") or "ServiceSlate/0.9")})
                for r in json.loads(raw or b"[]"):
                    candidates.append({"source":"OpenStreetMap/Nominatim","display_name":r.get("display_name"),"latitude":r.get("lat"),"longitude":r.get("lon"),"address":r.get("address"),"confidence":"candidate"})
    except Exception:
        pass
    website=_website_public_facts(p.website or "")
    return {"business_name":p.business_name,"candidates":candidates,"website_facts":website,"requires_human_review":True,"auto_commit":False}


# ---------------------------------------------------------------------------
# Production/readiness helpers
# ---------------------------------------------------------------------------

@router.get("/readiness")
def production_readiness(request:Request):
    u=_user(request)
    with connect() as conn:
        org=conn.execute("SELECT production_mode,public_base_url FROM organizations WHERE id=?",(u["organization_id"],)).fetchone()
        integrations={r["provider"]:r["state"] for r in conn.execute("SELECT provider,state FROM integration_status WHERE organization_id=?",(u["organization_id"],)).fetchall()}
        legal=conn.execute("SELECT COUNT(*) FROM legal_approvals WHERE organization_id=?",(u["organization_id"],)).fetchone()[0]
        usability=conn.execute("SELECT COUNT(*) FROM usability_sessions WHERE organization_id=? AND completed_at IS NOT NULL",(u["organization_id"],)).fetchone()[0]
        devices=conn.execute("SELECT COUNT(*) FROM device_readiness_reports WHERE organization_id=? AND status='PASS'",(u["organization_id"],)).fetchone()[0]
        active_users=conn.execute("SELECT COUNT(*) FROM users WHERE organization_id=? AND active=1",(u["organization_id"],)).fetchone()[0]
        mfa_users=conn.execute("SELECT COUNT(*) FROM users WHERE organization_id=? AND active=1 AND mfa_enabled=1",(u["organization_id"],)).fetchone()[0]
    public_ready=bool(org and org["public_base_url"] and str(org["public_base_url"]).startswith("https://"))
    checks=[
        {"id":"database","label":"Shared PostgreSQL database","state":"PASS" if database_backend()=="postgresql" else "READY_TO_DEPLOY","detail":database_backend()},
        {"id":"hosting","label":"Always-available shared host","state":"PASS" if bool(org and org["production_mode"]) and public_ready else "NEEDS_HOST_ACCOUNT","detail":"Docker/Caddy/PostgreSQL deployment files included"},
        {"id":"public_url","label":"Public HTTPS address / customer links","state":"PASS" if public_ready else "NEEDS_HOST_ACCOUNT"},
        {"id":"drive","label":"Google Drive documents + off-site backup","state":"PASS" if integrations.get("GOOGLE_DRIVE")=="CONNECTED" else "READY_TO_CONNECT"},
        {"id":"object_storage","label":"Production object-storage option","state":"PASS" if integrations.get("S3")=="CONNECTED" or bool(os.environ.get("SERVICESLATE_DEFAULT_S3_BUCKET")) else "READY_TO_CONNECT","detail":"S3/MinIO adapter available; Google Drive remains company document mirror"},
        {"id":"microsoft","label":"Microsoft Graph / New Outlook","state":"PASS" if integrations.get("MICROSOFT_GRAPH")=="CONNECTED" else "READY_TO_CONNECT"},
        {"id":"identity","label":"Production identity + MFA/passkey policy","state":"PASS" if integrations.get("MICROSOFT_GRAPH")=="CONNECTED" else ("PARTIAL" if mfa_users else "READY_TO_ENABLE"),"detail":f"Authenticator MFA: {mfa_users}/{active_users} active users. Company Microsoft sign-in can inherit Entra MFA/passkey policy when connected."},
        {"id":"calendar","label":"Outlook two-way calendar review","state":"PASS" if integrations.get("MICROSOFT_GRAPH")=="CONNECTED" else "READY_TO_CONNECT"},
        {"id":"web_security","label":"Hosted web security controls","state":"PASS" if bool(org and org["production_mode"]) and public_ready else "BUILT_IN_NOT_ACTIVE","detail":"Secure cookies, CSRF, rate limits, trusted hosts, HTTPS/HSTS/CSP and upload scanning activate with production hosting."},
        {"id":"operations","label":"Production operations automation","state":"READY_TO_DEPLOY","detail":"CI quality/security scans, structured logs, backup/restore drill, load-smoke script and hosted restore runbook are included; external alert/off-site destinations still need company configuration."},
        {"id":"sms","label":"Automated SMS + delivery receipts","state":"PASS" if integrations.get("TWILIO_SMS")=="CONNECTED" else "OPTIONAL","detail":"Carrier-phone handoff remains available without a paid provider"},
        {"id":"quickbooks","label":"QuickBooks live accounting adapter","state":"PASS" if integrations.get("QUICKBOOKS")=="CONNECTED" else "READY_OR_PERMISSION_REQUIRED"},
        {"id":"enrichment","label":"Public business enrichment","state":"PASS" if integrations.get("NOMINATIM")=="CONNECTED" else "BUILT_IN_REVIEW_ONLY"},
        {"id":"legal","label":"Business/legal approval of customer wording","state":"PASS" if legal else "NEEDS_HUMAN"},
        {"id":"usability","label":"Zero-training user test","state":"PASS" if usability else "NEEDS_HUMAN"},
        {"id":"devices","label":"Real phone/tablet field test","state":"PASS" if devices else "NEEDS_HUMAN"},
        {"id":"native_mobile","label":"Native mobile app","state":"NOT_REQUIRED" if not devices else "REVIEW_AFTER_FIELD_TEST","detail":"Build only if browser/PWA testing proves a real limitation"},
        {"id":"signed_installer","label":"Signed Windows installer + update/rollback channel","state":"NEEDS_EXTERNAL_CERTIFICATE","detail":"Standalone EXE/Setup build and verified update channel are implemented; Authenticode signing requires the company code-signing certificate and published HTTPS release manifest."},
    ]
    return {"production_mode":bool(org and org["production_mode"]),"checks":checks}


class LegalApproval(BaseModel):
    content_type:str
    content_id:str
    content_version:str="1"
    wording:str
    note:str|None=None


@router.post("/legal-approvals")
def legal_approval(p:LegalApproval,request:Request):
    u=_user(request); _admin_or_manager(u)
    digest=hashlib.sha256(p.wording.encode()).hexdigest(); now=utcnow()
    with connect() as conn:
        conn.execute("INSERT INTO legal_approvals(id,organization_id,content_type,content_id,content_version,wording_hash,approved_by_user_id,approved_at,note) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(organization_id,content_type,content_id,content_version) DO UPDATE SET wording_hash=excluded.wording_hash,approved_by_user_id=excluded.approved_by_user_id,approved_at=excluded.approved_at,note=excluded.note",
                     (new_id("legal"),u["organization_id"],p.content_type,p.content_id,p.content_version,digest,u["id"],now,p.note))
        audit(conn,u["organization_id"],u["id"],"legal_content",p.content_id,"BUSINESS_APPROVED",f"Approved {p.content_type} wording version {p.content_version}")
    return {"ok":True,"wording_hash":digest,"approved_by":u["name"],"approved_at":now}


class UsabilitySessionCreate(BaseModel):
    tester_role: str
    device_label: str | None = None
    task_results: list[dict[str, Any]] = []
    friction_notes: str | None = None
    completed: bool = True

@router.post("/readiness/usability")
def record_usability_session(p: UsabilitySessionCreate, request: Request):
    u=_user(request); _admin_or_manager(u); now=utcnow(); sid=new_id("ux")
    completed_at=now if p.completed else None
    with connect() as conn:
        conn.execute("INSERT INTO usability_sessions(id,organization_id,tester_role,device_label,started_at,completed_at,task_results_json,friction_notes,created_by_user_id) VALUES(?,?,?,?,?,?,?,?,?)",(sid,u["organization_id"],p.tester_role,p.device_label,now,completed_at,json.dumps(p.task_results),p.friction_notes,u["id"]))
        audit(conn,u["organization_id"],u["id"],"usability_session",sid,"RECORDED",f"Zero-training test for {p.tester_role}")
    return {"id":sid,"completed_at":completed_at}


class DeviceReadinessCreate(BaseModel):
    device_label: str
    browser: str | None = None
    capabilities: dict[str, Any] = {}
    offline_tested: bool = False
    reconnect_tested: bool = False
    camera_tested: bool = False
    notes: str | None = None

@router.post("/readiness/device")
def record_device_readiness(p: DeviceReadinessCreate, request: Request):
    u=_user(request); now=utcnow(); rid=new_id("device")
    passed=bool(p.offline_tested and p.reconnect_tested and p.camera_tested)
    status="PASS" if passed else "NEEDS_TESTING"
    with connect() as conn:
        conn.execute("INSERT INTO device_readiness_reports(id,organization_id,user_id,device_label,browser,capabilities_json,offline_tested,reconnect_tested,camera_tested,status,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(rid,u["organization_id"],u["id"],p.device_label,p.browser,json.dumps(p.capabilities),int(p.offline_tested),int(p.reconnect_tested),int(p.camera_tested),status,p.notes,now,now))
        audit(conn,u["organization_id"],u["id"],"device_readiness",rid,"RECORDED",f"{p.device_label}: {status}")
    return {"id":rid,"status":status}


@router.get("/readiness/evidence")
def readiness_evidence(request: Request):
    u=_user(request); _admin_or_manager(u)
    with connect() as conn:
        usability=[dict(r) for r in conn.execute("SELECT * FROM usability_sessions WHERE organization_id=? ORDER BY started_at DESC LIMIT 20",(u["organization_id"],)).fetchall()]
        devices=[dict(r) for r in conn.execute("SELECT * FROM device_readiness_reports WHERE organization_id=? ORDER BY created_at DESC LIMIT 20",(u["organization_id"],)).fetchall()]
        legal=[dict(r) for r in conn.execute("SELECT la.*,usr.name approved_by_name FROM legal_approvals la JOIN users usr ON usr.id=la.approved_by_user_id WHERE la.organization_id=? ORDER BY la.approved_at DESC",(u["organization_id"],)).fetchall()]
    return {"usability":usability,"devices":devices,"legal":legal}

class ProductionSettings(BaseModel):
    public_base_url: str | None = None
    backup_retention_days: int = 30
    production_mode: bool = False


@router.put("/readiness/settings")
def update_production_settings(p: ProductionSettings, request: Request):
    u=_user(request); _admin_or_manager(u)
    url=(p.public_base_url or "").strip().rstrip("/") or None
    if url and not url.startswith("https://"):
        raise HTTPException(400,"Production public address must use HTTPS")
    days=min(365,max(7,int(p.backup_retention_days)))
    now=utcnow()
    with connect() as conn:
        conn.execute("UPDATE organizations SET public_base_url=?,backup_retention_days=?,production_mode=? WHERE id=?",(url,days,int(p.production_mode),u["organization_id"]))
        audit(conn,u["organization_id"],u["id"],"organization",u["organization_id"],"PRODUCTION_SETTINGS_UPDATED",f"Public address {'configured' if url else 'not configured'}; backup retention {days} days")
    return {"ok":True,"public_base_url":url,"backup_retention_days":days,"production_mode":p.production_mode}
