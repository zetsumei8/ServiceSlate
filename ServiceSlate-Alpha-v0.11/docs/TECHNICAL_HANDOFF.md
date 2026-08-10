# ServiceSlate v0.9 — Technical Handoff

## Product boundary
This repository implements the ServiceSlate contract in `TOTAL_CONTRACT.md`. Production hardening may strengthen infrastructure but must not silently redesign approved behavior. If a real contradiction is discovered, produce **Decision Needed** instead of guessing.

## Runtime / clients
- Python 3.11+ / FastAPI
- SQLite local authoritative DB or PostgreSQL hosted authoritative DB
- local attachment store plus optional mirrored company storage
- vanilla JavaScript responsive SPA
- IndexedDB field cache/drafts/outbox
- optional PWA shell
- local launcher binds to 127.0.0.1

Core modules:
- `app.py` — sessions/API/security middleware
- `db.py` — schema/migrations/audit/idempotency/SQLite+PostgreSQL
- `features.py` — operational workflows
- `sales.py` — opportunity/order/install/commission lifecycle
- `security.py` — TOTP/recovery/Microsoft company sign-in
- `cloud_connectors.py` — Drive, Graph, calendar, QuickBooks, enrichment, readiness
- `integrations.py` — provider registry/secrets/transports
- `governance.py` — deterministic/human-review provenance
- `outlook_bridge.py` / `mail_intake.py` — Classic Outlook and deterministic inbound mail
- `updates.py` — verified update/rollback channel
- `tools.py` — role/profile-filtered Toolbox

## Persistence invariants
- stable permanent IDs;
- current row + history/audit, not clone-on-edit;
- optimistic record versions;
- idempotent retry-prone commands;
- immutable signed/submitted/revision evidence;
- migration backup/recovery boundaries;
- SQLite→PostgreSQL migration preserves IDs and behavior.

## Field sync
IndexedDB is a durable field buffer, not final archive. Save locally → durable operation ID → outbox → transmit → idempotent backend apply → remove only after acknowledgement. Validation/permission conflicts are Needs Attention, not “offline.” Cache/outbox remains organization+user scoped.

## Storage / Google Drive
Google Drive is never the relational database. `cloud_connectors.py` supports:
- existing Google Drive for Desktop folder mirror;
- direct Google Drive/Shared Drive OAuth API;
- file/backup mirrors;
- verified restore-on-read when a local file is absent.

Hosted file storage can use S3-compatible storage. The included Docker stack supplies MinIO. Multi-tenant hosts may not send the whole shared database backup into a single tenant's Drive.

## Hosted database/deployment
`deploy/` supplies PostgreSQL + Caddy + MinIO + upload scan + DB backup worker. `deploy/SETUP_HOST.sh` provides the guided bootstrap. `scripts/migrate_sqlite_to_postgres.py` validates source integrity, creates a safety copy, creates the PostgreSQL schema, preserves IDs and verifies counts.

## Microsoft boundary
Classic Outlook remains an interactive Windows adapter. Microsoft Graph is the New Outlook/hosted path for delegated mail/calendar access. Mail uses incremental state. Calendar writes ServiceSlate→Outlook; material Outlook changes produce `calendar_conflicts`. Outlook may not silently cancel/move operational work.

Company Microsoft sign-in accepts only existing active ServiceSlate users. Organization Entra can enforce MFA/passkeys; ServiceSlate does not invent passkey cryptography.

## QuickBooks boundary
- export-only is always available;
- QBO uses OAuth and human-triggered invoice creation from approved estimate data;
- Desktop Web Connector requires a hosted HTTPS ServiceSlate URL and admin authorization;
- `.qwc` points to the implemented SOAP route;
- invoices are queued only after a human action;
- QuickBooks TxnID/errors are reconciled into ServiceSlate integration evidence;
- no permission bypass.

## Customer awareness
Normal commitment is service day. Sent/opened/read never equals explicit acknowledgement. Schedule changes stale the old confirmation. Hosted HTTPS enables public one-click links; email reply/phone/in-person remain valid channels.

## Production security
Production mode enables secure cookies, HTTPS/trusted-host assumptions, CSRF, public/login rate limits, HSTS/CSP and stricter connector-secret behavior. ClamAV can be required for public uploads. Internet deployment still needs real infrastructure/identity/security acceptance.

## Windows distribution/update
- local `.cmd`/PowerShell bootstrap remains available;
- PyInstaller + NSIS build definitions are included;
- Windows GitHub Action can sign using organization PFX secrets;
- runtime verifies SHA-256 and Authenticode for update installers unless explicitly in an approved unsigned development mode;
- optional release-manifest rollback package can be preserved and launched;
- application rollback never implicitly restores an old database.

## Human evidence boundary
`legal_approvals`, `usability_sessions`, and `device_readiness_reports` record real human evidence. Never auto-fill these as “pass.” Native mobile remains conditional on actual field-device findings.

## Verification
```bash
python -m compileall -q src
node --check src/serviceslate/static/app.js
node --check src/serviceslate/static/sw.js
PYTHONPATH=src pytest -q
git diff --check
PYTHONPATH=src python scripts/restore_drill.py
```

CI additionally defines Ruff, basedpyright, ty, coverage, Bandit, pip-audit and package-build gates. Do not claim a gate ran locally if the packaging environment did not provide that tool.
