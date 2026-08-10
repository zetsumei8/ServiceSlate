from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from serviceslate.app import app
from serviceslate.db import DB_PATH, connect, init_db


@pytest.fixture(autouse=True)
def fresh_demo_db():
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    init_db()
    yield


def login(client: TestClient, email: str) -> None:
    response = client.post(
        "/api/login",
        json={"email": email, "password": "ServiceSlateDemo!26"},
    )
    assert response.status_code == 200


def test_coordinator_dashboard_and_compass_are_real():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        dashboard = client.get("/api/dashboard").json()
        assert dashboard["profile"] == "automotive_equipment"
        assert dashboard["counts"]["new_requests"] >= 1
        assert dashboard["counts"]["needs_scheduling"] >= 2

        compass = client.get("/api/compass").json()
        two_tech = next(s for s in compass["suggestions"] if s["job_number"] == "WO-26103")
        assert two_tech["crew"] == 2
        assert len(two_tech["recommended_technicians"]) == 2
        assert compass["duplication_warnings"]


def test_profile_isolation_for_grooming():
    with TestClient(app) as client:
        login(client, "reception@demo.example.com")
        me = client.get("/api/me").json()
        assert me["organization_profile"] == "grooming"
        dashboard = client.get("/api/dashboard").json()
        assert dashboard["profile"] == "grooming"
        assert "appointments" in dashboard


def test_technician_context_includes_approved_equipment_history():
    with TestClient(app) as client:
        login(client, "tech.chris@demo.example.com")
        today = client.get("/api/tech/today").json()
        assert today["visits"]
        future_job = next(v for v in today["visits"] if v["customer_name"] == "Future Ford of Clovis")
        context = client.get(f"/api/jobs/{future_job['job_id']}/context").json()
        assert context["history"]
        assert any("synchron" in (h.get("correction") or "").lower() for h in context["history"])


def test_schedule_command_is_idempotent_and_rejects_missing_second_tech():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        jobs = client.get("/api/calendar").json()["unscheduled"]
        two = next(j for j in jobs if j["crew_min"] == 2)
        payload = {
            "command_id": "test-two-tech-command",
            "job_id": two["id"],
            "technician_user_id": "u_chris",
            "helper_user_id": None,
            "start_at": "2030-08-10T15:00:00+00:00",
            "end_at": "2030-08-10T19:00:00+00:00",
        }
        bad = client.post("/api/schedule", json=payload)
        assert bad.status_code == 400
        payload["helper_user_id"] = "u_jordan"
        first = client.post("/api/schedule", json=payload)
        assert first.status_code == 200
        second = client.post("/api/schedule", json=payload)
        assert second.status_code == 200
        assert first.json() == second.json()


def test_backup_is_point_and_click_api():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        response = client.post("/api/backups")
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is True
        assert Path(result["path"]).exists()


def test_shared_host_blocks_tenant_access_to_physical_backups():
    from serviceslate.db import new_id, utcnow
    with connect() as conn:
        now = utcnow()
        for name in ("Live One", "Live Two"):
            conn.execute(
                "INSERT INTO organizations(id,name,profile,is_demo,created_at) VALUES(?,?,?,?,?)",
                (new_id("org"), name, "automotive_equipment", 0, now),
            )
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        assert client.post("/api/backups").status_code == 409
        assert client.get("/api/system/status").json()["backups"] == []


def test_job_review_and_submission_permissions_are_enforced():
    submission = {
        "command_id": "unassigned-submit", "finding": "Test", "correction": "Test",
        "verification": "Test", "outcome": "COMPLETED", "measurements": [], "parts": [], "recommendations": [],
    }
    with TestClient(app) as client:
        login(client, "tech.chris@demo.example.com")
        assigned_job_id = client.get("/api/tech/today").json()["visits"][0]["job_id"]
        job = client.get(f"/api/jobs/{assigned_job_id}/detail").json()["job"]
        assert client.patch(f"/api/jobs/{assigned_job_id}", json={"version": job["version"], "status": "IN_PROGRESS"}).status_code == 403
        assert client.post("/api/jobs/j_new/submit-work", json=submission).status_code == 403
        assert client.get("/api/reviews").status_code == 403
        client.post("/api/logout")
        login(client, "coordinator@demo.example.com")
        assert client.patch(f"/api/jobs/{assigned_job_id}", json={"version": job["version"], "status": "IN_PROGRESS"}).status_code == 200


def test_partial_two_person_window_releases_helper_for_later_work():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        first = client.post("/api/schedule", json={
            "command_id": "partial-crew-1", "job_id": "j_two", "technician_user_id": "u_chris",
            "helper_user_id": "u_jordan", "start_at": "2030-08-10T08:00:00-07:00", "end_at": "2030-08-10T12:00:00-07:00",
        })
        assert first.status_code == 200
        assert first.json()["helper_end_at"] == "2030-08-10T09:00:00-07:00"
        second = client.post("/api/schedule", json={
            "command_id": "partial-crew-2", "job_id": "j_unsched", "technician_user_id": "u_jordan",
            "helper_user_id": None, "start_at": "2030-08-10T09:00:00-07:00", "end_at": "2030-08-10T10:30:00-07:00",
        })
        assert second.status_code == 200


def test_parts_ready_unblocks_return_visit():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        response = client.post("/api/job-parts/jp_cyl/status", json={"command_id": "parts-arrived", "status": "RECEIVED"})
        assert response.status_code == 200
        assert response.json()["all_ready"] is True
        detail = client.get("/api/jobs/j_parts/detail").json()
        assert detail["job"]["status"] == "APPROVED_UNSCHEDULED"
        assert "schedule return visit" in detail["job"]["next_action"].lower()


def test_estimate_approval_is_exact_revision_authorization_and_unblocks_job():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        created = client.post("/api/estimates", json={
            "command_id": "estimate-create-test", "customer_id": "c_future", "job_id": "j_new",
            "lines": [{"line_type": "LABOR", "description": "Diagnosis and repair", "quantity": 2, "unit_price_cents": 12500}],
            "assumptions": "Access available", "exclusions": "Parts beyond scope",
        })
        assert created.status_code == 200
        eid = created.json()["id"]
        approved = client.post(f"/api/estimates/{eid}/action", json={
            "command_id": "estimate-approve-test", "action": "approve", "signer_name": "Rosa Martinez", "signer_title": "Service Manager"
        })
        assert approved.status_code == 200
        detail = client.get(f"/api/estimates/{eid}").json()
        assert detail["estimate"]["status"] == "APPROVED"
        assert len(detail["authorizations"]) == 1
        assert "revision 1" in detail["authorizations"][0]["scope_text"]
        assert "250.00" in detail["authorizations"][0]["scope_text"]
        job = client.get("/api/jobs/j_new/detail").json()["job"]
        assert job["status"] == "APPROVED_UNSCHEDULED"


def test_file_upload_is_hash_deduplicated():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        data = b"same field photo bytes"
        form = {"entity_type": "job", "entity_id": "j_new", "category": "PHOTO"}
        first = client.post("/api/files", data=form, files={"file": ("before.jpg", data, "image/jpeg")})
        second = client.post("/api/files", data=form, files={"file": ("before-again.jpg", data, "image/jpeg")})
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()["already_exists"] is False
        assert second.json()["already_exists"] is True
        listed = client.get("/api/files", params={"entity_type": "job", "entity_id": "j_new"}).json()
        assert len(listed) == 1


def test_customer_csv_import_stages_duplicates_and_commits_only_ready_rows():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        csv_data = "name,phone,address,city,state,zip\nFuture Ford of Clovis,555,old,Fresno,CA,93701\nNew Demo Fleet,559-555-9999,10 Test Ave,Fresno,CA,93727\n"
        preview = client.post("/api/import/customers/preview", files={"file": ("customers.csv", csv_data.encode(), "text/csv")})
        assert preview.status_code == 200
        result = preview.json()
        assert result["ready_count"] == 1
        assert result["issue_count"] == 1
        commit = client.post(f"/api/import/{result['batch_id']}/commit", json={"command_id": "import-commit-test"})
        assert commit.status_code == 200
        assert commit.json()["created"] == 1
        search = client.get("/api/search", params={"q": "New Demo Fleet"}).json()
        assert any(x["type"] == "customer" and "New Demo Fleet" in x["label"] for x in search)


def test_technician_cannot_open_unassigned_live_job_but_can_search_approved_history():
    with TestClient(app) as client:
        login(client, "tech.chris@demo.example.com")
        assert client.get("/api/jobs/j_new/context").status_code == 403
        live_results = client.get("/api/search", params={"q": "Hydraulic leak reported"}).json()
        assert not any(x.get("type") == "job" and x.get("id") == "j_new" for x in live_results)
        history = client.get("/api/search", params={"q": "synchronization"}).json()
        assert any(x["type"] == "history" for x in history)


