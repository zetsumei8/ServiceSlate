# Moving ServiceSlate from the Laptop Database to PostgreSQL

The local Alpha uses SQLite because it is simple and requires no database server. Shared hosting uses PostgreSQL.

## Principle
The move changes infrastructure, not ServiceSlate record identity/workflows.

## Tool
`scripts/migrate_sqlite_to_postgres.py`

Validation only:

```bash
PYTHONPATH=src python scripts/migrate_sqlite_to_postgres.py \
  --source /path/to/serviceslate.db \
  --target postgresql://user:password@host/serviceslate
```

Actual migration into a **fresh/empty** PostgreSQL ServiceSlate database:

```bash
PYTHONPATH=src python scripts/migrate_sqlite_to_postgres.py \
  --source /path/to/serviceslate.db \
  --target postgresql://user:password@host/serviceslate \
  --yes
```

The script:
- checks SQLite integrity first;
- creates a source safety copy;
- creates the target schema through normal ServiceSlate migrations;
- preserves ServiceSlate business IDs;
- respects table dependency order and restores self-references afterward;
- verifies row counts after transfer.

Keep the original SQLite database until hosted backups, restore drill, representative-record validation and user acceptance are complete.
