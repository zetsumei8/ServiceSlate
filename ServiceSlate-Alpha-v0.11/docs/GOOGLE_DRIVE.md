# ServiceSlate + Company Google Drive

The company can keep using its established Google Drive. ServiceSlate does not turn Drive into a database.

## Easiest mode: Google Drive for Desktop
If the company PC already has a Drive/Shared Drive folder mounted by Google Drive for Desktop, choose that folder in **Tools & Connections → Company Drive**. ServiceSlate can mirror documents/files and backups into it with no ServiceSlate-specific Google API registration.

## Direct Shared Drive mode
For a hosted ServiceSlate instance, an administrator can configure a company-approved Google OAuth application and connect a Shared Drive/folder through the Drive API.

## What belongs in Drive
- manuals and technical documents
- customer/job/equipment attachments
- reports and generated documents
- file/photo mirrors where appropriate
- off-site local/single-company backup copies

## What does not belong in Drive
The live SQLite/PostgreSQL database. A synchronized `.db` file is not a safe multi-user database strategy.

On a multi-company hosted database, whole-database backup is host-level and must not be copied into one tenant's Drive.
