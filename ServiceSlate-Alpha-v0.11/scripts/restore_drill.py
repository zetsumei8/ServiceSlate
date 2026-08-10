"""Offline backup/restore smoke drill for CI and administrators.

The drill runs only against a temporary ServiceSlate data directory. It proves
that a generated backup contains a readable database (or PostgreSQL dump
manifest) and that SQLite backup bytes can be restored into a fresh database.
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="serviceslate-restore-drill-") as tmp:
        os.environ["SERVICESLATE_DATA_DIR"] = tmp
        os.environ.pop("SERVICESLATE_DATABASE_URL", None)
        os.environ["SERVICESLATE_SEED_DEMO"] = "1"
        import serviceslate.db as db
        importlib.reload(db)
        db.init_db()
        backup = db.create_backup_archive("RestoreDrill")
        with zipfile.ZipFile(backup) as zf:
            names = set(zf.namelist())
            if "serviceslate.db" not in names or "backup-manifest.json" not in names:
                raise SystemExit("Backup archive is missing required database/manifest files")
            restored = Path(tmp) / "restored.db"
            restored.write_bytes(zf.read("serviceslate.db"))
        with sqlite3.connect(restored) as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
            schema = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            orgs = conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        if result != "ok" or not schema or orgs < 1:
            raise SystemExit(f"Restore drill failed: integrity={result!r}, schema={schema!r}, orgs={orgs}")
        print(f"Restore drill passed: schema {schema}, {orgs} organization(s), integrity ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
