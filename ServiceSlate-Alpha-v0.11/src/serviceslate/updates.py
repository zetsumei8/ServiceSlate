from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .db import DATA_DIR, audit, connect, create_backup_archive, utcnow

router = APIRouter(prefix="/api/updates")
CURRENT_VERSION = "0.9.0"
UPDATES_DIR = DATA_DIR / "updates"
UPDATES_DIR.mkdir(parents=True, exist_ok=True)


def _user(request: Request) -> dict[str, Any]:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Sign in required")
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    if not row:
        raise HTTPException(401, "Sign in required")
    return dict(row)


def _admin(user: dict[str, Any]) -> None:
    if user["role"] not in ("ADMIN", "MANAGER"):
        raise HTTPException(403, "Administrator or manager permission required")


def _version_tuple(raw: str) -> tuple[int, ...]:
    out = []
    for part in raw.strip().lstrip("v").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits or 0))
    return tuple(out)


def _manifest_url() -> str:
    return os.environ.get("SERVICESLATE_UPDATE_MANIFEST_URL", "").strip()


def fetch_manifest() -> dict[str, Any] | None:
    url = _manifest_url()
    if not url:
        return None
    if os.environ.get("SERVICESLATE_PRODUCTION_MODE", "0") == "1" and not url.startswith("https://"):
        raise RuntimeError("Production update manifests must use HTTPS")
    req = urllib.request.Request(url, headers={"User-Agent": f"ServiceSlate/{CURRENT_VERSION}"})
    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("version") or not data.get("sha256") or not (data.get("installer_url") or data.get("installer")):
        raise RuntimeError("Update manifest is incomplete")
    installer_ref = str(data.get("installer_url") or data.get("installer"))
    data["installer_url"] = urllib.parse.urljoin(url, installer_ref)
    if data.get("rollback_url"):
        data["rollback_url"] = urllib.parse.urljoin(url, str(data["rollback_url"]))
    return data


@router.get("/status")
def update_status(request: Request):
    u = _user(request)
    try:
        manifest = fetch_manifest()
    except Exception as exc:
        return {"configured": True, "current_version": CURRENT_VERSION, "state": "NEEDS_ATTENTION", "detail": str(exc)}
    if not manifest:
        return {"configured": False, "current_version": CURRENT_VERSION, "state": "NOT_CONFIGURED", "detail": "A release channel can be connected when the company is ready."}
    available = _version_tuple(str(manifest["version"])) > _version_tuple(CURRENT_VERSION)
    history_path = UPDATES_DIR / "history.jsonl"
    rollback_available = False
    if history_path.exists():
        for line in reversed(history_path.read_text(encoding="utf-8").splitlines()):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("rollback_installer") and Path(str(row["rollback_installer"])).exists():
                rollback_available = True
                break
    return {
        "configured": True,
        "current_version": CURRENT_VERSION,
        "latest_version": manifest["version"],
        "available": available,
        "state": "UPDATE_AVAILABLE" if available else "CURRENT",
        "notes": manifest.get("notes"),
        "windows_install_supported": platform.system() == "Windows",
        "signed_required": os.environ.get("SERVICESLATE_ALLOW_UNSIGNED_UPDATES", "0") != "1",
        "role_can_install": u["role"] in ("ADMIN", "MANAGER"),
        "rollback_available": rollback_available,
    }


def _download_verified(url: str, expected_sha256: str, filename: str) -> Path:
    if os.environ.get("SERVICESLATE_PRODUCTION_MODE", "0") == "1" and not url.startswith("https://"):
        raise RuntimeError("Production installers must use HTTPS")
    dest = UPDATES_DIR / filename
    req = urllib.request.Request(url, headers={"User-Agent": f"ServiceSlate/{CURRENT_VERSION}"})
    with urllib.request.urlopen(req, timeout=120) as response, dest.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest().lower()
    if digest != str(expected_sha256).lower():
        dest.unlink(missing_ok=True)
        raise RuntimeError("Downloaded installer failed its SHA-256 verification")
    return dest


def _download_installer(manifest: dict[str, Any]) -> Path:
    url = str(manifest["installer_url"])
    version = str(manifest["version"])
    return _download_verified(url, str(manifest["sha256"]), f"ServiceSlate-Setup-{version}.exe")


def _authenticode_valid(path: Path) -> bool:
    if platform.system() != "Windows":
        return False
    escaped = str(path).replace("'", "''")
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        f"$s=Get-AuthenticodeSignature -LiteralPath '{escaped}'; if($s.Status -eq 'Valid'){{exit 0}} else{{Write-Output $s.Status; exit 2}}",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False).returncode == 0


