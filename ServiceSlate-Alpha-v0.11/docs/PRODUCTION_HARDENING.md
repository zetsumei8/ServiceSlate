# ServiceSlate v0.9 — Production Hardening & Activation Gate

v0.9 contains a production-shaped deployment and integration path, but software cannot self-certify company credentials, legal approval, human usability or real-device behavior.

## Built into v0.9
- PostgreSQL backend plus SQLite→PostgreSQL migration utility.
- Caddy HTTPS reverse proxy and trusted-host/secure-cookie production settings.
- CSRF and public/login rate limiting.
- HSTS/CSP/security headers.
- encrypted connector secret vault with host master-key support.
- ClamAV upload scanning option/requirement.
- MinIO/S3-compatible file mirroring and restore-on-read.
- scheduled PostgreSQL dump + checksum + S3-compatible backup upload.
- database backup/restore runbook and automated restore-drill script.
- structured request/application logs.
- local TOTP MFA and Microsoft-company-sign-in route.
- Microsoft Graph mail/calendar integration with conflict review.
- Google Drive document/backup mirror.
- QuickBooks Online + authorized Desktop Web Connector routes.
- verified Windows update channel plus rollback-package support.
- CI definitions for lint, formatting, typing, tests/coverage, Bandit, dependency audit, restore drill and build.
- production readiness evidence registry.

## Must be supplied/verified by the organization before production acceptance
- a real host/domain/DNS and valid HTTPS reachability.
- unique production database/storage/session/master-key credentials.
- true off-site backup destination plus a successful restore drill.
- approved Google/Microsoft/QuickBooks/SMS credentials as applicable.
- Microsoft/Entra least-privilege and account lifecycle review.
- code-signing certificate and signed release verification.
- legal/business approval of the actual customer-facing wording.
- zero-training acceptance with real first-time users.
- field-device acceptance on actual phones/tablets.
- production load test on expected concurrency/data size.
- security/authorization/tenant-isolation review against the chosen deployment.
- alerting/incident ownership and retention policy.

## Release rule
A hardening change may strengthen infrastructure/security but may not silently change customer commitments, technician-history privacy, human authority, deterministic-first behavior, profile isolation or the no-tracking boundary.
