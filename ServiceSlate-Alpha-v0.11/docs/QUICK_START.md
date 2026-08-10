# ServiceSlate v0.9 — Quick Start

## Local Alpha / manager demo
1. On Windows, double-click **INSTALL_SERVICESLATE.cmd**.
2. Open the new ServiceSlate shortcut.
3. Choose a demo role or **Set up my business**.
4. Use the optional three-step orientation or choose **Not now**.

No dedicated server is required for this mode.

## Normal office day
Start on **Coordinator Assist**. It surfaces work that actually needs a person: new requests, scheduling, customer acknowledgement, FastField review, parts, recurring work, technician approval and billing readiness.

Use global search instead of hunting through menus.

## Company Google Drive
For the easiest real-company setup, if Google Drive for Desktop is already installed:
1. Open **Tools & Connections → Company Drive**.
2. Choose the existing company/Shared Drive folder.
3. Turn on document/file mirroring and/or off-site backup copying as appropriate.

ServiceSlate keeps its database separate from Drive.

## Outlook / FastField
- Classic Outlook: use the local Windows Outlook connection.
- New Outlook/hosted Microsoft 365: connect the Microsoft Graph option after company approval.
- FastField email remains intake only. Review extracted facts before **Approve & Add Ready History**.

## Customer service-day confirmation
ServiceSlate promises the scheduled **day** unless a human explicitly creates a narrower time commitment. Send the approved notice by Outlook/SMS and collect explicit confirmation by reply, secure public link (hosted), phone or in-person.

Opened/read does not equal confirmed. Rescheduling makes the old confirmation stale.

## Field work
Technician opens **My Work**, selects the assignment, reviews approved context/history, records work once and submits. Drafts are stored on-device and queued until ServiceSlate acknowledges synchronization.

## Moving online later
When the company wants live remote synchronization:
1. provision a normal Linux Docker host/domain;
2. run `deploy/SETUP_HOST.sh`;
3. migrate the SQLite database with `scripts/migrate_sqlite_to_postgres.py`;
4. run Production Readiness and a restore drill;
5. connect company Google/Microsoft/QuickBooks services as approved.

## Help
Use the small **? Help** control. Guides are short, optional and do not repeat after completion.
