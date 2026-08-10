from __future__ import annotations

import json
import os
import secrets
import base64
import hashlib
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .db import audit, connect, hash_password, utcnow, verify_password
from .integrations import VAULT, _http_request, _provider_row

router = APIRouter(prefix="/api/security")


def _user(request: Request) -> dict[str, Any]:
    uid=request.session.get("user_id")
    if not uid: raise HTTPException(401,"Sign in required")
    with connect() as conn:
        row=conn.execute("SELECT * FROM users WHERE id=? AND active=1",(uid,)).fetchone()
    if not row: raise HTTPException(401,"Sign in required")
    return dict(row)


def _mfa_secret(org:str,user_id:str)->str|None:
    return VAULT.get_provider(org,f"MFA_{user_id}").get("totp_secret")


def verify_totp(org:str,user_id:str,code:str)->bool:
    secret=_mfa_secret(org,user_id)
    if not secret: return False
    try:
        import pyotp
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("MFA support requires pyotp") from exc
    return bool(pyotp.TOTP(secret).verify(code.strip(),valid_window=1))


def new_totp_secret()->str:
    try:
        import pyotp
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("MFA support requires pyotp") from exc
    return pyotp.random_base32()


@router.get("/status")
def security_status(request:Request):
    u=_user(request)
    return {
        "mfa_enabled": bool(u.get("mfa_enabled")),
        "password_changed_at": u.get("password_changed_at"),
        "last_login_at": u.get("last_login_at"),
        "force_password_change": bool(u.get("force_password_change")),
        "passkey_ready": False,
        "passkey_note": "Authenticator MFA is live. When company Microsoft sign-in is connected, Microsoft/Entra can enforce the organization’s MFA or passkey policy without ServiceSlate implementing its own cryptography.",
        "account_recovery": "Administrator-assisted reset is available; no security-question recovery is used.",
    }


@router.post("/mfa/setup")
def mfa_setup(request:Request):
    u=_user(request)
    secret=new_totp_secret(); VAULT.set_provider(u["organization_id"],f"MFA_{u['id']}",{"totp_secret":secret})
    try:
        import pyotp
        uri=pyotp.TOTP(secret).provisioning_uri(name=u["email"],issuer_name="ServiceSlate")
    except ImportError as exc: raise HTTPException(500,"MFA package unavailable") from exc
    return {"secret":secret,"otpauth_uri":uri,"message":"Add this account to an authenticator app, then enter a code to turn MFA on."}


class MfaEnable(BaseModel): code:str


@router.post("/mfa/enable")
def mfa_enable(p:MfaEnable,request:Request):
    u=_user(request)
    if not verify_totp(u["organization_id"],u["id"],p.code): raise HTTPException(400,"That authenticator code did not match")
    with connect() as conn:
        conn.execute("UPDATE users SET mfa_enabled=1 WHERE id=?",(u["id"],)); audit(conn,u["organization_id"],u["id"],"user",u["id"],"MFA_ENABLED","Enabled authenticator MFA")
    return {"ok":True}


@router.post("/mfa/disable")
def mfa_disable(p:MfaEnable,request:Request):
    u=_user(request)
    if not verify_totp(u["organization_id"],u["id"],p.code): raise HTTPException(400,"Authenticator code required")
    VAULT.clear_provider(u["organization_id"],f"MFA_{u['id']}")
    with connect() as conn:
        conn.execute("UPDATE users SET mfa_enabled=0 WHERE id=?",(u["id"],)); audit(conn,u["organization_id"],u["id"],"user",u["id"],"MFA_DISABLED","Disabled authenticator MFA")
    return {"ok":True}


class PasswordChange(BaseModel): current_password:str; new_password:str


