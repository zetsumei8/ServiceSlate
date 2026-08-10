from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .db import audit, connect, new_id, utcnow
from .features import user_for

router = APIRouter(prefix="/api/lan", tags=["office-network"])


def _lan_mode() -> str:
    mode = os.environ.get("SERVICESLATE_LAN_ROLE", "local").strip().lower()
    return mode if mode in {"local", "host", "client"} else "local"


def _best_lan_ip() -> str | None:
    # No packet is transmitted. UDP connect only asks the OS which interface it
    # would use, which avoids binding ServiceSlate to a guessed adapter.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        sock.close()


class HeartbeatIn(BaseModel):
    device_id: str = Field(min_length=4, max_length=120)
    device_label: str = Field(default="ServiceSlate workstation", max_length=160)


class MessageIn(BaseModel):
    recipient_user_id: str | None = None
    body: str = Field(min_length=1, max_length=4000)
    linked_entity_type: str | None = Field(default=None, max_length=80)
    linked_entity_id: str | None = Field(default=None, max_length=160)


@router.get("/status")
def lan_status(request: Request) -> dict[str, Any]:
    user = user_for(request)
    port = int(os.environ.get("SERVICESLATE_PORT", "8765"))
    ip = _best_lan_ip()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    with connect() as conn:
        online = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM lan_presence WHERE organization_id=? AND last_seen_at>=?",
            (user["organization_id"], cutoff),
        ).fetchone()[0]
        unread = conn.execute(
            """SELECT COUNT(*) FROM internal_messages
               WHERE organization_id=? AND recipient_user_id=? AND read_at IS NULL AND archived_at IS NULL""",
            (user["organization_id"], user["id"]),
        ).fetchone()[0]
    hostname = socket.gethostname().strip()
    return {
        "mode": _lan_mode(),
        "sharing_active": _lan_mode() == "host",
        "lan_url": f"http://{hostname}:{port}" if hostname else (f"http://{ip}:{port}" if ip else None),
        "lan_ip_url": f"http://{ip}:{port}" if ip else None,
        "hostname": hostname or None,
        "port": port,
        "online_users": online,
        "unread_messages": unread,
        "trusted_network_only": True,
    }


@router.post("/heartbeat")
def heartbeat(payload: HeartbeatIn, request: Request):
    user = user_for(request)
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """INSERT INTO lan_presence(organization_id,user_id,device_id,device_label,last_seen_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(organization_id,user_id,device_id)
               DO UPDATE SET device_label=excluded.device_label,last_seen_at=excluded.last_seen_at""",
            (user["organization_id"], user["id"], payload.device_id, payload.device_label.strip(), now),
        )
        # Trim stale presence rows instead of growing indefinitely.
        stale = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        conn.execute("DELETE FROM lan_presence WHERE organization_id=? AND last_seen_at<?", (user["organization_id"], stale))
    return {"ok": True, "at": now}


@router.get("/presence")
def presence(request: Request):
    user = user_for(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.user_id,u.name,u.role,MAX(p.last_seen_at) last_seen_at,
                      MAX(CASE WHEN p.last_seen_at>=? THEN 1 ELSE 0 END) online
               FROM lan_presence p JOIN users u ON u.id=p.user_id
               WHERE p.organization_id=? AND u.active=1
               GROUP BY p.user_id,u.name,u.role ORDER BY online DESC,u.name""",
            (cutoff, user["organization_id"]),
        ).fetchall()
        # Users who have never opened the LAN-aware build should still be messageable.
        known = {r["user_id"] for r in rows}
        all_users = conn.execute(
            "SELECT id user_id,name,role FROM users WHERE organization_id=? AND active=1 ORDER BY name",
            (user["organization_id"],),
        ).fetchall()
    result = [dict(r) for r in rows]
    result.extend({**dict(r), "last_seen_at": None, "online": 0} for r in all_users if r["user_id"] not in known)
    return result


@router.get("/messages")
def messages(request: Request, limit: int = 100):
    user = user_for(request)
    limit = max(1, min(limit, 250))
    with connect() as conn:
        rows = conn.execute(
            """SELECT m.*,s.name sender_name,r.name recipient_name
               FROM internal_messages m
               JOIN users s ON s.id=m.sender_user_id
               LEFT JOIN users r ON r.id=m.recipient_user_id
               WHERE m.organization_id=? AND m.archived_at IS NULL
                 AND (m.recipient_user_id IS NULL OR m.recipient_user_id=? OR m.sender_user_id=?)
               ORDER BY m.created_at DESC LIMIT ?""",
            (user["organization_id"], user["id"], user["id"], limit),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/messages")
def send_message(payload: MessageIn, request: Request):
    user = user_for(request)
    body = payload.body.strip()
    if not body:
        raise HTTPException(400, "Message cannot be blank")
    if payload.recipient_user_id == user["id"]:
        raise HTTPException(400, "Choose another teammate or send to everyone")
    now = utcnow()
    mid = new_id("imsg")
    with connect() as conn:
        if payload.recipient_user_id:
            target = conn.execute(
                "SELECT id FROM users WHERE id=? AND organization_id=? AND active=1",
                (payload.recipient_user_id, user["organization_id"]),
            ).fetchone()
            if not target:
                raise HTTPException(404, "Teammate not found")
        conn.execute(
            """INSERT INTO internal_messages(id,organization_id,sender_user_id,recipient_user_id,body,
               linked_entity_type,linked_entity_id,created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (mid, user["organization_id"], user["id"], payload.recipient_user_id, body,
             payload.linked_entity_type, payload.linked_entity_id, now),
        )
        audit(conn, user["organization_id"], user["id"], "internal_message", mid, "SENT",
              "Internal ServiceSlate message sent", after={"recipient_user_id": payload.recipient_user_id, "linked_entity_type": payload.linked_entity_type, "linked_entity_id": payload.linked_entity_id})
    return {"id": mid, "created_at": now}


@router.post("/messages/{message_id}/read")
def mark_read(message_id: str, request: Request):
    user = user_for(request)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM internal_messages WHERE id=? AND organization_id=?",
            (message_id, user["organization_id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Message not found")
        if row["recipient_user_id"] not in (None, user["id"]):
            raise HTTPException(403, "This message is not addressed to you")
        conn.execute("UPDATE internal_messages SET read_at=COALESCE(read_at,?) WHERE id=?", (utcnow(), message_id))
    return {"ok": True}