def test_grooming_profile_cannot_use_automotive_parts_routes():
    with TestClient(app) as client:
        login(client, "reception@demo.example.com")
        assert client.get("/api/parts").status_code == 404
        assert client.get("/api/grooming/waitlist").status_code == 200


def test_same_technician_can_submit_and_lock_multiple_return_visits_on_one_job():
    with TestClient(app) as client:
        login(client, "tech.chris@demo.example.com")
        first = client.post("/api/jobs/j_sched_future/submit-work", json={
            "command_id": "return-submit-1", "finding": "Further adjustment required", "correction": "Initial adjustment completed",
            "verification": "Unit improved but needs return visit", "outcome": "NEED_RETURN_VISIT", "measurements": [], "parts": [], "recommendations": []
        })
        assert first.status_code == 200
        first_id = first.json()["submission_id"]
        client.post("/api/logout")
        login(client, "coordinator@demo.example.com")
        assert client.post(f"/api/reviews/{first_id}", json={"command_id": "return-review-1", "action": "approve", "note": "Return approved"}).status_code == 200
        scheduled = client.post("/api/schedule", json={
            "command_id": "return-schedule-2", "job_id": "j_sched_future", "technician_user_id": "u_chris", "helper_user_id": None,
            "start_at": "2026-08-12T08:00:00-07:00", "end_at": "2026-08-12T10:00:00-07:00"
        })
        assert scheduled.status_code == 200
        client.post("/api/logout")
        login(client, "tech.chris@demo.example.com")
        second = client.post("/api/jobs/j_sched_future/submit-work", json={
            "command_id": "return-submit-2", "finding": "Return adjustment completed", "correction": "Final synchronization adjustment",
            "verification": "Five cycles normal", "outcome": "COMPLETED", "measurements": [], "parts": [], "recommendations": []
        })
        assert second.status_code == 200
        second_id = second.json()["submission_id"]
        assert second_id != first_id
        client.post("/api/logout")
        login(client, "coordinator@demo.example.com")
        assert client.post(f"/api/reviews/{second_id}", json={"command_id": "return-review-2", "action": "approve", "note": "Complete"}).status_code == 200
        detail = client.get("/api/jobs/j_sched_future/detail").json()
        locked = [s for s in detail["submissions"] if s["state"] == "LOCKED" and s["technician_user_id"] == "u_chris"]
        assert len(locked) >= 2


def test_guided_setup_creates_real_local_organization_and_signs_in():
    with TestClient(app) as client:
        response = client.post("/api/setup/organization", json={
            "business_name": "Central Shop Systems", "profile": "automotive_equipment", "admin_name": "Alex Owner",
            "email": "alex.owner@example.local", "password": "local-pass-123", "phone": "559-555-0100",
            "address1": "100 Main St", "city": "Fresno", "state": "CA", "postal_code": "93721"
        })
        assert response.status_code == 200
        me = client.get("/api/me").json()
        assert me["organization_name"] == "Central Shop Systems"
        assert me["organization_profile"] == "automotive_equipment"
        assert me["is_demo"] == 0
        team = client.get("/api/team").json()
        assert len(team) == 1 and team[0]["role"] == "ADMIN"


def test_live_workspace_readiness_is_tenant_scoped_and_demo_safe():
    with TestClient(app) as client:
        created = client.post("/api/setup/organization", json={
            "business_name": "Readiness Shop", "profile": "automotive_equipment", "admin_name": "Jamie Owner",
            "email": "workspace.readiness@example.local", "password": "local-pass-123",
        })
        assert created.status_code == 200
        readiness = client.get("/api/workspace/readiness")
        assert readiness.status_code == 200
        body = readiness.json()
        assert body["is_demo"] is False
        assert body["total"] == 5
        assert {item["id"] for item in body["items"]} == {"customers", "team", "services", "records", "backup"}
        assert next(item for item in body["items"] if item["id"] == "team")["complete"] is False

    with TestClient(app) as demo:
        login(demo, "coordinator@demo.example.com")
        assert demo.get("/api/workspace/readiness").json()["is_demo"] is True


def test_hosted_guided_setup_inherits_secure_public_address_and_strong_password_policy(monkeypatch):
    monkeypatch.setenv("SERVICESLATE_PRODUCTION_MODE", "1")
    monkeypatch.setenv("SERVICESLATE_PUBLIC_BASE_URL", "https://service.example.test")
    with TestClient(app) as client:
        weak = client.post("/api/setup/organization", json={
            "business_name": "Hosted Shop", "profile": "automotive_equipment", "admin_name": "Hosted Owner",
            "email": "hosted.owner@example.local", "password": "shortpass"
        })
        assert weak.status_code == 400
        created = client.post("/api/setup/organization", json={
            "business_name": "Hosted Shop", "profile": "automotive_equipment", "admin_name": "Hosted Owner",
            "email": "hosted.owner@example.local", "password": "hosted-pass-123"
        })
        assert created.status_code == 200
        with connect() as conn:
            org = conn.execute("SELECT production_mode,public_base_url FROM organizations WHERE id=?", (created.json()["organization_id"],)).fetchone()
        assert bool(org["production_mode"]) is True
        assert org["public_base_url"] == "https://service.example.test"


def test_manager_can_add_profile_valid_team_member():
    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        created = client.post("/api/team", json={
            "name": "Demo Installer", "email": "installer@example.local", "role": "TECHNICIAN",
            "temporary_password": "temporary-123", "qualifications": ["LIFT", "INSTALL"]
        })
        assert created.status_code == 200
        team = client.get("/api/team").json()
        member = next(x for x in team if x["email"] == "installer@example.local") if team and "email" in team[0] else None
        # Team endpoint intentionally returns operational identity; verify by ID if email is hidden.
        if member is None:
            assert any(x["id"] == created.json()["id"] and "INSTALL" in x["qualifications"] for x in team)


def test_remote_start_is_explicit_approved_exception_without_tracking():
    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        created = client.post("/api/remote-starts", json={
            "command_id": "remote-start-test", "technician_user_id": "u_chris", "vehicle_id": "veh12",
            "work_date": "2026-08-15", "origin_label": "Madera area", "latitude": 36.9613, "longitude": -120.0607,
            "reason": "Distant first job; truck approved overnight", "expected_return_label": "Fresno branch"
        })
        assert created.status_code == 200
        rows = client.get("/api/remote-starts").json()
        row = next(r for r in rows if r["work_date"] == "2026-08-15" and r["technician_user_id"] == "u_chris")
        assert row["origin_label"] == "Madera area"
        assert "gps" not in str(row).lower()


def test_point_and_click_restore_validates_and_restores_database():
    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        backup = client.post("/api/backups").json()
        backup_bytes = Path(backup["path"]).read_bytes()
        # Add data after the backup so restore has something observable to undo.
        made = client.post("/api/customers", json={
            "name": "Restore Test Customer", "phone": "555-1212", "email": "restore@example.local",
            "address1": "1 Temporary Rd", "city": "Fresno", "state": "CA", "postal_code": "93721"
        })
        assert made.status_code == 200
        assert any(x.get("label") == "Restore Test Customer" for x in client.get("/api/search", params={"q": "Restore Test Customer"}).json())
        restored = client.post("/api/restore", files={"file": (backup["filename"], backup_bytes, "application/zip")})
        assert restored.status_code == 200
        assert restored.json()["ok"] is True
        # Restore deliberately signs the user out; log back into the restored database.
        login(client, "manager@demo.example.com")
        results = client.get("/api/search", params={"q": "Restore Test Customer"}).json()
        assert not any(x.get("label") == "Restore Test Customer" for x in results)


def test_controlled_form_builder_publishes_profile_scoped_template():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        made = client.post("/api/forms/templates", json={
            "command_id": "form-template-create-test",
            "name": "Return Visit Verification",
            "category": "Service",
            "wording": "Confirm the equipment condition before leaving.",
            "fields": [
                {"key": "operating_normally", "label": "Operating normally?", "type": "choice", "required": True, "options": ["Yes", "No"]},
                {"key": "notes", "label": "Notes", "type": "long_text", "required": False, "options": []},
            ],
        })
        assert made.status_code == 200
        templates = client.get("/api/forms/templates").json()
        template = next(x for x in templates if x["name"] == "Return Visit Verification")
        assert template["version"] == 1
        assert template["status"] == "PUBLISHED"
        assert template["schema"][0]["required"] is True


def test_grooming_cancellation_surfaces_eligible_waitlist_matches():
    with TestClient(app) as client:
        login(client, "reception@demo.example.com")
        detail = client.get("/api/grooming/appointments/ga2")
        assert detail.status_code == 200
        canceled = client.post("/api/grooming/appointments/ga2/cancel", json={
            "command_id": "groom-cancel-match-test",
            "reason": "Customer requested another day",
        })
        assert canceled.status_code == 200
        result = canceled.json()
        assert result["state"] == "CANCELED"
        assert any(x["pet_name"] == "Buddy" for x in result["waitlist_matches"])


