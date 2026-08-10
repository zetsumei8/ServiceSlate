from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .db import audit, connect, new_id, utcnow

AssistanceMethod = Literal[
    "HUMAN_AUTHORED",
    "DETERMINISTIC_STRUCTURED",
    "DETERMINISTIC_RULE",
    "LOCAL_OCR",
    "AI_FALLBACK",
]

TRUSTED_WITHOUT_SECOND_REVIEW = {"HUMAN_AUTHORED"}
SOFTWARE_DERIVED = {"DETERMINISTIC_STRUCTURED", "DETERMINISTIC_RULE", "LOCAL_OCR", "AI_FALLBACK"}

# These are deliberately broad. ServiceSlate should err toward asking a human to own a
# consequential decision, not toward silently accepting a machine-derived one.
CONSEQUENTIAL_KINDS = {
    "CUSTOMER_COMMITMENT",
    "SCHEDULE_CHANGE",
    "TECHNICAL_FINDING",
    "SAFETY_FINDING",
    "INSPECTION_RESULT",
    "ESTIMATE",
    "AUTHORIZATION",
    "WARRANTY_DECISION",
    "JOB_COMPLETION",
    "JOB_CANCELLATION",
    "PERMANENT_HISTORY",
    "CUSTOMER_COMMUNICATION",
    "IMPORT_TO_TRUSTED_HISTORY",
}


@dataclass(frozen=True, slots=True)
class GovernancePolicy:
    deterministic_first: bool = True
    ai_enabled: bool = False
    ai_is_authority: bool = False
    human_review_for_software_derived: bool = True


def organization_policy(organization_id: str) -> GovernancePolicy:
    with connect() as conn:
        row = conn.execute(
            "SELECT deterministic_first,ai_enabled,ai_is_authority,human_review_for_software_derived "
            "FROM governance_policy WHERE organization_id=?",
            (organization_id,),
        ).fetchone()
    if not row:
        return GovernancePolicy()
    return GovernancePolicy(
        deterministic_first=bool(row["deterministic_first"]),
        ai_enabled=bool(row["ai_enabled"]),
        ai_is_authority=bool(row["ai_is_authority"]),
        human_review_for_software_derived=bool(row["human_review_for_software_derived"]),
    )


def requires_human_review(method: str, *, consequential: bool = True) -> bool:
    if not consequential:
        return method == "AI_FALLBACK"
    return method != "HUMAN_AUTHORED"


def create_suggestion(
    organization_id: str,
    *,
    kind: str,
    entity_type: str,
    entity_id: str | None,
    field_name: str | None,
    suggested_value: Any,
    method: AssistanceMethod,
    evidence: dict[str, Any] | None = None,
    created_by_user_id: str | None = None,
) -> str:
    """Persist a software-derived suggestion without granting it business authority."""
    suggestion_id = new_id("suggest")
    now = utcnow()
    status = "ACCEPTED" if method == "HUMAN_AUTHORED" else "AWAITING_HUMAN_REVIEW"
    with connect() as conn:
        conn.execute(
            """INSERT INTO assistance_suggestions(
                id,organization_id,kind,entity_type,entity_id,field_name,suggested_value_json,
                method,evidence_json,status,created_by_user_id,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                suggestion_id,
                organization_id,
                kind,
                entity_type,
                entity_id,
                field_name,
                json.dumps(suggested_value, ensure_ascii=False),
                method,
                json.dumps(evidence or {}, ensure_ascii=False),
                status,
                created_by_user_id,
                now,
                now,
            ),
        )
    return suggestion_id


def review_suggestion(
    organization_id: str,
    suggestion_id: str,
    reviewer_user_id: str,
    *,
    accept: bool,
    final_value: Any | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Record human responsibility for accepting/rejecting a suggestion.

    This function only establishes authorship/approval. Applying the accepted value to a
    consequential domain record remains the responsibility of the normal domain command.
    """
    now = utcnow()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM assistance_suggestions WHERE id=? AND organization_id=?",
            (suggestion_id, organization_id),
        ).fetchone()
        if not row:
            raise KeyError("Suggestion not found")
        if row["status"] in ("ACCEPTED", "REJECTED"):
            return dict(row)
        status = "ACCEPTED" if accept else "REJECTED"
        conn.execute(
            """UPDATE assistance_suggestions
               SET status=?,reviewed_by_user_id=?,reviewed_at=?,final_value_json=?,review_note=?,updated_at=?
               WHERE id=?""",
            (
                status,
                reviewer_user_id,
                now,
                json.dumps(final_value, ensure_ascii=False) if final_value is not None else None,
                note,
                now,
                suggestion_id,
            ),
        )
        audit(
            conn,
            organization_id,
            reviewer_user_id,
            "assistance_suggestion",
            suggestion_id,
            status,
            f"Human review of {row['method']} suggestion" + (f": {note}" if note else ""),
        )
        updated = conn.execute("SELECT * FROM assistance_suggestions WHERE id=?", (suggestion_id,)).fetchone()
    return dict(updated)


router = APIRouter(prefix="/api/governance")


def _request_user(request: Request) -> dict[str, Any]:
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Sign in required")
    with connect() as conn:
        row = conn.execute(
            """SELECT u.*,o.profile organization_profile FROM users u
               JOIN organizations o ON o.id=u.organization_id
               WHERE u.id=? AND u.active=1""",
            (uid,),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Sign in required")
    return dict(row)


@router.get("/policy")
def get_policy(request: Request) -> dict[str, Any]:
    user = _request_user(request)
    policy = organization_policy(user["organization_id"])
    return {
        "deterministic_first": policy.deterministic_first,
        "ai_enabled": policy.ai_enabled,
        "ai_is_authority": policy.ai_is_authority,
        "human_review_for_software_derived": policy.human_review_for_software_derived,
        "plain_language": {
            "primary": "Rules first",
            "ai": "AI off by default" if not policy.ai_enabled else "AI suggestions only",
            "authority": "A human owns important decisions",
        },
        "guarantees": [
            "Core scheduling, status, permissions, recurrence, billing readiness and history do not require AI.",
            "Structured data and deterministic rules are used before any ambiguous extraction method.",
            "Software-derived consequential data requires named human review before it becomes trusted history.",
            "AI can never be the final authority for a safety, financial, legal, customer-commitment or permanent-history decision.",
        ],
    }


@router.get("/suggestions")
def list_suggestions(request: Request, status: str = "AWAITING_HUMAN_REVIEW") -> list[dict[str, Any]]:
    user = _request_user(request)
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.*,u.name reviewed_by_name FROM assistance_suggestions s
               LEFT JOIN users u ON u.id=s.reviewed_by_user_id
               WHERE s.organization_id=? AND s.status=? ORDER BY s.created_at DESC LIMIT 200""",
            (user["organization_id"], status),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["suggested_value"] = json.loads(item.pop("suggested_value_json"))
        item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
        if item.get("final_value_json"):
            item["final_value"] = json.loads(item["final_value_json"])
        item.pop("final_value_json", None)
        result.append(item)
    return result


class SuggestionReview(BaseModel):
    accept: bool
    final_value: Any | None = None
    note: str | None = None


@router.post("/suggestions/{suggestion_id}/review")
def review_suggestion_api(suggestion_id: str, payload: SuggestionReview, request: Request) -> dict[str, Any]:
    user = _request_user(request)
    try:
        return review_suggestion(
            user["organization_id"],
            suggestion_id,
            user["id"],
            accept=payload.accept,
            final_value=payload.final_value,
            note=payload.note,
        )
    except KeyError as exc:
        raise HTTPException(404, "Suggestion not found") from exc