@router.post("/password")
def change_password(p:PasswordChange,request:Request):
    u=_user(request)
    if not verify_password(p.current_password,u["password_hash"]): raise HTTPException(400,"Current password did not match")
    if len(p.new_password)<12: raise HTTPException(400,"Use at least 12 characters for a production password")
    now=utcnow()
    with connect() as conn:
        conn.execute("UPDATE users SET password_hash=?,password_changed_at=?,failed_login_count=0,locked_until=NULL,force_password_change=0 WHERE id=?",(hash_password(p.new_password),now,u["id"])); audit(conn,u["organization_id"],u["id"],"user",u["id"],"PASSWORD_CHANGED","Changed account password")
    return {"ok":True,"password_changed_at":now}


def note_login_failure(email:str)->None:
    now=datetime.now(timezone.utc)
    with connect() as conn:
        row=conn.execute("SELECT id,organization_id,failed_login_count FROM users WHERE lower(email)=lower(?)",(email.strip(),)).fetchone()
        if not row: return
        count=int(row["failed_login_count"] or 0)+1
        locked_until=(now+timedelta(minutes=15)).isoformat() if count>=5 else None
        conn.execute("UPDATE users SET failed_login_count=?,locked_until=? WHERE id=?",(count,locked_until,row["id"]))
        audit(conn,row["organization_id"],None,"user",row["id"],"LOGIN_FAILED",f"Failed login attempt {count}" + ("; account temporarily locked" if locked_until else ""))


def login_lock_state(row:Any)->tuple[bool,str|None]:
    raw=row["locked_until"] if row and "locked_until" in row.keys() else None
    if not raw: return False,None
    try:
        until=datetime.fromisoformat(raw.replace("Z","+00:00"))
        if until>datetime.now(timezone.utc): return True,raw
    except Exception: pass
    return False,None


def complete_login(user_id:str)->None:
    with connect() as conn:
        conn.execute("UPDATE users SET failed_login_count=0,locked_until=NULL,last_login_at=? WHERE id=?",(utcnow(),user_id))


class AdminPasswordReset(BaseModel):
    temporary_password: str
    require_change: bool = True


def _admin(request: Request) -> dict[str, Any]:
    u = _user(request)
    if u["role"] not in ("ADMIN", "MANAGER"):
        raise HTTPException(403, "Administrator or manager permission required")
    return u


@router.post("/users/{user_id}/unlock")
def admin_unlock_account(user_id: str, request: Request):
    actor = _admin(request)
    with connect() as conn:
        target = conn.execute("SELECT id,name FROM users WHERE id=? AND organization_id=?", (user_id, actor["organization_id"])).fetchone()
        if not target:
            raise HTTPException(404, "Account not found")
        conn.execute("UPDATE users SET failed_login_count=0,locked_until=NULL WHERE id=?", (user_id,))
        audit(conn, actor["organization_id"], actor["id"], "user", user_id, "ACCOUNT_UNLOCKED", f"Unlocked {target['name']}")
    return {"ok": True}


@router.post("/users/{user_id}/reset-password")
def admin_reset_password(user_id: str, p: AdminPasswordReset, request: Request):
    actor = _admin(request)
    if len(p.temporary_password) < 12:
        raise HTTPException(400, "Temporary password must be at least 12 characters")
    now = utcnow()
    with connect() as conn:
        target = conn.execute("SELECT id,name FROM users WHERE id=? AND organization_id=?", (user_id, actor["organization_id"])).fetchone()
        if not target:
            raise HTTPException(404, "Account not found")
        conn.execute(
            "UPDATE users SET password_hash=?,failed_login_count=0,locked_until=NULL,password_changed_at=?,force_password_change=? WHERE id=?",
            (hash_password(p.temporary_password), now, int(p.require_change), user_id),
        )
        audit(conn, actor["organization_id"], actor["id"], "user", user_id, "PASSWORD_RESET_BY_ADMIN", f"Reset {target['name']} password; change required: {p.require_change}")
    return {"ok": True, "force_password_change": p.require_change}


class MicrosoftSsoStart(BaseModel):
    email: str


