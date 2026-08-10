# ServiceSlate Alpha v0.9 — What Is Real vs. What Still Requires Activation

## Live in the codebase now

### Core product
- Automotive Equipment Sales & Service profile and isolated Pet Grooming profile.
- Real SQLite persistence locally; PostgreSQL backend supported for hosted deployment.
- Stable record IDs, audit history, idempotent commands and optimistic concurrency.
- Customers, contacts, locations, equipment, service history, jobs/visits, estimates, forms, inspections, parts, recurring plans, recommendations, follow-ups, vehicles and soft truck stock.
- Coordinator Assist and deterministic Compass scheduling/crew/geographic-affinity logic.
- Technician Companion with durable IndexedDB drafts/cache/outbox and approved historical context.
- Technician submission → coordinator review → permanent operational history.
- Service-day customer awareness/confirmation with stale-on-reschedule behavior.
- Human-accountability provenance and AI-disabled/deterministic-first governance.
- FastField/NoteWise/customer import staging with human review.
- Sales & Install lifecycle through installed equipment and commissioning.
- Optional just-in-time tutorials.
- 29-tool Toolbox/device/interop helpers.

### Google Drive / storage
- Google Drive for Desktop folder mirror mode.
- Direct Google Drive OAuth/Shared Drive API connector.
- Document/file and backup mirroring without using Drive as the live database.
- Restore-on-read if the local file is missing but a verified mirrored copy exists.
- S3-compatible production file mirror; hosted stack includes MinIO.
- Multi-tenant guard preventing one organization from receiving a whole shared-host database backup.

### Shared hosting / PostgreSQL
- Docker Compose stack for PostgreSQL + ServiceSlate + Caddy HTTPS + MinIO + scheduled database backup.
- Guided `deploy/SETUP_HOST.sh` for a normal Linux Docker host.
- SQLite integrity validation and `scripts/migrate_sqlite_to_postgres.py` for preserving business IDs during migration into a fresh PostgreSQL ServiceSlate database.
- Hosted restore runbook and automated restore-drill script.

### Microsoft / Outlook
- Existing Classic Outlook local bridge remains available on Windows.
- Microsoft Graph connector for New Outlook/Microsoft 365.
- Mail scanning through Graph using incremental mailbox state.
- Outbound Microsoft 365 email.
- Two-way Outlook calendar synchronization.
- External Outlook changes become reviewable conflicts instead of silently overwriting ServiceSlate.
- Company Microsoft sign-in for existing authorized ServiceSlate accounts; no automatic user creation.

### Customer communications
- Local Outlook/email-reply service-day acknowledgement.
- Public HTTPS confirmation links when a hosted public base URL exists.
- Read/open evidence does not equal customer confirmation.
- Paired-phone/carrier SMS handoff.
- Direct Twilio SMS adapter with provider delivery/failure callback state when configured.
- Open/self-hosted notification options remain available under Advanced connections.

### QuickBooks
- Neutral accounting export always available.
- QuickBooks Online OAuth connection and human-triggered invoice creation from an approved estimate.
- QuickBooks Desktop Web Connector `.qwc` generation for an HTTPS hosted ServiceSlate instance.
- Human-approved invoice queue for QuickBooks Desktop.
- Web Connector SOAP authentication/request/response handling and QuickBooks TxnID/error reconciliation.
- Core ServiceSlate records remain valid if QuickBooks rejects or cannot process a request.

### Production auth/security/operations
- TOTP authenticator MFA.
- Password minimums, failed-login lockout, admin unlock/recovery and forced change of temporary passwords.
- Optional company Microsoft identity path so Entra can enforce organization MFA/passkey policy.
- Secure-cookie/HTTPS production mode, CSRF, rate limits, trusted hosts, HSTS/CSP and baseline browser security headers.
- Encrypted connector vault with production master-key requirement.
- Optional required ClamAV upload scan.
- Structured rotating application logs with request IDs.
- CI definitions for lint/type/test/coverage/security/build/restore checks.
- Scheduled PostgreSQL backup container and external S3-compatible off-site target option.
- Load-smoke and restore-drill scripts.

### Windows distribution/update
- Existing local Windows bootstrap remains usable.
- PyInstaller standalone build specification.
- NSIS Setup EXE specification.
- GitHub Actions Windows release workflow.
- Optional Authenticode signing from organization certificate secrets.
- HTTPS update manifest, SHA-256 verification and Windows signature verification.
- Pre-update database safety backup.
- Optional verified previous-version installer can be preserved and launched as application rollback; database rollback remains a separate explicit restore decision.

### Production readiness evidence
- Managers can record exact customer wording approval and its hash/version.
- Managers can record actual zero-training test results/friction.
- Real field devices can record offline/reconnect/camera readiness evidence.
- Native mobile is intentionally classified as not required until those field tests prove a browser/PWA limitation.

## External activation or real-world evidence still required

These cannot truthfully be completed inside a downloadable Alpha by itself:

1. **A real always-available host/domain** if the company wants live remote sync/public links.
2. **Company Google authorization** for direct Shared Drive API mode; Drive for Desktop folder mode can be used without creating a ServiceSlate Google API app.
3. **Company Microsoft tenant/application approval** for Graph/new Outlook/company SSO.
4. **QuickBooks administrator/Intuit authorization** for whichever live QuickBooks route the company permits.
5. **An SMS provider account** only if automated server-side SMS/delivery receipts are desired.
6. **A real Windows code-signing certificate** before calling the Setup EXE professionally signed.
7. **A real legal/business review** of disclosure, authorization and e-sign wording.
8. **Actual zero-training sessions** with first-time coordinator/manager/technician users.
9. **Actual Android/iPhone/tablet field tests** on the devices the company will use.
10. **An external off-site backup destination and alert destination** if the organization wants disaster protection beyond the bundled host itself.
11. **Production load/security/restore acceptance on the chosen infrastructure** before company-wide rollout.

## Native mobile decision

Do not build a separate Android/iOS app merely because it sounds more complete. The responsive web/PWA client already uses durable local storage and sync semantics. Build a native client only after real field-device testing identifies a capability/reliability problem that cannot be solved reasonably in the browser. Any native client must use the same ServiceSlate APIs, IDs, permissions and domain rules.
