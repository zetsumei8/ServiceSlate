#!/usr/bin/env python3
"""Migrate a ServiceSlate SQLite database into a fresh PostgreSQL database.

The target schema is created by the same ServiceSlate migrations used by the app.
Business IDs are preserved. Self-referential foreign keys are restored after the
base rows are loaded. Run against a new/empty target and keep the automatic
source backup produced by this script until the hosted deployment is verified.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Move ServiceSlate data from SQLite to PostgreSQL")
    parser.add_argument("--source", required=True, help="Path to serviceslate.db")
    parser.add_argument("--target", required=True, help="PostgreSQL URL, for example postgresql://user:pass@host/db")
    parser.add_argument("--yes", action="store_true", help="Perform the migration instead of validation-only mode")
    return parser.parse_args()


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]


def foreign_keys(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, str]]:
    # local column, referenced table, referenced column
    return [(str(r[3]), str(r[2]), str(r[4])) for r in conn.execute(f"PRAGMA foreign_key_list({quote_ident(table)})").fetchall()]


def table_order(conn: sqlite3.Connection, tables: list[str]) -> list[str]:
    deps: dict[str, set[str]] = {t: set() for t in tables}
    children: dict[str, set[str]] = defaultdict(set)
    for table in tables:
        for _, ref, _ in foreign_keys(conn, table):
            if ref in deps and ref != table:
                deps[table].add(ref)
                children[ref].add(table)
    queue = deque(sorted(t for t, d in deps.items() if not d))
    order: list[str] = []
    while queue:
        table = queue.popleft(); order.append(table)
        for child in sorted(children.get(table, set())):
            deps[child].discard(table)
            if not deps[child] and child not in order and child not in queue:
                queue.append(child)
    remaining = [t for t in tables if t not in order]
    if remaining:
        raise RuntimeError("Cross-table foreign-key cycle prevents deterministic migration: " + ", ".join(remaining))
    return order


def main() -> int:
    args = parse_args()
    source = Path(args.source).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"Source database not found: {source}")
    if not args.target.startswith(("postgresql://", "postgres://")):
        raise SystemExit("Target must be a PostgreSQL connection URL")

    source_conn = sqlite3.connect(source)
    source_conn.row_factory = sqlite3.Row
    integrity = source_conn.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        raise SystemExit(f"Source SQLite integrity check failed: {integrity}")
    tables = sqlite_tables(source_conn)
    order = table_order(source_conn, tables)
    counts = {t: int(source_conn.execute(f"SELECT COUNT(*) FROM {quote_ident(t)}").fetchone()[0]) for t in order}
    print(json.dumps({"source": str(source), "tables": len(order), "rows": sum(counts.values()), "integrity": integrity}, indent=2))
    if not args.yes:
        print("Validation only. Re-run with --yes to migrate into a fresh PostgreSQL database.")
        return 0

    safety = source.with_name(source.stem + "-before-postgres-migration" + source.suffix)
    shutil.copy2(source, safety)
    print(f"Source safety copy: {safety}")

    # Import ServiceSlate only after the target environment is set so its normal
    # migrations create the PostgreSQL schema without demo-seeding a production DB.
    os.environ["SERVICESLATE_DATABASE_URL"] = args.target
    os.environ["SERVICESLATE_PRODUCTION_MODE"] = "1"
    from serviceslate.db import connect, init_db  # noqa: PLC0415

    init_db()
    self_refs: list[tuple[str, str, str, Any, Any]] = []
    with connect() as target:
        # Refuse to merge into a database that already contains organization data.
        existing = target.execute("SELECT COUNT(*) AS n FROM organizations").fetchone()["n"]
        if existing:
            raise RuntimeError("Target PostgreSQL database is not empty. Use a fresh ServiceSlate target.")
        for table in order:
            source_columns = [str(r[1]) for r in source_conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()]
            target_columns = [str(r[0]) for r in target.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=? ORDER BY ordinal_position",
                (table,),
            ).fetchall()]
            columns = [c for c in source_columns if c in target_columns]
            if not columns:
                continue
            self_fk_columns = {local for local, ref, _ in foreign_keys(source_conn, table) if ref == table}
            rows = source_conn.execute(f"SELECT * FROM {quote_ident(table)}").fetchall()
            placeholders = ",".join("?" for _ in columns)
            column_sql = ",".join(quote_ident(c) for c in columns)
            for row in rows:
                values = []
                for col in columns:
                    value = row[col]
                    if col in self_fk_columns and value is not None:
                        self_refs.append((table, col, "id", row["id"] if "id" in row.keys() else None, value))
                        value = None
                    values.append(value)
                target.execute(f"INSERT INTO {quote_ident(table)}({column_sql}) VALUES({placeholders})", tuple(values))
            print(f"Copied {table}: {len(rows)}")

        for table, column, key_column, key_value, ref_value in self_refs:
            if key_value is not None:
                target.execute(
                    f"UPDATE {quote_ident(table)} SET {quote_ident(column)}=? WHERE {quote_ident(key_column)}=?",
                    (ref_value, key_value),
                )

        # Reset serial sequences where PostgreSQL created one (notably audit_events).
        for table in order:
            try:
                seq_row = target.execute("SELECT pg_get_serial_sequence(?, 'id') AS seq", (table,)).fetchone()
                seq = seq_row["seq"] if seq_row else None
                if seq:
                    target.execute(
                        f"SELECT setval(?, COALESCE((SELECT MAX(id) FROM {quote_ident(table)}), 1), (SELECT COUNT(*)>0 FROM {quote_ident(table)}))",
                        (seq,),
                    )
            except Exception:
                # Most ServiceSlate IDs are text and have no sequence.
                pass

    with connect() as target:
        mismatches = []
        for table, expected in counts.items():
            row = target.execute(f"SELECT COUNT(*) AS n FROM {quote_ident(table)}").fetchone()
            actual = int(row["n"])
            if actual != expected:
                mismatches.append((table, expected, actual))
        if mismatches:
            raise RuntimeError("Migration count verification failed: " + repr(mismatches))
    print(f"Migration complete. Verified {sum(counts.values())} rows across {len(order)} tables.")
    print("Keep the SQLite source and safety copy until hosted backup/restore and user acceptance checks are complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
