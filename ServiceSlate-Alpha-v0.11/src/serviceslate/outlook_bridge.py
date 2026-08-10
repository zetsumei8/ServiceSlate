from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .mail_intake import MailAttachment, MailEnvelope, ingest_mail_message, process_service_day_reply
from .db import connect, new_id, utcnow

OL_FOLDER_INBOX = 6
OL_MAIL_ITEM = 0


def _win32() -> tuple[Any, Any]:
    if os.name != "nt":
        raise RuntimeError("Classic Outlook connection is available only on Windows")
    try:
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Classic Outlook support is not installed in this ServiceSlate build") from exc
    return pythoncom, win32com.client


class _OutlookCom:
    def __enter__(self) -> Any:
        self.pythoncom, self.win32 = _win32()
        self.pythoncom.CoInitialize()
        return self.win32

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.pythoncom.CoUninitialize()


def status() -> dict[str, Any]:
    if os.name != "nt":
        return {"available": False, "reason": "Classic Outlook detection runs on Windows only"}
    try:
        with _OutlookCom() as win32:
            app = win32.Dispatch("Outlook.Application")
            namespace = app.GetNamespace("MAPI")
            accounts = []
            try:
                for account in namespace.Accounts:
                    accounts.append(str(account.SmtpAddress or account.DisplayName))
            except Exception:
                pass
            return {"available": True, "accounts": accounts, "mode": "classic_outlook"}
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:300]}


def _folder(namespace: Any, folder_name: str | None) -> Any:
    inbox = namespace.GetDefaultFolder(OL_FOLDER_INBOX)
    name = (folder_name or "Inbox").strip()
    if not name or name.lower() == "inbox":
        return inbox
    current = inbox
    # Permit a simple Inbox child path such as "FastField" or "FastField\\Processed".
    for part in [x.strip() for x in name.replace("/", "\\").split("\\") if x.strip() and x.lower() != "inbox"]:
        current = current.Folders.Item(part)
    return current


def _sender_address(item: Any) -> str | None:
    try:
        value = str(item.SenderEmailAddress or "").strip()
        if value and not value.startswith("/O="):
            return value
        sender = item.Sender
        if sender is not None:
            exchange = sender.GetExchangeUser()
            if exchange is not None and exchange.PrimarySmtpAddress:
                return str(exchange.PrimarySmtpAddress)
    except Exception:
        pass
    return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(value)


def _envelope(item: Any) -> MailEnvelope:
    attachments: list[MailAttachment] = []
    if getattr(item, "Attachments", None):
        with tempfile.TemporaryDirectory(prefix="serviceslate-outlook-") as td:
            for index in range(1, int(item.Attachments.Count) + 1):
                att = item.Attachments.Item(index)
                name = str(att.FileName or f"attachment-{index}.bin")
                target = Path(td) / Path(name).name
                att.SaveAsFile(str(target))
                data = target.read_bytes()
                attachments.append(MailAttachment(name=name, content_type=None, data=data))
    return MailEnvelope(
        external_id=str(item.EntryID),
        internet_message_id=str(getattr(item, "InternetMessageID", "") or "").strip() or None,
        sender=_sender_address(item),
        subject=str(item.Subject or ""),
        received_at=_iso(getattr(item, "ReceivedTime", None)),
        body=str(item.Body or ""),
        attachments=attachments,
    )


def scan(
    organization_id: str,
    actor_user_id: str | None,
    *,
    folder_name: str = "Inbox",
    fastfield_sender_filter: str = "",
    fastfield_subject_filter: str = "FastField",
    limit: int = 100,
) -> dict[str, Any]:
    folder_key = (folder_name or "Inbox").strip() or "Inbox"
    with connect() as conn:
        sync = conn.execute(
            "SELECT last_external_id FROM mail_sync_state WHERE organization_id=? AND provider='OUTLOOK' AND folder=?",
            (organization_id, folder_key),
        ).fetchone()
    last_external_id = sync["last_external_id"] if sync else None
    results: list[dict[str, Any]] = []
    scanned = 0
    newest_external_id: str | None = None
    sender_filter = fastfield_sender_filter.strip().lower()
    subject_filter = fastfield_subject_filter.strip().lower()
    with _OutlookCom() as win32:
        app = win32.Dispatch("Outlook.Application")
        namespace = app.GetNamespace("MAPI")
        folder = _folder(namespace, folder_name)
        items = folder.Items
        try:
            items.Sort("[ReceivedTime]", True)
        except Exception:
            pass
        for item in items:
            if scanned >= max(1, min(limit, 500)):
                break
            try:
                if int(getattr(item, "Class", 0)) != 43:
                    continue
                envelope = _envelope(item)
            except Exception as exc:
                scanned += 1
                results.append({"state": "SKIPPED", "reason": str(exc)[:200]})
                continue
            if newest_external_id is None:
                newest_external_id = envelope.external_id
            if last_external_id and envelope.external_id == last_external_id:
                break
            scanned += 1
            reply = process_service_day_reply(organization_id, envelope)
            if reply and reply.get("matched"):
                results.append({"type": "SERVICE_DAY_REPLY", **reply})
                continue
            sender_ok = not sender_filter or sender_filter in (envelope.sender or "").lower()
            subject_ok = not subject_filter or subject_filter in envelope.subject.lower()
            if sender_ok and subject_ok:
                imported = ingest_mail_message(organization_id, actor_user_id, "OUTLOOK", envelope)
                results.append({"type": "FASTFIELD", **imported})
    now = utcnow()
    if newest_external_id:
        with connect() as conn:
            conn.execute(
                """INSERT INTO mail_sync_state(id,organization_id,provider,folder,last_synced_at,last_external_id,created_at,updated_at)
                   VALUES(?,?,'OUTLOOK',?,?,?,?,?)
                   ON CONFLICT(organization_id,provider,folder) DO UPDATE SET
                   last_synced_at=excluded.last_synced_at,last_external_id=excluded.last_external_id,updated_at=excluded.updated_at""",
                (new_id("mail_sync"), organization_id, folder_key, now, newest_external_id, now, now),
            )
    return {
        "scanned": scanned,
        "handled": len(results),
        "confirmations": sum(1 for r in results if r.get("type") == "SERVICE_DAY_REPLY"),
        "fastfield": sum(1 for r in results if r.get("type") == "FASTFIELD"),
        "results": results,
    }


def send_email(
    *,
    to: str,
    subject: str,
    body: str,
    request_read_receipt: bool = True,
    request_delivery_receipt: bool = False,
) -> str:
    with _OutlookCom() as win32:
        app = win32.Dispatch("Outlook.Application")
        mail = app.CreateItem(OL_MAIL_ITEM)
        mail.To = to
        mail.Subject = subject
        mail.Body = body
        try:
            mail.ReadReceiptRequested = bool(request_read_receipt)
            mail.OriginatorDeliveryReportRequested = bool(request_delivery_receipt)
        except Exception:
            pass
        mail.Send()
        try:
            return str(mail.EntryID or "outlook-sent")
        except Exception:
            return "outlook-sent"
