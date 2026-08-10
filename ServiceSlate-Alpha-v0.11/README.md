# ServiceSlate Alpha v0.11

**Operational Memory Software**

ServiceSlate connects customers, equipment, scheduling, field paperwork, approvals, communications, parts, recurring work and permanent service history so information is entered once and remains useful later.

> **Technician records facts once → ServiceSlate saves/connects/checks them → coordinator reviews exceptions → future staff inherit useful history.**

## Run locally

On Windows, the lowest-friction Alpha path remains:

1. Extract the release ZIP.
2. Double-click **INSTALL_SERVICESLATE.cmd**.
3. Open the ServiceSlate shortcut.
4. Use a fictional demo role or choose **Set up my business**.

No dedicated server, database administration or paid API is required for the local Alpha.

## What v0.9 adds

v0.9 adds the real-company path without making any external service mandatory:

- Google Drive for company documents and off-site backups, including an easy **Drive for Desktop folder** mode and a direct Shared Drive API mode.
- File recovery from the configured company Drive/object-storage mirror if a local application copy is missing.
- A production-shaped self-hosted stack using PostgreSQL, Caddy HTTPS, MinIO/S3-compatible storage and ClamAV.
- A guided Linux host bootstrap plus a SQLite → PostgreSQL migration utility.
- Microsoft Graph support for New Outlook/Microsoft 365 mail intake, outbound email and two-way calendar synchronization with human-reviewed conflicts.
- Company Microsoft sign-in for existing ServiceSlate users; the company's Entra policy can enforce MFA/passkeys instead of ServiceSlate inventing its own passkey system.
- Public HTTPS customer confirmation links when a real hosted address is configured; explicit email-reply confirmation still works without public hosting.
- Automated Twilio SMS when a company chooses a paid provider; paired-phone/carrier SMS remains available with no SMS API.
- QuickBooks Online OAuth/invoice handoff and a real QuickBooks Desktop Web Connector queue/response endpoint when the hosted QuickBooks administrator authorizes it.
- Deterministic public-business enrichment suggestions with human review before customer creation.
- Automotive sales lifecycle: opportunity → quote → equipment order → installation → installed equipment → commissioning → lifetime service identity.
- Production authentication controls: TOTP MFA, lockout, forced temporary-password change, admin recovery/unlock and optional Microsoft company sign-in.
- Production web controls for HTTPS deployments: secure cookies, CSRF, rate limits, trusted hosts, HSTS/CSP and upload malware scanning.
- Structured JSON logs, CI quality/security workflows, scheduled PostgreSQL backup automation, restore runbook/drill script and load smoke testing.
- Standalone Windows/PyInstaller + NSIS release pipeline, Authenticode signing hook, verified update channel and optional verified rollback package support.
- Production Readiness evidence for legal/business wording approval, zero-training human tests and real field-device tests. These are deliberately **not self-certified by software**.
- The current ServiceSlate brand emblem is now used in the application/PWA itself.


## v0.11 premium UI / UX refinement

v0.11 keeps the v0.10 Office Network and production-foundation behavior while refining the real application interface:

- premium graphite/slate visual system with restrained cobalt-metal accents
- stronger information hierarchy across dashboards, queues, tables and record drawers
- clearer active navigation and system-state presentation
- refined forms, buttons, search, dialogs, drawers and notifications
- denser but more readable operational tables and status pills
- improved mobile/touch spacing and bottom navigation
- more polished login and first-run presentation
- neutral workspace connection language for standalone, LAN-hosted and joined workstations
- reduced-motion support and retained keyboard/focus accessibility

No workflow or data-model behavior is intentionally changed by this visual pass.

## Google Drive rule

Google Drive is **not the ServiceSlate database**.

Use Drive for:

- customer/job/equipment documents
- manuals
- reports
- photos/files where appropriate
- off-site application backup copies for a single-company/local deployment

Structured operational data remains in SQLite locally or PostgreSQL when hosted. On a multi-company host, full database backups follow host-level backup policy rather than being copied into one organization's Drive.

## Human authority / deterministic behavior

ServiceSlate is rules-first and AI-independent.

- AI is off by default and not required for any correct core workflow.
- Software-extracted consequential information remains **Awaiting Human Review**.
- Human-authored entries already have an accountable author.
- Safety, inspection, estimate, authorization, schedule commitment, job completion/cancellation and permanent-history decisions cannot be silently authored by AI.
- FastField/Outlook intake prefers structured attachments and deterministic matching before any document/OCR fallback.

## Production hosting

The local Alpha still works with no server. When the company wants technicians/coordinators to sync from anywhere continuously, use `deploy/` on a company-approved always-available host.

The included stack provides:

- PostgreSQL structured data
- HTTPS through Caddy
- S3-compatible file mirror through MinIO
- ClamAV upload scanning
- scheduled PostgreSQL backups
- separate application/data boundaries

See `deploy/README.md` and `docs/POSTGRES_MIGRATION.md`.

## Important external activation boundaries

ServiceSlate contains the connector/workflow, but these still require real organization inputs:

- Google API mode: company-approved Google OAuth application, unless using Drive for Desktop folder mode.
- Microsoft Graph/company SSO: company tenant/application approval.
- QuickBooks Online: Intuit application credentials/company authorization.
- QuickBooks Desktop: hosted QuickBooks administrator approval for Web Connector.
- Automated SMS: provider account if the company wants server-side SMS/delivery receipts.
- Public remote access: an actual host/domain/DNS.
- Signed Windows releases: a real code-signing certificate.
- Legal readiness: a real human/business/legal review.
- Zero-training and phone/tablet readiness: real people and real devices must perform the tests.

ServiceSlate records those facts; it does not fabricate them.

## Deliberate exclusions

ServiceSlate does not contain GPS tracking, dash-cam data, route replay, speed monitoring, geofencing, driver scoring or continuous employee-location history.

It also is not payroll, full accounting, payment processing, a marketing-automation suite or a full ERP.

## Verification

The release is verified with the repository's behavioral tests plus Python/JavaScript syntax, database integrity and ZIP integrity checks before packaging. CI additionally defines Ruff, basedpyright, ty, coverage, Bandit, pip-audit, build and restore-drill gates for a real repository runner.

## Office LAN mode

ServiceSlate can share one authoritative workspace across computers on the same trusted office Ethernet/Wi-Fi network without requiring internet access.

1. Install ServiceSlate on the computer that will remain available in the office.
2. Run `CONFIGURE_OFFICE_NETWORK.cmd` and choose **This computer is the Office Host**.
3. Restart ServiceSlate. Open **Office Network** to see the join address (for example `http://192.168.1.20:8765`).
4. On another installed ServiceSlate computer, run `CONFIGURE_OFFICE_NETWORK.cmd`, choose **Join an existing Office Host**, and enter that address.
5. Start ServiceSlate normally. The workstation opens the Office Host and uses its database/files as the source of truth.

Joined computers share customers, equipment, jobs, scheduling, files, submissions, sales/service records, and other organization data immediately because they use the same backend. The Office Network screen also provides internal direct messages and office-wide announcements.

LAN mode deliberately does **not** synchronize separate SQLite databases. That avoids conflicting edits and split history. If the Office Host is unreachable, a joined workstation does not silently open a competing company database; field-browser drafts/outbox continue protecting unsent technician work where supported.

Use LAN mode only on a trusted private network. For access across the public internet, use the hardened HTTPS/PostgreSQL deployment instead.