class InstallUpdate(BaseModel):
    confirm: bool = False


@router.post("/install")
def install_update(p: InstallUpdate, request: Request):
    u = _user(request); _admin(u)
    if not p.confirm:
        raise HTTPException(400, "Confirm the update before installing")
    if platform.system() != "Windows":
        raise HTTPException(409, "The automatic installer is available on Windows. Use the normal hosted deployment process on servers.")
    try:
        manifest = fetch_manifest()
        if not manifest:
            raise RuntimeError("No update channel is configured")
        if _version_tuple(str(manifest["version"])) <= _version_tuple(CURRENT_VERSION):
            return {"ok": True, "state": "CURRENT", "version": CURRENT_VERSION}
        installer = _download_installer(manifest)
        if os.environ.get("SERVICESLATE_ALLOW_UNSIGNED_UPDATES", "0") != "1" and not _authenticode_valid(installer):
            raise RuntimeError("The update is not signed by a trusted Windows publisher")
        safety_backup = create_backup_archive(f"BeforeUpdate-{CURRENT_VERSION}")
        rollback_installer = None
        if manifest.get("rollback_url") and manifest.get("rollback_sha256"):
            rollback_installer = _download_verified(str(manifest["rollback_url"]), str(manifest["rollback_sha256"]), f"ServiceSlate-Rollback-{CURRENT_VERSION}.exe")
            if os.environ.get("SERVICESLATE_ALLOW_UNSIGNED_UPDATES", "0") != "1" and not _authenticode_valid(rollback_installer):
                rollback_installer.unlink(missing_ok=True)
                raise RuntimeError("The rollback package is not signed by a trusted Windows publisher")
        history = UPDATES_DIR / "history.jsonl"
        with history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"from": CURRENT_VERSION, "to": manifest["version"], "installer": str(installer), "rollback_installer": str(rollback_installer) if rollback_installer else None, "backup": str(safety_backup), "at": utcnow()}) + "\n")
        with connect() as conn:
            audit(conn, u["organization_id"], u["id"], "application_update", str(manifest["version"]), "UPDATE_LAUNCHED", f"Verified update from {CURRENT_VERSION}; safety backup {safety_backup.name}")
        subprocess.Popen([str(installer), "/S"], close_fds=True)
        return {"ok": True, "state": "INSTALLER_LAUNCHED", "version": manifest["version"], "safety_backup": safety_backup.name}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


class RollbackUpdate(BaseModel):
    confirm: bool = False


@router.post("/rollback")
def rollback_update(p: RollbackUpdate, request: Request):
    u = _user(request); _admin(u)
    if not p.confirm:
        raise HTTPException(400, "Confirm the application rollback first")
    if platform.system() != "Windows":
        raise HTTPException(409, "Application rollback through the installer is available on Windows only")
    history = UPDATES_DIR / "history.jsonl"
    if not history.exists():
        raise HTTPException(409, "No verified rollback package is available on this installation")
    rows = []
    for line in history.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    candidate = next((row for row in reversed(rows) if row.get("rollback_installer") and Path(str(row["rollback_installer"])).exists()), None)
    if not candidate:
        raise HTTPException(409, "The release channel has not supplied a verified previous-version installer for rollback")
    installer = Path(str(candidate["rollback_installer"]))
    if os.environ.get("SERVICESLATE_ALLOW_UNSIGNED_UPDATES", "0") != "1" and not _authenticode_valid(installer):
        raise HTTPException(400, "The rollback installer is not signed by a trusted Windows publisher")
    safety_backup = create_backup_archive(f"BeforeRollback-{CURRENT_VERSION}")
    with connect() as conn:
        audit(conn, u["organization_id"], u["id"], "application_update", str(candidate.get("from") or "previous"), "ROLLBACK_LAUNCHED", f"Verified rollback launched; safety backup {safety_backup.name}")
    subprocess.Popen([str(installer), "/S"], close_fds=True)
    return {"ok": True, "state": "ROLLBACK_INSTALLER_LAUNCHED", "target_version": candidate.get("from"), "safety_backup": safety_backup.name}


@router.get("/history")
def update_history(request: Request):
    u = _user(request); _admin(u)
    history = UPDATES_DIR / "history.jsonl"
    if not history.exists():
        return []
    rows = []
    for line in history.read_text(encoding="utf-8").splitlines()[-20:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))
