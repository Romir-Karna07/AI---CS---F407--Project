"""
Row models for the three database tables (Pydantic) + declared validation rules.

Every row carries the provenance columns the Phase 2 rubric requires (M14):
source_file, source_hash, source_type, agent, run_id, locator, plus crop / location / time / unit
(filled, or explicitly coded 'UNSPECIFIED' / NULL when the source has no such concept).
"""
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_file: str
    source_hash: str
    source_type: str                       # json | csv
    agent: str                             # soil_agent | rainfall_agent | production_agent
    run_id: str
    locator: str                           # JSON path or CSV line/columns the row came from
    district: str
    district_code: str
    state: str
    state_code: str
    crop: str = "UNSPECIFIED"
    unit: str                              # unit basis of the numeric columns
    unit_status: str = "assumed"           # declared | assumed | unknown:<text>
    validation_status: str = "valid"       # valid | invalid
    validation_notes: str = ""
    extra: Optional[str] = None            # JSON of source fields that matched no column (nothing is dropped)


class SoilRow(Provenance):
    period: Optional[str] = None           # sampling cycle, e.g. 2017-19
    year: Optional[int] = None             # first year of the cycle
    village: str = "UNSPECIFIED"
    tehsil: str = "UNSPECIFIED"
    block: str = "UNSPECIFIED"
    category: str = "UNSPECIFIED"
    record_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    n: Optional[float] = None
    p: Optional[float] = None
    k: Optional[float] = None
    oc: Optional[float] = None
    ph: Optional[float] = None
    ec: Optional[float] = None
    s: Optional[float] = None              # sulphur
    zn: Optional[float] = None
    fe: Optional[float] = None
    cu: Optional[float] = None
    mn: Optional[float] = None
    b: Optional[float] = None


class RainfallRow(Provenance):
    year_month: str                        # YYYY-MM
    year: int
    month: int
    rainfall_mm: Optional[float] = None
    rainy_days_ge_2_5mm: Optional[int] = None
    days_with_data: Optional[int] = None
    days_in_month: Optional[int] = None


class ProductionRow(Provenance):
    season: str = "UNSPECIFIED"
    period: Optional[str] = None           # agricultural year label, e.g. 1997-98
    year: Optional[int] = None             # first year
    crop_raw: str = ""
    area_ha: Optional[float] = None
    production_t: Optional[float] = None
    yield_t_ha: Optional[float] = None


TABLES = {
    "soil_agent": ("soil_health", SoilRow),
    "rainfall_agent": ("rainfall_monthly", RainfallRow),
    "production_agent": ("crop_production", ProductionRow),
}

# ---------------------------------------------------------------------------- validation rules (M15)
# (column, min, max) - values outside are 'invalid'. Ranges are plausibility bounds, not agronomic norms.
RANGES = {
    "soil_agent": [("ph", 2, 11), ("oc", 0, 15), ("ec", 0, 25), ("n", 0, 3000), ("p", 0, 3000), ("k", 0, 5000),
                   ("s", 0, 5000), ("zn", 0, 5000), ("fe", 0, 5000), ("cu", 0, 5000), ("mn", 0, 5000), ("b", 0, 5000),
                   ("latitude", 23.5, 25.8), ("longitude", 92.9, 94.9)],
    "rainfall_agent": [("rainfall_mm", 0, 3000), ("month", 1, 12), ("rainy_days_ge_2_5mm", 0, 31),
                       ("days_in_month", 28, 31), ("days_with_data", 0, 31), ("year", 1900, 2100)],
    "production_agent": [("area_ha", 0, 1e7), ("production_t", 0, 1e8), ("yield_t_ha", 0, 500), ("year", 1900, 2100)],
}
YIELD_TOLERANCE = 0.05   # |yield - production/area| / yield


def validate_row(agent: str, row: dict) -> list[str]:
    notes = []
    if row["district_code"] == "UNSPECIFIED":
        notes.append(f"district_not_in_manipur_reference:{row['district']}")
    for col, lo, hi in RANGES[agent]:
        v = row.get(col)
        if v is not None and not (lo <= v <= hi):
            notes.append(f"{col}_out_of_range:{v}")
    if agent == "soil_agent":
        if sum(row.get(c) is not None for c in ("n", "p", "k", "oc", "ph", "ec", "s", "zn", "fe", "cu", "mn", "b")) < 3:
            notes.append("fewer_than_3_nutrient_values")
        if row.get("period") is None:
            notes.append("period_missing")
    if agent == "rainfall_agent":
        d, w = row.get("rainy_days_ge_2_5mm"), row.get("days_in_month")
        if d is not None and w is not None and d > w:
            notes.append("rainy_days_exceed_days_in_month")
        if row.get("rainfall_mm") is None:
            notes.append("rainfall_missing")
    if agent == "production_agent":
        a, p, y = row.get("area_ha"), row.get("production_t"), row.get("yield_t_ha")
        if a and p is not None and y:
            if abs(y - p / a) > max(YIELD_TOLERANCE * y, 0.01):   # 5% or source rounding to 2 decimals
                notes.append(f"yield_inconsistent:{y}_vs_{round(p / a, 2)}")
        if row.get("period") is None:
            notes.append("period_missing")
        if all(row.get(c) is None for c in ("area_ha", "production_t", "yield_t_ha")):
            notes.append("no_numeric_values")
    if str(row.get("unit_status", "")).startswith("unknown"):
        notes.append(row["unit_status"])
    return notes
