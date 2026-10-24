-- Manipur agri database (SQLite). Created by pipeline/db.py; safe to re-run.
-- Provenance columns on every fact table: source_file, source_hash, source_type, agent, run_id, locator.
-- Missing text is coded 'UNSPECIFIED'; missing numbers are NULL.

CREATE TABLE IF NOT EXISTS districts (
    district_code  TEXT PRIMARY KEY,          -- SLUSI/SHC portal code (not verified against LGD)
    district_name  TEXT NOT NULL UNIQUE,
    state          TEXT NOT NULL,
    state_code     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    input_file        TEXT NOT NULL,
    source_hash       TEXT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL,          -- complete | partial | failed
    rows_extracted    INTEGER DEFAULT 0,
    rows_inserted     INTEGER DEFAULT 0,
    rows_duplicate    INTEGER DEFAULT 0,
    rows_invalid      INTEGER DEFAULT 0,
    retries           INTEGER DEFAULT 0,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS soil_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL, source_hash TEXT NOT NULL, source_type TEXT NOT NULL,
    agent TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id), locator TEXT NOT NULL,
    district TEXT NOT NULL, district_code TEXT NOT NULL, state TEXT NOT NULL, state_code TEXT NOT NULL,
    crop TEXT NOT NULL DEFAULT 'UNSPECIFIED',
    period TEXT, year INTEGER,
    village TEXT NOT NULL DEFAULT 'UNSPECIFIED', tehsil TEXT NOT NULL DEFAULT 'UNSPECIFIED',
    block TEXT NOT NULL DEFAULT 'UNSPECIFIED', category TEXT NOT NULL DEFAULT 'UNSPECIFIED',
    record_id TEXT, latitude REAL, longitude REAL,
    n REAL, p REAL, k REAL, oc REAL, ph REAL, ec REAL, s REAL, zn REAL, fe REAL, cu REAL, mn REAL, b REAL,
    unit TEXT NOT NULL, unit_status TEXT NOT NULL,
    validation_status TEXT NOT NULL, validation_notes TEXT NOT NULL DEFAULT '', extra TEXT,
    UNIQUE (source_hash, locator)
);

CREATE TABLE IF NOT EXISTS rainfall_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL, source_hash TEXT NOT NULL, source_type TEXT NOT NULL,
    agent TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id), locator TEXT NOT NULL,
    district TEXT NOT NULL, district_code TEXT NOT NULL, state TEXT NOT NULL, state_code TEXT NOT NULL,
    crop TEXT NOT NULL DEFAULT 'UNSPECIFIED',
    year_month TEXT, year INTEGER, month INTEGER,
    rainfall_mm REAL, rainy_days_ge_2_5mm INTEGER, days_with_data INTEGER, days_in_month INTEGER,
    unit TEXT NOT NULL, unit_status TEXT NOT NULL,
    validation_status TEXT NOT NULL, validation_notes TEXT NOT NULL DEFAULT '', extra TEXT,
    UNIQUE (source_hash, locator)
);

CREATE TABLE IF NOT EXISTS crop_production (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL, source_hash TEXT NOT NULL, source_type TEXT NOT NULL,
    agent TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(run_id), locator TEXT NOT NULL,
    district TEXT NOT NULL, district_code TEXT NOT NULL, state TEXT NOT NULL, state_code TEXT NOT NULL,
    crop TEXT NOT NULL, crop_raw TEXT NOT NULL DEFAULT '',
    season TEXT NOT NULL DEFAULT 'UNSPECIFIED', period TEXT, year INTEGER,
    area_ha REAL, production_t REAL, yield_t_ha REAL,
    unit TEXT NOT NULL, unit_status TEXT NOT NULL,
    validation_status TEXT NOT NULL, validation_notes TEXT NOT NULL DEFAULT '', extra TEXT,
    UNIQUE (source_hash, locator)
);

-- semantic duplicate detection (M16): same place/time/crop appearing in more than one source row
CREATE INDEX IF NOT EXISTS ix_soil_nat  ON soil_health(district_code, village, period, record_id);
CREATE INDEX IF NOT EXISTS ix_rain_nat  ON rainfall_monthly(district_code, year_month);
CREATE INDEX IF NOT EXISTS ix_prod_nat  ON crop_production(district_code, crop, season, period);