def test_grooming_completion_creates_rebooking_obligation():
    with TestClient(app) as client:
        login(client, "groomer.sam@demo.example.com")
        completed = client.post("/api/grooming/appointments/ga1/complete", json={
            "command_id": "groom-complete-test",
            "checkout_note": "Coat healthy; same trim next time.",
            "rebook_weeks": 6,
        })
        assert completed.status_code == 200
        client.post("/api/logout")
        login(client, "reception@demo.example.com")
        rebooking = client.get("/api/grooming/rebooking").json()
        assert any(x["pet_name"] == "Luna" and x["service_name"] == "Full Groom" for x in rebooking)


def test_only_assigned_groomer_or_office_can_complete_appointment():
    with TestClient(app) as client:
        login(client, "reception@demo.example.com")
        denied = client.post("/api/grooming/appointments/ga1/complete", json={"command_id": "groom-denied", "checkout_note": "Nope"})
        assert denied.status_code == 403


def test_legacy_work_history_import_preserves_source_and_becomes_searchable_memory():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        csv_data = (
            "customer,work order,date,serial,technician,complaint,finding,cause,correction,verification\n"
            "Future Ford of Clovis,FF-8841,2024-05-02,DEMO-CL10A-004,Pat Legacy,"
            "Lift drifting,Left side lower,Valve adjustment out of sync,Adjusted synchronization,Cycled five times normal\n"
        )
        preview = client.post(
            "/api/import/work-history/preview",
            data={"source_system": "NoteWise"},
            files={"file": ("notewise-history.csv", csv_data.encode(), "text/csv")},
        )
        assert preview.status_code == 200
        batch = preview.json()
        assert batch["ready_count"] == 1
        committed = client.post(
            f"/api/import/work-history/{batch['batch_id']}/commit",
            json={"command_id": "legacy-history-commit-test"},
        )
        assert committed.status_code == 200
        assert committed.json()["created"] == 1
        results = client.get("/api/search", params={"q": "Lift drifting"}).json()
        historical = next(x for x in results if x["type"] == "history")
        detail = client.get(f"/api/jobs/{historical['id']}/detail").json()
        assert detail["job"]["data_origin"] == "legacy_import"
        locked = next(x for x in detail["submissions"] if x["state"] == "LOCKED")
        assert locked["source_system"] == "NoteWise"
        assert locked["performed_by_text"] == "Pat Legacy"


def test_legacy_work_history_preview_blocks_unknown_equipment_serial_for_review():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        csv_data = "customer,work order,date,serial,correction\nFuture Ford of Clovis,FF-NEW,2024-06-01,UNKNOWN-123,Repaired lift\n"
        preview = client.post(
            "/api/import/work-history/preview",
            data={"source_system": "FastField"},
            files={"file": ("fastfield.csv", csv_data.encode(), "text/csv")},
        )
        assert preview.status_code == 200
        assert preview.json()["ready_count"] == 0
        detail = client.get(f"/api/import/{preview.json()['batch_id']}").json()
        assert "serial not found" in detail["rows"][0]["issue"].lower()


def test_technician_equipment_history_does_not_expose_unapproved_live_work():
    with TestClient(app) as client:
        login(client, "tech.chris@demo.example.com")
        history = client.get("/api/equipment/e_cl10a_1/history")
        assert history.status_code == 200
        payload = history.json()
        assert all(j["status"] in ("READY_TO_INVOICE", "INVOICED", "CLOSED") for j in payload["jobs"])
        assert all(s["state"] in ("APPROVED", "LOCKED") for s in payload["approved_work"])


def test_new_automotive_organization_gets_safe_starter_configuration():
    with TestClient(app) as client:
        created = client.post("/api/setup/organization", json={
            "business_name": "Starter Equipment Co", "profile": "automotive_equipment", "admin_name": "Owner User",
            "email": "starter.owner@example.local", "password": "starter-pass-123", "city": "Fresno", "state": "CA"
        })
        assert created.status_code == 200
        assert len(client.get("/api/forms/templates").json()) >= 2
        assert len(client.get("/api/inspections/templates").json()) >= 1


def test_structured_inspection_is_locked_and_updates_next_due_date():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        template = client.get("/api/inspections/templates").json()[0]
        items = [
            {"item_key": x["key"], "label": x["label"], "result": "PASS", "measurement_value": None, "measurement_unit": x.get("measurement_unit"), "comment": None}
            for x in template["items"]
        ]
        recorded = client.post("/api/inspections", json={
            "command_id": "inspection-record-test", "equipment_id": "e_cl10a_1", "template_id": template["id"],
            "job_id": None, "inspection_date": "2026-08-07", "items": items,
            "summary": "Company procedure completed; no deficiencies recorded.", "customer_ack_name": "Rosa Martinez",
            "next_due_date": "2027-08-07"
        })
        assert recorded.status_code == 200
        assert recorded.json()["state"] == "LOCKED"
        detail = client.get(f"/api/inspections/{recorded.json()['id']}").json()
        assert detail["inspection"]["result"] == "PASS"
        assert len(detail["items"]) == len(items)
        equipment = client.get("/api/equipment/e_cl10a_1/history").json()["equipment"]
        assert equipment["inspection_due_date"] == "2027-08-07"


def test_failed_inspection_item_requires_deficiency_comment():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        template = client.get("/api/inspections/templates").json()[0]
        x = template["items"][0]
        response = client.post("/api/inspections", json={
            "command_id": "inspection-fail-comment-test", "equipment_id": "e_cl10a_1", "template_id": template["id"],
            "inspection_date": "2026-08-07", "items": [{"item_key": x["key"], "label": x["label"], "result": "FAIL"}]
        })
        assert response.status_code == 400
        assert "describe the deficiency" in response.json()["detail"].lower()


def test_assigned_technician_context_includes_inspection_history_and_templates():
    with TestClient(app) as client:
        login(client, "tech.chris@demo.example.com")
        context = client.get("/api/jobs/j_sched_future/context")
        assert context.status_code == 200
        payload = context.json()
        assert payload["inspection_templates"]
        assert payload["inspection_templates"][0]["items"]


def test_assigned_technician_can_record_structured_inspection_for_job():
    with TestClient(app) as client:
        login(client, "tech.chris@demo.example.com")
        context = client.get("/api/jobs/j_sched_future/context").json()
        template = context["inspection_templates"][0]
        result = client.post("/api/inspections", json={
            "command_id": "tech-inspection-test", "equipment_id": context["job"]["equipment_id"],
            "template_id": template["id"], "job_id": "j_sched_future", "inspection_date": "2026-08-07",
            "items": [{"item_key": x["key"], "label": x["label"], "result": "PASS"} for x in template["items"]],
            "summary": "Field inspection recorded by assigned technician."
        })
        assert result.status_code == 200
        assert result.json()["state"] == "LOCKED"


def test_service_catalog_defaults_can_be_managed_without_database_access():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        services = client.get("/api/service-catalog")
        assert services.status_code == 200
        assert services.json()
        created = client.post("/api/service-catalog", json={
            "name": "Lift Troubleshooting", "category": "Repair", "default_minutes": 150,
            "default_crew": 1, "qualification_required": "LIFT"
        })
        assert created.status_code == 200
        service_id = created.json()["id"]
        changed = client.patch(f"/api/service-catalog/{service_id}", json={"default_minutes": 180, "default_crew": 2})
        assert changed.status_code == 200
        assert changed.json()["default_minutes"] == 180
        assert changed.json()["default_crew"] == 2
        retired = client.patch(f"/api/service-catalog/{service_id}", json={"active": False})
        assert retired.status_code == 200
        assert all(x["id"] != service_id for x in client.get("/api/service-catalog").json())


def test_manager_can_add_branch_but_technician_cannot():
    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        created = client.post("/api/branches", json={"name": "Visalia", "city": "Visalia", "state": "CA"})
        assert created.status_code == 200
        assert any(b["name"] == "Visalia" for b in client.get("/api/branches").json())

    with TestClient(app) as client:
        login(client, "tech.chris@demo.example.com")
        denied = client.post("/api/branches", json={"name": "Should Not Exist"})
        assert denied.status_code == 403


