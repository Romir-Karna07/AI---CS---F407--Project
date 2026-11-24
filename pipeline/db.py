"""SQLite loader. Schema lives in db/schema.sql so it can be reviewed and dumped independently."""
import json
import sqlite3
from pathlib import Path

from .normalize import STATE_CODE, STATE_LEVEL_CODE, STATE_LEVEL_NAME, STATE_NAME, _districts
from .schemas import TABLES

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "db" / "manipur.db"


def connect(path=DEFAULT_DB) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    con.executemany("INSERT OR IGNORE INTO districts VALUES (?,?,?,?)",
                    [(d["code"], d["name"], STATE_NAME, STATE_CODE) for d in _districts["districts"]]
                    + [(STATE_LEVEL_CODE, STATE_LEVEL_NAME, STATE_NAME, STATE_CODE)])     # pseudo-district for whole-state figures
    con.commit()
    return con


def insert_rows(con, agent: str, rows: list[dict]) -> tuple[int, int]:
    """-> (inserted, duplicates_skipped). Re-ingesting the same file content is a no-op."""
    table, model = TABLES[agent]
    cols = list(model.model_fields)
    before = con.total_changes
    sql = f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    con.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    inserted = con.total_changes - before
    return inserted, len(rows) - inserted


def insert_review(con, run_id: str, items: list[dict]) -> int:
    """rows set aside for human review (plausibility check); idempotent per (source_hash, locator, field)"""
    cols = ["run_id", "agent", "table_name", "source_file", "source_hash", "locator", "district", "field", "value", "reason", "flagged_by", "row_json"]
    before = con.total_changes
    con.executemany(f"INSERT OR IGNORE INTO review_queue ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [tuple(({**it, 'run_id': run_id}).get(c) for c in cols) for it in items])
    return con.total_changes - before
