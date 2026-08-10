# ServiceSlate Shared Hosting

The local laptop Alpha does not need a server. Use this stack only when the company wants an always-available ServiceSlate for live remote technician/coordinator synchronization and public customer links.

## Open-source/default components
- PostgreSQL — shared structured data
- Caddy — HTTPS/reverse proxy
- MinIO — S3-compatible application file mirror/cache
- ClamAV — uploaded-file malware scanning
- scheduled `pg_dump` backup worker

Google Drive can remain the human-friendly company document/off-site integration. It is not the live database.

## Guided start
On a normal Linux Docker host with DNS already pointed to it:

```bash
cd deploy
./SETUP_HOST.sh
```

The script generates private database/storage/session/master-key values, writes `deploy/.env`, and starts the stack.

Or configure `.env.production.example` manually and run:

```bash
docker compose up -d --build
```

## Database migration
Use `../scripts/migrate_sqlite_to_postgres.py` from a machine that can reach the fresh PostgreSQL target. See `../docs/POSTGRES_MIGRATION.md`.

## Backups
The bundled backup worker can write to the bundled MinIO store, but that is not true disaster recovery if the whole host is lost. For production, set `BACKUP_S3_*` to a company-approved **external** S3-compatible destination and run the restore drill.

## Important
- Do not expose PostgreSQL or MinIO ports publicly.
- Do not keep the live database in Google Drive/OneDrive file synchronization.
- Connect Microsoft/Google/QuickBooks only with organization-approved credentials.
- Run Production Readiness, legal approval, zero-training and real-device tests before company-wide rollout.