def test_secure_customer_estimate_link_records_exact_revision_once():
    with TestClient(app) as office:
        login(office, "coordinator@demo.example.com")
        created = office.post("/api/estimates", json={
            "command_id": "portal-estimate-create", "customer_id": "c_future", "job_id": None,
            "lines": [{"line_type": "LABOR", "description": "Lift service", "quantity": 2, "unit_price_cents": 12500}],
            "assumptions": "Existing electrical supply is serviceable.", "exclusions": "Concrete repair is excluded."
        })
        assert created.status_code == 200
        estimate = office.get(f"/api/estimates/{created.json()['id']}").json()["estimate"]
        link = office.post(f"/api/estimates/{estimate['id']}/portal-link", json={"expires_days": 7})
        assert link.status_code == 200
        token = link.json()["token"]

        with TestClient(app) as public:
            preview = public.get(f"/api/public/portal/{token}")
            assert preview.status_code == 200
            assert preview.json()["estimate_id"] == estimate["id"]
            decision = {
                "command_id": "portal-approval-idempotent", "action": "approve",
                "signer_name": "Rosa Martinez", "signer_title": "Service Manager"
            }
            first = public.post(f"/api/public/portal/{token}/decision", json=decision)
            second = public.post(f"/api/public/portal/{token}/decision", json=decision)
            assert first.status_code == 200
            assert first.json()["state"] == "APPROVED"
            assert second.json() == first.json()
            replay = public.post(f"/api/public/portal/{token}/decision", json={**decision, "command_id": "portal-replay-command"})
            assert replay.status_code == 409

        detail = office.get(f"/api/estimates/{estimate['id']}").json()
        assert detail["estimate"]["status"] == "APPROVED"
        assert any(a["method"] == "SECURE_LINK" and a["signer_name"] == "Rosa Martinez" for a in detail["authorizations"])


def test_customer_contact_log_never_fakes_email_delivery_and_can_create_followup():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        response = client.post("/api/customers/c_future/communications", json={
            "command_id": "contact-email-prepared", "channel": "EMAIL", "direction": "OUTBOUND",
            "subject": "Estimate follow-up", "body": "Checking whether you have questions.",
            "outcome": "Prepared", "followup_summary": "Call if no reply", "followup_due_at": "2026-08-14"
        })
        assert response.status_code == 200
        assert response.json()["status"] == "PREPARED"
        history = client.get("/api/customers/c_future/communications").json()
        assert history[0]["status"] == "PREPARED"
        followups = client.get("/api/followups").json()
        assert any(f["summary"] == "Call if no reply" for f in followups)


def test_customer_edit_updates_same_identity_and_preserves_audit_history():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        before = client.get("/api/customers/c_future").json()["customer"]
        changed = client.patch("/api/customers/c_future", json={
            "version": before["version"], "phone": "(559) 555-2222", "notes": "Primary service account"
        })
        assert changed.status_code == 200
        assert changed.json()["id"] == "c_future"
        assert changed.json()["phone"] == "(559) 555-2222"
        detail = client.get("/api/customers/c_future").json()
        assert detail["customer"]["id"] == "c_future"
        assert any(e["action"] == "UPDATED" for e in detail["history"])


def test_equipment_edit_rejects_duplicate_serial_and_updates_same_record():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        equipment = client.get("/api/equipment/e_cl10a_1/history").json()["equipment"]
        changed = client.patch("/api/equipment/e_cl10a_1", json={
            "version": equipment["version"], "bay": "Bay 7A", "notes": "Updated location label"
        })
        assert changed.status_code == 200
        assert changed.json()["id"] == "e_cl10a_1"
        assert changed.json()["bay"] == "Bay 7A"
        other = next(e for e in client.get("/api/equipment").json() if e["id"] != "e_cl10a_1" and e.get("serial_number"))
        duplicate = client.patch("/api/equipment/e_cl10a_1", json={
            "version": changed.json()["version"], "serial_number": other["serial_number"]
        })
        assert duplicate.status_code == 409


def test_major_office_creates_are_idempotent_when_retried():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        customer_payload = {
            "command_id": "create-customer-once", "name": "Retry Safe Auto", "city": "Fresno", "state": "CA"
        }
        c1 = client.post("/api/customers", json=customer_payload)
        c2 = client.post("/api/customers", json=customer_payload)
        assert c1.status_code == 200 and c2.status_code == 200
        assert c1.json() == c2.json()
        cid, lid = c1.json()["id"], c1.json()["location_id"]

        equipment_payload = {
            "command_id": "create-equipment-once", "customer_id": cid, "location_id": lid,
            "category": "Vehicle Lift", "manufacturer": "Challenger", "model": "CL10A", "serial_number": "RETRY-SERIAL-1"
        }
        e1 = client.post("/api/equipment", json=equipment_payload)
        e2 = client.post("/api/equipment", json=equipment_payload)
        assert e1.status_code == 200 and e2.status_code == 200
        assert e1.json() == e2.json()

        job_payload = {
            "command_id": "create-job-once", "customer_id": cid, "location_id": lid, "equipment_id": e1.json()["id"],
            "description": "Inspect intermittent control issue", "job_type": "Service Call",
            "estimated_minutes": 90, "crew_min": 1, "crew_recommended": 1
        }
        j1 = client.post("/api/jobs", json=job_payload)
        j2 = client.post("/api/jobs", json=job_payload)
        assert j1.status_code == 200 and j2.status_code == 200
        assert j1.json() == j2.json()
        jobs = client.get("/api/jobs").json()
        assert sum(1 for j in jobs if j["id"] == j1.json()["id"]) == 1


def test_toolbox_exposes_many_local_first_tools_and_profile_filters():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        tools = client.get("/api/tools").json()
        ids = {t["id"] for t in tools}
        assert len(tools) >= 20
        assert {"inch_fraction", "hydraulic_force", "quickbooks_handoff", "calendar_link"} <= ids
        assert all(t["availability"] == "READY" for t in tools)

    with TestClient(app) as client:
        login(client, "reception@demo.example.com")
        ids = {t["id"] for t in client.get("/api/tools").json()}
        assert "hydraulic_force" not in ids
        assert "quickbooks_handoff" not in ids
        assert "unit_length" in ids
        assert "calendar_link" in ids


def test_field_math_tools_are_deterministic_and_do_not_need_external_services():
    with TestClient(app) as client:
        login(client, "tech.chris@demo.example.com")
        fraction = client.post("/api/tools/inch_fraction/run", json={"inputs": {"value": 8.375, "mode": "decimal_to_fraction", "denominator": 64}})
        assert fraction.status_code == 200
        assert "8 3/8" in fraction.json()["headline"]
        pressure = client.post("/api/tools/unit_pressure/run", json={"inputs": {"value": 100, "from_unit": "psi", "to_unit": "bar"}})
        assert pressure.status_code == 200
        assert "bar" in pressure.json()["headline"]
        hydraulic = client.post("/api/tools/hydraulic_force/run", json={"inputs": {"pressure_psi": 2000, "bore_in": 2, "rod_in": 1}})
        assert hydraulic.status_code == 200
        assert "lbf" in hydraulic.json()["headline"]


def test_handoff_tools_generate_links_without_claiming_external_success():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        map_result = client.post("/api/tools/map_link/run", json={"inputs": {"address": "123 Main St Fresno CA", "provider": "OpenStreetMap"}}).json()
        assert map_result["action"]["url"].startswith("https://www.openstreetmap.org/")
        email_result = client.post("/api/tools/email_link/run", json={"inputs": {"to": "service@example.com", "subject": "WO-1", "body": "Ready"}}).json()
        assert email_result["action"]["url"].startswith("mailto:")
        assert "does not mark" in email_result["details"][0]


def test_standard_file_wrappers_generate_calendar_contact_and_import_files():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        ics = client.get("/api/tools/calendar_link/download", params={"title":"Service Call","start_at":"2026-08-10T08:00","end_at":"2026-08-10T09:00","location":"Fresno"})
        assert ics.status_code == 200
        assert "BEGIN:VCALENDAR" in ics.text
        vcf = client.get("/api/tools/vcard/download", params={"name":"Pat Demo","phone":"555-1212"})
        assert vcf.status_code == 200
        assert "BEGIN:VCARD" in vcf.text
        template = client.get("/api/tools/customer_csv_template/download")
        assert template.status_code == 200
        assert "customer_name" in template.text


def test_quickbooks_handoff_is_export_only_and_print_tools_use_existing_records():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        handoff = client.get("/api/tools/quickbooks_handoff/download")
        assert handoff.status_code == 200
        assert "work_order,customer,location" in handoff.text
        work = client.get("/api/tools/work_order_print/download", params={"job_number":"WO-24091"})
        assert work.status_code == 200
        assert "Future Ford" in work.text
        equipment = client.get("/api/tools/equipment_print/download", params={"serial":"DEMO-CL10A-004"})
        assert equipment.status_code == 200
        assert "Challenger" in equipment.text


def test_tool_usage_is_recorded_without_storing_calculator_inputs():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        r = client.post("/api/tools/markup_margin/run", json={"inputs":{"cost":100,"sell":150}})
        assert r.status_code == 200
        from serviceslate.db import connect
        with connect() as conn:
            row = conn.execute("SELECT tool_id FROM tool_usage ORDER BY created_at DESC LIMIT 1").fetchone()
            assert row["tool_id"] == "markup_margin"
            cols = {x["name"] for x in conn.execute("PRAGMA table_info(tool_usage)").fetchall()}
            assert "inputs_json" not in cols