def _microsoft_redirect_base(org: str, request: Request) -> str:
    settings, _ = _provider_row(org, "MICROSOFT_GRAPH")
    with connect() as conn:
        row = conn.execute("SELECT public_base_url FROM organizations WHERE id=?", (org,)).fetchone()
    base = str(settings.get("redirect_base_url") or (row["public_base_url"] if row else "") or request.base_url).rstrip("/")
    if os.environ.get("SERVICESLATE_PRODUCTION_MODE", "0") == "1" and not base.startswith("https://"):
        raise HTTPException(400, "Microsoft company sign-in requires the production HTTPS ServiceSlate address")
    return base


@router.post("/microsoft/start")
def microsoft_sso_start(p: MicrosoftSsoStart, request: Request):
    """Start Microsoft/Entra sign-in for an already-authorized ServiceSlate account.

    ServiceSlate never creates an account merely because Microsoft authenticated
    someone. The email must already map to an active ServiceSlate user.
    """
    with connect() as conn:
        row = conn.execute("SELECT id,organization_id,email FROM users WHERE lower(email)=lower(?) AND active=1", (p.email.strip(),)).fetchone()
    if not row:
        # Avoid becoming an account-discovery endpoint.
        raise HTTPException(400, "Microsoft sign-in is not available for that ServiceSlate account")
    settings, _ = _provider_row(row["organization_id"], "MICROSOFT_GRAPH")
    client_id = str(settings.get("client_id") or "").strip()
    tenant = str(settings.get("tenant_id") or "organizations").strip()
    if not client_id:
        raise HTTPException(400, "Microsoft company sign-in has not been connected by an administrator")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    redirect_uri = _microsoft_redirect_base(row["organization_id"], request) + "/api/security/microsoft/callback"
    request.session["microsoft_sso_state"] = state
    request.session["microsoft_sso_user_id"] = row["id"]
    request.session["microsoft_sso_verifier"] = verifier
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": "openid profile email User.Read",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return {"url": f"https://login.microsoftonline.com/{urllib.parse.quote(tenant, safe='')}/oauth2/v2.0/authorize?{urllib.parse.urlencode(params)}"}


@router.get("/microsoft/callback")
def microsoft_sso_callback(request: Request, code: str, state: str):
    expected_state = request.session.get("microsoft_sso_state")
    user_id = request.session.get("microsoft_sso_user_id")
    verifier = request.session.get("microsoft_sso_verifier")
    if not expected_state or not secrets.compare_digest(str(expected_state), str(state)) or not user_id or not verifier:
        raise HTTPException(400, "Microsoft sign-in session expired or did not match")
    with connect() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
    if not user:
        raise HTTPException(401, "ServiceSlate account is no longer active")
    settings, _ = _provider_row(user["organization_id"], "MICROSOFT_GRAPH")
    client_id = str(settings.get("client_id") or "").strip()
    tenant = str(settings.get("tenant_id") or "organizations").strip()
    redirect_uri = _microsoft_redirect_base(user["organization_id"], request) + "/api/security/microsoft/callback"
    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "scope": "openid profile email User.Read",
    }).encode()
    status, raw, _ = _http_request(
        f"https://login.microsoftonline.com/{urllib.parse.quote(tenant, safe='')}/oauth2/v2.0/token",
        method="POST",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status >= 300:
        raise HTTPException(401, "Microsoft sign-in could not be verified")
    token = json.loads(raw or b"{}").get("access_token")
    if not token:
        raise HTTPException(401, "Microsoft did not return a usable sign-in token")
    status, raw, _ = _http_request(
        "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName,displayName",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    if status >= 300:
        raise HTTPException(401, "Microsoft account identity could not be read")
    profile = json.loads(raw or b"{}")
    microsoft_email = str(profile.get("mail") or profile.get("userPrincipalName") or "").strip().lower()
    if not microsoft_email or microsoft_email != str(user["email"]).strip().lower():
        raise HTTPException(403, "Microsoft account does not match the authorized ServiceSlate user")
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["csrf_token"] = secrets.token_urlsafe(24)
    complete_login(user["id"])
    with connect() as conn:
        audit(conn, user["organization_id"], user["id"], "user", user["id"], "MICROSOFT_SSO_LOGIN", "Signed in through company Microsoft identity")
    return RedirectResponse(url="/", status_code=303)
