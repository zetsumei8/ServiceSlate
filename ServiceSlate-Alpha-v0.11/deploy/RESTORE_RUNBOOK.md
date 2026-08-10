# Hosted restore runbook

1. Put ServiceSlate into maintenance mode and record the incident/request ID.
2. Verify the target backup checksum and manifest before touching production data.
3. Snapshot the current PostgreSQL volume/database as a pre-restore safety copy.
4. Restore the selected `pg_dump` into a new database first; never overwrite the only live copy in place.
5. Run schema/version checks and application smoke tests against the restored database.
6. Point ServiceSlate at the restored database only after validation.
7. Verify representative customers, jobs, equipment history, approvals and audit events.
8. Record who approved the restore, which backup was used, and the validation result.
9. Keep the pre-restore copy until the organization's retention policy allows removal.

A restore is not considered tested merely because backups exist. Run this procedure on a non-production target on the organization's chosen schedule.