def test_service_day_confirmation_records_explicit_customer_awareness():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        created = client.post(
            "/api/jobs/j_sched_future/service-day-link",
            json={"command_id": "service-link-1", "expires_days": 7, "recipient": "service@example.test", "channel": "EMAIL"},
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["status"] == "AWAITING_CONFIRMATION"
        portal = client.get(f"/api/public/portal/{payload['token']}")
        assert portal.status_code == 200
        page = portal.json()
        assert page["kind"] == "service_day"
        assert page["scheduled_date"] == payload["scheduled_date"]
        assert "scheduled service day" in page["disclosure_text"].lower()

        confirmed = client.post(
            f"/api/public/portal/{payload['token']}/decision",
            json={"command_id": "confirm-service-day", "action": "confirm", "signer_name": "Maria Lopez"},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["state"] == "CONFIRMED"
        rows = client.get("/api/service-confirmations").json()
        row = next(r for r in rows if r["id"] == payload["confirmation_id"])
        assert row["status"] == "CONFIRMED"
        assert row["confirmed_by_name"] == "Maria Lopez"


def test_reschedule_invalidates_previous_service_day_confirmation():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        created = client.post("/api/jobs/j_sched_future/service-day-link", json={"command_id": "service-link-2", "expires_days": 7}).json()
        confirmed = client.post(
            f"/api/public/portal/{created['token']}/decision",
            json={"command_id": "confirm-before-reschedule", "action": "confirm", "signer_name": "Customer Contact"},
        )
        assert confirmed.status_code == 200
        moved = client.post("/api/schedule", json={
            "command_id": "move-confirmed-job", "job_id": "j_sched_future", "technician_user_id": "u_chris",
            "helper_user_id": None, "start_at": "2026-09-30T08:00:00-07:00", "end_at": "2026-09-30T10:00:00-07:00",
        })
        assert moved.status_code == 200
        assert moved.json()["rescheduled"] is True
        rows = client.get("/api/service-confirmations").json()
        old = next(r for r in rows if r["id"] == created["confirmation_id"])
        assert old["status"] == "STALE"
        stale_portal = client.get(f"/api/public/portal/{created['token']}")
        assert stale_portal.status_code == 409


def test_service_day_can_be_confirmed_by_phone_without_hosting():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        created = client.post("/api/jobs/j_sched_valley/service-day-link", json={"command_id": "service-link-3", "expires_days": 7}).json()
        response = client.post(
            f"/api/service-confirmations/{created['confirmation_id']}/manual-confirm",
            json={"command_id": "phone-confirm", "contact_name": "Pat Manager", "channel": "PHONE"},
        )
        assert response.status_code == 200
        row = next(r for r in client.get("/api/service-confirmations").json() if r["id"] == created["confirmation_id"])
        assert row["status"] == "CONFIRMED"
        assert row["channel"] == "PHONE"


def test_integration_catalog_exposes_open_first_and_paid_optional_adapters():
    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        catalog = client.get("/api/integrations/catalog")
        assert catalog.status_code == 200
        rows = {x["id"]: x for x in catalog.json()}
        assert rows["SMTP"]["open_source"] is True
        assert rows["NTFY"]["open_source"] is True
        assert rows["WEBDAV"]["open_source"] is True
        assert rows["CALDAV"]["open_source"] is True
        assert rows["TWILIO_SMS"]["mode"] == "paid_adapter"
        assert rows["MOBILE_SMS"]["mode"] == "handoff"


def test_outbound_http_rejects_private_destinations_and_twilio_fails_closed(monkeypatch):
    from serviceslate import integrations as integration_module
    from serviceslate.db import new_id, utcnow

    with pytest.raises(RuntimeError, match="public internet"):
        integration_module._http_request("http://127.0.0.1:8080")
    with connect() as conn:
        now = utcnow()
        conn.execute(
            "INSERT INTO integration_deliveries(id,organization_id,provider,external_reference,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (new_id("delivery"), "org_auto_demo", "TWILIO_SMS", "SM-security-test", "SENT", now, now),
        )
    monkeypatch.setattr(integration_module.VAULT, "get_provider", lambda *args: {})
    with TestClient(app) as client:
        response = client.post("/api/integrations/twilio/status", data={"MessageSid": "SM-security-test", "MessageStatus": "delivered"})
        assert response.status_code == 403


def test_import_and_restore_limits_reject_oversized_payloads(monkeypatch):
    from serviceslate import features
    monkeypatch.setattr(features, "MAX_UPLOAD_BYTES", 4)
    monkeypatch.setattr(features, "MAX_RESTORE_BYTES", 4)
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        csv_result = client.post("/api/import/customers/preview", files={"file": ("large.csv", b"12345", "text/csv")})
        assert csv_result.status_code == 413
        client.post("/api/logout")
        login(client, "manager@demo.example.com")
        restore_result = client.post("/api/restore", files={"file": ("large.zip", b"12345", "application/zip")})
        assert restore_result.status_code == 413


def test_office_host_requires_tls_material(monkeypatch):
    import run_serviceslate
    monkeypatch.setenv("SERVICESLATE_LAN_ROLE", "host")
    monkeypatch.delenv("SERVICESLATE_LAN_TLS_CERT", raising=False)
    monkeypatch.delenv("SERVICESLATE_LAN_TLS_KEY", raising=False)
    with pytest.raises(RuntimeError, match="requires .*HTTPS"):
        run_serviceslate.main()


def test_integration_configuration_keeps_secret_out_of_sqlite(monkeypatch):
    from serviceslate import integrations as integration_module

    monkeypatch.setattr(integration_module.VAULT, "set_provider", lambda org, provider, values: None)
    monkeypatch.setattr(integration_module.VAULT, "get_provider", lambda org, provider: {"password": "saved-secret"} if provider == "SMTP" else {})
    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        response = client.put("/api/integrations/SMTP", json={
            "enabled": True,
            "settings": {"host": "mail.example.test", "port": 587, "username": "service@example.test", "from_email": "service@example.test", "security": "STARTTLS"},
            "secrets": {"password": "super-secret-password"},
        })
        assert response.status_code == 200
        catalog = {x["id"]: x for x in client.get("/api/integrations/catalog").json()}
        assert catalog["SMTP"]["settings"]["host"] == "mail.example.test"
        assert catalog["SMTP"]["secret_fields_set"] == ["password"]
        assert "super-secret-password" not in str(catalog["SMTP"])
        from serviceslate.db import connect
        with connect() as conn:
            row = conn.execute("SELECT settings_json FROM integration_status WHERE organization_id='org_auto_demo' AND provider='SMTP'").fetchone()
            assert "super-secret-password" not in row[0]


def test_prepared_email_can_use_real_transport_boundary_without_duplicate_record(monkeypatch):
    from serviceslate import integrations as integration_module

    monkeypatch.setattr(integration_module, "_provider_row", lambda org, provider: ({"host":"mail.test","from_email":"service@test"}, {"password":"x"}))
    monkeypatch.setattr(integration_module, "_smtp", lambda settings, secrets, *, to, subject, body: "smtp-test-ref")
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        created = client.post("/api/customers/c_future/communications", json={
            "command_id":"integration-email-send-once", "channel":"EMAIL", "direction":"OUTBOUND",
            "subject":"Service day", "body":"Please confirm your scheduled service day."
        })
        assert created.status_code == 200
        cid = created.json()["id"]
        sent = client.post(f"/api/integrations/communications/{cid}/deliver", json={"provider":"SMTP"})
        assert sent.status_code == 200
        assert sent.json()["status"] == "SENT"
        history = client.get("/api/customers/c_future/communications").json()
        row = next(x for x in history if x["id"] == cid)
        assert row["delivery_provider"] == "SMTP"
        assert row["delivery_reference"] == "smtp-test-ref"
        assert row["status"] == "SENT"


def test_public_nominatim_is_opt_in_not_silently_enabled():
    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        configured = client.put("/api/integrations/NOMINATIM", json={
            "enabled": True,
            "settings": {"base_url":"https://nominatim.openstreetmap.org", "user_agent":"ServiceSlate test", "allow_public_osm":False},
            "secrets": {},
        })
        assert configured.status_code == 200
        result = client.post("/api/integrations/nominatim/geocode", json={"address":"Fresno, CA"})
        assert result.status_code == 400
        assert "disabled" in result.json()["detail"].lower()


def test_governance_policy_is_deterministic_and_ai_off_by_default():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        policy = client.get("/api/governance/policy")
        assert policy.status_code == 200
        data = policy.json()
        assert data["deterministic_first"] is True
        assert data["ai_enabled"] is False
        assert data["ai_is_authority"] is False
        assert data["human_review_for_software_derived"] is True


def test_fastfield_mail_intake_is_deterministic_and_waits_for_human_review():
    from serviceslate.db import connect
    from serviceslate.mail_intake import MailAttachment, MailEnvelope, ingest_mail_message

    envelope = MailEnvelope(
        external_id="fastfield-test-1",
        internet_message_id="<fastfield-test-1@example.com>",
        sender="forms@example.com",
        subject="FastField Work Order",
        received_at="2026-08-08T12:00:00Z",
        body="",
        attachments=[
            MailAttachment(
                "submission.csv",
                "text/csv",
                b"Customer,Serial Number,Work Order,Technician,Work Performed\n"
                b"Future Ford of Clovis,DEMO-CL10A-004,FF-100,Chris,Adjusted lift\n",
            )
        ],
    )
    result = ingest_mail_message("org_auto_demo", "u_coord", "OUTLOOK", envelope)
    assert result["state"] == "AWAITING_HUMAN_REVIEW"
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM import_rows WHERE batch_id=?",
            (result["batch_id"],),
        ).fetchone()
        assert row["derivation_method"] == "DETERMINISTIC_STRUCTURED"
        assert row["review_state"] == "AWAITING_HUMAN_REVIEW"
        import json
        normalized = json.loads(row["normalized_json"])
        assert normalized["_provenance"]["ai_used"] is False
        assert normalized["_provenance"]["human_review_required"] is True


def test_fastfield_history_commit_records_named_human_approval():
    from serviceslate.db import connect
    from serviceslate.mail_intake import MailAttachment, MailEnvelope, ingest_mail_message

    envelope = MailEnvelope(
        external_id="fastfield-test-2",
        internet_message_id="<fastfield-test-2@example.com>",
        sender="forms@example.com",
        subject="FastField Work Order",
        received_at="2026-08-08T12:00:00Z",
        body="",
        attachments=[
            MailAttachment(
                "submission.csv",
                "text/csv",
                b"Customer,Serial Number,Work Order,Technician,Work Performed,Date\n"
                b"Future Ford of Clovis,DEMO-CL10A-004,FF-101,Chris,Adjusted lift,2026-08-01\n",
            )
        ],
    )
    staged = ingest_mail_message("org_auto_demo", "u_coord", "OUTLOOK", envelope)
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        approved = client.post(
            f"/api/import/work-history/{staged['batch_id']}/commit",
            json={"command_id": "approve-fastfield-101"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["created"] == 1
    with connect() as conn:
        row = conn.execute("SELECT * FROM import_rows WHERE batch_id=?", (staged["batch_id"],)).fetchone()
        assert row["review_state"] == "HUMAN_APPROVED"
        assert row["reviewed_by_user_id"] == "u_coord"
        assert row["reviewed_at"]


def test_ai_fallback_is_only_a_suggestion_until_human_accepts_it():
    from serviceslate.governance import create_suggestion

    sid = create_suggestion(
        "org_auto_demo",
        kind="TECHNICAL_FINDING",
        entity_type="equipment",
        entity_id="e_cl10a_1",
        field_name="possible_cause",
        suggested_value="Possible synchronization issue",
        method="AI_FALLBACK",
        evidence={"source": "technician note"},
    )
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        pending = client.get("/api/governance/suggestions").json()
        suggestion = next(x for x in pending if x["id"] == sid)
        assert suggestion["status"] == "AWAITING_HUMAN_REVIEW"
        assert suggestion["method"] == "AI_FALLBACK"
        reviewed = client.post(
            f"/api/governance/suggestions/{sid}/review",
            json={"accept": True, "final_value": "Confirmed synchronization issue", "note": "Reviewed against field report"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["status"] == "ACCEPTED"
        assert reviewed.json()["reviewed_by_user_id"] == "u_coord"


def test_polished_ui_uses_serviceslate_dialogs_instead_of_browser_prompts():
    static = Path(__file__).resolve().parents[1] / "src" / "serviceslate" / "static"
    js = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "styles.css").read_text(encoding="utf-8")
    assert "prompt(" not in js
    assert "confirm(" not in js
    assert "function askConfirm" in js
    assert "function askText" in js
    assert ".modal-backdrop" in css
    assert "Saved locally" in js
    assert "mobileSignOut" in js


def _create_service_day_confirmation(client: TestClient, command_id: str = "sdc-create-test") -> dict:
    response = client.post(
        "/api/jobs/j_sched_future/service-day-link",
        json={
            "command_id": command_id,
            "recipient": "service@example.com",
            "channel": "EMAIL",
            "expires_days": 14,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_service_day_confirmation_prepares_deterministic_outlook_message_with_reply_code():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        created = _create_service_day_confirmation(client)
        assert created["reply_code"]
        prepared = client.post(
            f"/api/service-confirmations/{created['confirmation_id']}/prepare-message",
            json={"command_id": "prepare-sdc-email"},
        )
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()
        assert body["status"] == "PREPARED"
        assert f"[SSDAY:{created['reply_code']}]" in body["subject"]
        assert "reply CONFIRM" in body["body"]
        listed = client.get("/api/service-confirmations").json()
        row = next(x for x in listed if x["id"] == created["confirmation_id"])
        assert row["reply_code"] == created["reply_code"]
        assert row["status"] == "AWAITING_CONFIRMATION"


def test_outlook_eml_explicit_customer_reply_confirms_service_day_without_ai():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        created = _create_service_day_confirmation(client, "sdc-email-reply")
        raw = (
            "From: service@example.com\r\n"
            "To: service@company.example\r\n"
            f"Subject: Re: Service Day [SSDAY:{created['reply_code']}]\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            "CONFIRM - that day works for us.\r\n"
        ).encode()
        response = client.post(
            "/api/integrations/outlook/import-eml",
            files={"file": ("reply.eml", raw, "message/rfc822")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "CONFIRMED"
        row = next(x for x in client.get("/api/service-confirmations").json() if x["id"] == created["confirmation_id"])
        assert row["status"] == "CONFIRMED"
        assert row["channel"] == "EMAIL_REPLY"
        assert row["reply_received_at"]


def test_outlook_eml_ambiguous_or_wrong_sender_does_not_confirm_service_day():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        created = _create_service_day_confirmation(client, "sdc-email-ambiguous")
        ambiguous = (
            "From: service@example.com\r\n"
            f"Subject: Re: Service Day [SSDAY:{created['reply_code']}]\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            "Thanks for the update.\r\n"
        ).encode()
        r1 = client.post("/api/integrations/outlook/import-eml", files={"file": ("ambiguous.eml", ambiguous, "message/rfc822")})
        assert r1.status_code == 200
        assert r1.json()["state"] == "NEEDS_HUMAN_REVIEW"
        wrong = (
            "From: someone-else@example.com\r\n"
            f"Subject: Re: Service Day [SSDAY:{created['reply_code']}]\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            "CONFIRM\r\n"
        ).encode()
        r2 = client.post("/api/integrations/outlook/import-eml", files={"file": ("wrong.eml", wrong, "message/rfc822")})
        assert r2.status_code == 200
        assert r2.json()["state"] == "NEEDS_HUMAN_REVIEW"
        row = next(x for x in client.get("/api/service-confirmations").json() if x["id"] == created["confirmation_id"])
        assert row["status"] == "AWAITING_CONFIRMATION"


def test_outlook_fastfield_eml_stages_structured_history_for_human_review():
    from email.message import EmailMessage

    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        msg = EmailMessage()
        msg["From"] = "reports@fastfieldforms.com"
        msg["To"] = "service@company.example"
        msg["Subject"] = "FastField Work Order WO-TEST-88"
        msg.set_content("FastField submission attached.")
        csv_data = (
            "customer,work_order,date,serial,technician,complaint,finding,cause,correction,verification\n"
            "Future Ford of Clovis,WO-TEST-88,2026-08-07,DEMO-CL10A-004,Chris Patel,Uneven platform,Left side low,Adjustment drift,Synchronized platforms,Five cycles normal\n"
        )
        msg.add_attachment(csv_data.encode(), maintype="text", subtype="csv", filename="submission.csv")
        response = client.post(
            "/api/integrations/outlook/import-eml",
            files={"file": ("fastfield.eml", msg.as_bytes(), "message/rfc822")},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["type"] == "FASTFIELD"
        assert body["batch_id"]
        batch = client.get(f"/api/import/{body['batch_id']}").json()
        assert batch["batch"]["source_type"] == "FASTFIELD_EMAIL"
        assert batch["rows"]
        row = batch["rows"][0]
        assert row["review_state"] != "HUMAN_APPROVED"
        assert row["normalized"]["source_system"] == "FastField"
        assert row["normalized"].get("ai_used") in (False, 0, "false", None)
        dashboard = client.get("/api/dashboard").json()
        assert dashboard["counts"]["fastfield_review"] >= 1
        assert any(b["id"] == body["batch_id"] for b in dashboard["fastfield_batches"])


def test_outlook_status_never_crashes_when_classic_outlook_is_unavailable():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        response = client.get("/api/integrations/outlook/status")
        assert response.status_code == 200
        assert "available" in response.json()


def test_quiet_tutorial_layer_is_optional_and_not_a_forced_login_tour():
    source = (Path(__file__).parents[1] / "src/serviceslate/static/app.js").read_text(encoding="utf-8")
    assert "maybeWelcomeGuide" in source
    assert "Not now" in source
    assert "Show me" in source
    assert "serviceslate-guides:" in source
    assert "completed" in source


def test_service_day_reply_email_is_idempotent_when_outlook_scans_same_message_again():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        created = _create_service_day_confirmation(client, "sdc-email-idempotent")
        raw = (
            "Message-ID: <same-reply@example.com>\r\n"
            "From: service@example.com\r\n"
            f"Subject: Re: Service Day [SSDAY:{created['reply_code']}]\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            "CONFIRM\r\n"
        ).encode()
        first = client.post("/api/integrations/outlook/import-eml", files={"file": ("reply.eml", raw, "message/rfc822")})
        second = client.post("/api/integrations/outlook/import-eml", files={"file": ("reply.eml", raw, "message/rfc822")})
        assert first.status_code == 200 and second.status_code == 200
        assert second.json().get("already_processed") is True
        # Only one customer-confirmation audit event should be generated for the same message.
        from serviceslate.db import connect
        with connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id='j_sched_future' AND action='CUSTOMER_SERVICE_DAY_CONFIRMED'"
            ).fetchone()[0]
        assert count == 1


def test_local_security_headers_and_database_integrity_health_are_visible():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["database"] == "working"
        assert health.json()["database_integrity"] == "ok"
        assert health.headers["x-content-type-options"] == "nosniff"
        assert health.headers["x-frame-options"] == "DENY"
        assert health.headers["cache-control"] == "no-store"


def test_quoted_service_day_instructions_do_not_turn_confirm_into_change_request():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        created = _create_service_day_confirmation(client, "sdc-quoted-reply")
        raw = (
            "Message-ID: <quoted-reply@example.com>\r\n"
            "From: service@example.com\r\n"
            f"Subject: Re: Service Day [SSDAY:{created['reply_code']}]\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            "CONFIRM - Tuesday works for us.\r\n\r\n"
            "-----Original Message-----\r\n"
            "Please reply CONFIRM if the day works. If you need another day, reply DIFFERENT DAY.\r\n"
        ).encode()
        response = client.post("/api/integrations/outlook/import-eml", files={"file": ("quoted.eml", raw, "message/rfc822")})
        assert response.status_code == 200
        assert response.json()["state"] == "CONFIRMED"


def test_service_day_reply_sender_comparison_requires_exact_email_address():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        created = _create_service_day_confirmation(client, "sdc-exact-sender")
        raw = (
            "Message-ID: <spoofish@example.com>\r\n"
            "From: service@example.com.evil.example\r\n"
            f"Subject: Re: Service Day [SSDAY:{created['reply_code']}]\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\nCONFIRM\r\n"
        ).encode()
        response = client.post("/api/integrations/outlook/import-eml", files={"file": ("sender.eml", raw, "message/rfc822")})
        assert response.status_code == 200
        assert response.json()["state"] == "NEEDS_HUMAN_REVIEW"


def test_v09_readiness_distinguishes_built_from_real_external_activation():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        readiness = client.get("/api/cloud/readiness")
        assert readiness.status_code == 200
        checks = {c["id"]: c for c in readiness.json()["checks"]}
        assert checks["database"]["state"] == "READY_TO_DEPLOY"
        assert checks["drive"]["state"] in {"READY_TO_CONNECT", "PASS"}
        assert checks["legal"]["state"] == "NEEDS_HUMAN"
        assert checks["usability"]["state"] == "NEEDS_HUMAN"
        assert checks["devices"]["state"] == "NEEDS_HUMAN"
        assert checks["signed_installer"]["state"] == "NEEDS_EXTERNAL_CERTIFICATE"


def test_v09_google_drive_desktop_folder_mode_syncs_file_without_becoming_database():
    from tempfile import TemporaryDirectory
    from serviceslate.db import FILES_DIR, connect

    with TemporaryDirectory() as drive_dir, TestClient(app) as client:
        login(client, "manager@demo.example.com")
        configured = client.put(
            "/api/integrations/GOOGLE_DRIVE",
            json={"settings": {"mode": "LOCAL_FOLDER", "local_folder": drive_dir, "sync_files": True}},
        )
        assert configured.status_code == 200, configured.text
        with connect() as conn:
            org = conn.execute("SELECT organization_id FROM users WHERE email='manager@demo.example.com'").fetchone()[0]
            customer = conn.execute("SELECT id FROM customers WHERE organization_id=? ORDER BY name LIMIT 1", (org,)).fetchone()[0]
        content = b"ServiceSlate Google Drive mirror test"
        uploaded = client.post(
            "/api/files",
            data={"entity_type":"customer","entity_id":customer,"category":"DOCUMENT"},
            files={"file": ("mirror-test.txt", content, "text/plain")},
        )
        assert uploaded.status_code == 200, uploaded.text
        body = uploaded.json()
        mirrored = list(Path(drive_dir).rglob("*mirror-test.txt"))
        assert mirrored and mirrored[0].read_bytes() == content
        with connect() as conn:
            stored = conn.execute("SELECT stored_name FROM file_records WHERE id=?", (body["id"],)).fetchone()[0]
        local_path = FILES_DIR / org / stored
        assert local_path.exists() and local_path.resolve().is_relative_to(FILES_DIR.resolve())
        local_path.unlink()
        restored = client.get(f"/api/files/{body['id']}/download")
        assert restored.status_code == 200
        assert restored.content == content
        assert local_path.exists()


def test_v09_multi_tenant_host_will_not_copy_whole_database_to_one_company_drive():
    from serviceslate.cloud_connectors import organization_database_backup_is_safe_for_offsite
    from serviceslate.db import connect, new_id, utcnow
    with connect() as conn:
        now = utcnow()
        conn.execute("INSERT INTO organizations(id,name,profile,is_demo,created_at) VALUES(?,?,?,?,?)", (new_id('org'), 'Live One', 'automotive_equipment', 0, now))
        second = new_id('org')
        conn.execute("INSERT INTO organizations(id,name,profile,is_demo,created_at) VALUES(?,?,?,?,?)", (second, 'Live Two', 'automotive_equipment', 0, now))
    allowed, reason = organization_database_backup_is_safe_for_offsite(second)
    assert allowed is False
    assert "host-managed" in reason


def test_v09_sales_lifecycle_reaches_installed_equipment_and_commissioning():
    from serviceslate.db import connect
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        with connect() as conn:
            customer = conn.execute("SELECT id FROM customers WHERE organization_id='org_auto_demo' ORDER BY name LIMIT 1").fetchone()[0]
            location = conn.execute("SELECT id FROM locations WHERE organization_id='org_auto_demo' AND customer_id=? LIMIT 1", (customer,)).fetchone()[0]
        opp = client.post("/api/sales/opportunities", json={"command_id":"v09-opp","customer_id":customer,"location_id":location,"title":"New lift package","expected_value_cents":2500000})
        assert opp.status_code == 200, opp.text
        opp_id = opp.json()["id"]
        order = client.post(f"/api/sales/opportunities/{opp_id}/orders", json={"command_id":"v09-order","vendor":"Challenger","order_number":"PO-DEMO-9"})
        assert order.status_code == 200, order.text
        order_id = order.json()["id"]
        project = client.post(f"/api/sales/orders/{order_id}/installation", json={"command_id":"v09-project","location_id":location,"name":"Lift installation"})
        assert project.status_code == 200, project.text
        project_id = project.json()["id"]
        work = client.post(f"/api/sales/projects/{project_id}/job", json={"command_id":"v09-install-job","description":"Install and commission lift"})
        assert work.status_code == 200, work.text
        equipment = client.post(f"/api/sales/projects/{project_id}/installed-equipment", json={"command_id":"v09-equip","category":"vehicle lift","manufacturer":"Challenger","model":"CL10A","serial_number":"V09-DEMO-SERIAL"})
        assert equipment.status_code == 200, equipment.text
        commissioned = client.post(f"/api/sales/projects/{project_id}/commission", json={"command_id":"v09-commission","result":"PASS","job_id":work.json()["job_id"],"equipment_id":equipment.json()["id"],"customer_ack_name":"Demo Customer"})
        assert commissioned.status_code == 200, commissioned.text
        assert commissioned.json()["lifetime_service_ready"] is True
        pipeline = client.get("/api/sales/pipeline").json()
        row = next(x for x in pipeline["opportunities"] if x["id"] == opp_id)
        assert row["stage"] == "WON"


def test_v09_admin_can_unlock_and_issue_a_forced_change_temporary_password():
    from serviceslate.db import connect
    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        team = client.get("/api/team").json()
        target = next(x for x in team if x["role"] == "TECHNICIAN")
        reset = client.post(f"/api/security/users/{target['id']}/reset-password", json={"temporary_password":"TemporaryPass!2026","require_change":True})
        assert reset.status_code == 200, reset.text
        with connect() as conn:
            row = conn.execute("SELECT force_password_change,failed_login_count,locked_until FROM users WHERE id=?", (target["id"],)).fetchone()
        assert row["force_password_change"] == 1
        assert row["failed_login_count"] == 0 and row["locked_until"] is None


def test_v09_business_enrichment_never_auto_commits_a_customer(monkeypatch):
    import serviceslate.cloud_connectors as cc
    monkeypatch.setattr(cc, "_website_public_facts", lambda url: {"website": url, "page_title": "Example", "phones": ["555-1000"], "emails": []})
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        before = len(client.get("/api/customers").json())
        result = client.post("/api/cloud/business-enrichment", json={"business_name":"Potential Customer","website":"https://example.invalid"})
        assert result.status_code == 200
        assert result.json()["requires_human_review"] is True
        assert result.json()["auto_commit"] is False
        after = len(client.get("/api/customers").json())
        assert before == after


def test_v09_public_base_url_adds_secure_confirmation_link_without_changing_service_day_rule():
    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        settings = client.put("/api/cloud/readiness/settings", json={"public_base_url":"https://service.example.com","backup_retention_days":30,"production_mode":False})
        assert settings.status_code == 200, settings.text
        created = _create_service_day_confirmation(client, "v09-public-link")
        prepared = client.post(f"/api/service-confirmations/{created['confirmation_id']}/prepare-message", json={"command_id":"v09-public-message"})
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()["body"]
        assert "https://service.example.com" in body
        assert "scheduled service day" in body.lower() or "service day" in body.lower()


def test_v09_postgres_qmark_translation_preserves_conflict_semantics():
    from serviceslate.db import _postgres_sql
    sql = _postgres_sql("INSERT OR IGNORE INTO sample(id,name) VALUES(?,?)")
    assert "%s" in sql
    assert "ON CONFLICT DO NOTHING" in sql


def test_v09_microsoft_company_sign_in_never_auto_creates_unknown_users():
    from serviceslate.db import connect
    with TestClient(app) as client:
        with connect() as conn:
            before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        result = client.post("/api/security/microsoft/start", json={"email":"unknown-person@example.com"})
        assert result.status_code == 400
        with connect() as conn:
            after = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        assert before == after


def test_v09_update_channel_is_truthful_when_not_configured():
    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        status = client.get("/api/updates/status")
        assert status.status_code == 200
        assert status.json()["state"] == "NOT_CONFIGURED"
        assert status.json()["configured"] is False


def _soap_call(client, method: str, inner: str = ""):
    payload = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body>'
        f'<{method} xmlns="http://developer.intuit.com/">{inner}</{method}>'
        '</soap:Body></soap:Envelope>'
    )
    return client.post('/api/cloud/quickbooks/desktop/soap', content=payload.encode(), headers={'content-type':'text/xml'})


def test_v09_quickbooks_desktop_web_connector_is_a_real_human_approved_queue():
    import xml.etree.ElementTree as ET
    from serviceslate.db import connect

    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        configured = client.put(
            "/api/integrations/QUICKBOOKS",
            json={
                "settings": {
                    "mode":"DESKTOP_WEB_CONNECTOR",
                    "web_connector_url":"https://service.example.com",
                    "web_connector_username":"serviceslate-demo",
                    "desktop_service_item_name":"Service Labor",
                },
                "secrets":{"web_connector_password":"WebConnectorPass!2026"},
                "enabled":True,
            },
        )
        assert configured.status_code == 200, configured.text

        qwc = client.get('/api/cloud/quickbooks/desktop/qwc')
        assert qwc.status_code == 200, qwc.text
        assert 'https://service.example.com/api/cloud/quickbooks/desktop/soap' in qwc.text
        assert '<UserName>serviceslate-demo</UserName>' in qwc.text

        queued = client.post('/api/cloud/quickbooks/desktop/jobs/j_two/queue-invoice', json={'confirm':True})
        assert queued.status_code == 200, queued.text
        queue_id = queued.json()['queue_id']

        auth = _soap_call(client, 'authenticate', '<strUserName>serviceslate-demo</strUserName><strPassword>WebConnectorPass!2026</strPassword>')
        assert auth.status_code == 200
        root = ET.fromstring(auth.text)
        strings = [el.text or '' for el in root.iter() if el.tag.rsplit('}',1)[-1] == 'string']
        assert len(strings) == 2 and strings[0]
        ticket = strings[0]

        request_xml = _soap_call(client, 'sendRequestXML', f'<ticket>{ticket}</ticket>')
        assert request_xml.status_code == 200
        request_root = ET.fromstring(request_xml.text)
        result = next(el for el in request_root.iter() if el.tag.rsplit('}',1)[-1] == 'sendRequestXMLResult')
        assert '<InvoiceAddRq' in (result.text or '')
        assert 'WO-26103' in (result.text or '')

        qb_response = (
            '<?xml version="1.0"?><QBXML><QBXMLMsgsRs>'
            '<InvoiceAddRs requestID="j_two" statusCode="0" statusSeverity="Info" statusMessage="Status OK">'
            '<InvoiceRet><TxnID>QB-TXN-9001</TxnID><RefNumber>WO-26103</RefNumber></InvoiceRet>'
            '</InvoiceAddRs></QBXMLMsgsRs></QBXML>'
        )
        from xml.sax.saxutils import escape
        received = _soap_call(
            client,
            'receiveResponseXML',
            f'<ticket>{ticket}</ticket><response>{escape(qb_response)}</response><hresult></hresult><message></message>',
        )
        assert received.status_code == 200
        assert '>100<' in received.text
        with connect() as conn:
            row = conn.execute('SELECT status,external_id FROM quickbooks_desktop_queue WHERE id=?', (queue_id,)).fetchone()
            link = conn.execute("SELECT external_id FROM integration_entity_links WHERE organization_id='org_auto_demo' AND provider='QUICKBOOKS' AND entity_type='job_invoice' AND entity_id='j_two'").fetchone()
        assert row['status'] == 'COMPLETED'
        assert row['external_id'] == 'QB-TXN-9001'
        assert link['external_id'] == 'QB-TXN-9001'


def test_v09_quickbooks_desktop_rejects_bad_web_connector_password():
    import xml.etree.ElementTree as ET
    with TestClient(app) as client:
        login(client, "manager@demo.example.com")
        client.put(
            "/api/integrations/QUICKBOOKS",
            json={
                "settings": {"mode":"DESKTOP_WEB_CONNECTOR","web_connector_username":"serviceslate-demo","desktop_service_item_name":"Service Labor"},
                "secrets":{"web_connector_password":"CorrectPass!2026"},
                "enabled":True,
            },
        )
        auth = _soap_call(client, 'authenticate', '<strUserName>serviceslate-demo</strUserName><strPassword>WrongPassword</strPassword>')
        root = ET.fromstring(auth.text)
        strings = [el.text or '' for el in root.iter() if el.tag.rsplit('}',1)[-1] == 'string']
        assert strings == ['', 'nvu']


def test_v09_update_manifest_accepts_installer_next_to_https_manifest(monkeypatch):
    import serviceslate.updates as updates

    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            return b'{"version":"1.0.0","sha256":"abc123","installer":"ServiceSlate-Setup-1.0.0.exe"}'

    monkeypatch.setattr(updates, "_manifest_url", lambda: "https://releases.example.com/serviceslate/update-manifest.json")
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    manifest = updates.fetch_manifest()
    assert manifest["installer_url"] == "https://releases.example.com/serviceslate/ServiceSlate-Setup-1.0.0.exe"


def test_lan_internal_messages_and_presence_are_org_scoped():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        hb = client.post("/api/lan/heartbeat", json={"device_id": "test-device-001", "device_label": "Front Desk PC"})
        assert hb.status_code == 200
        presence = client.get("/api/lan/presence")
        assert presence.status_code == 200
        assert any(p["user_id"] == "u_coord" and p["online"] for p in presence.json())

        sent = client.post("/api/lan/messages", json={"recipient_user_id": "u_chris", "body": "Please check WO-26103 before lunch."})
        assert sent.status_code == 200

    with TestClient(app) as tech:
        login(tech, "tech.chris@demo.example.com")
        inbox = tech.get("/api/lan/messages")
        assert inbox.status_code == 200
        msg = next(m for m in inbox.json() if m["body"] == "Please check WO-26103 before lunch.")
        assert msg["sender_name"]
        marked = tech.post(f"/api/lan/messages/{msg['id']}/read")
        assert marked.status_code == 200
        assert next(m for m in tech.get("/api/lan/messages").json() if m["id"] == msg["id"])["read_at"]


def test_lan_message_rejects_cross_organization_recipient():
    with TestClient(app) as client:
        login(client, "coordinator@demo.example.com")
        r = client.post("/api/lan/messages", json={"recipient_user_id": "u_reception", "body": "This must not cross organizations."})
        assert r.status_code == 404
