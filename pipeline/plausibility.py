"""
Plausibility check: flag impossible-looking rows and KEEP THEM ASIDE for a human. The model never edits or "fixes" a value.

Candidates are chosen by code only: a value outside the declared range (schemas.RANGES), a physically impossible combination (rainy days above
days in the month) or an extreme statistical outlier within the file (robust z-score on the median absolute deviation). A clean file therefore
produces no candidates and no model call. The model is shown only the candidates (with the file's own median / maximum for context) and answers
implausible / plausible / uncertain for each. Only "implausible" rows are removed from the data tables; they are stored in `review_queue`
(`review_status = pending_review`) with the reason, the raw row and its locator, so a person can accept or correct them. Without a model nothing is
quarantined: the existing validation flags stay as the only signal.
"""
import json
import statistics

from .trace import traceable

COLS = {"soil_agent": ["n", "p", "k", "oc", "ph", "ec", "s", "zn", "fe", "cu", "mn", "b"],
        "rainfall_agent": ["rainfall_mm", "rainy_days_ge_2_5mm"], "production_agent": ["area_ha", "production_t", "yield_t_ha"]}
MAX_CANDIDATES = 25
Z = 25.0
RATIO = 10.0
SYSTEM = (
    "You review extracted agricultural figures for Manipur, India. Each item failed a range check or is an extreme outlier in its file. "
    "Decide whether the value is PRACTICALLY POSSIBLE in the real world, given the units and context shown. "
    "implausible = physically impossible, or so far beyond any real-world record that it is almost certainly a data / parsing error "
    "(e.g. 99,999 mm of rain in one month, pH 55, a crop area larger than the whole state). "
    "plausible = unusual, large or small but could occur (a big district in a good year; a rich micronutrient patch). "
    "uncertain = cannot tell. Use the other figures in the same row to check consistency (e.g. production / area = yield). "
    "The file median is background only: being much larger than the other rows does NOT make a value implausible. "
    "Be conservative: answer implausible only when you are confident it cannot be real. Do not correct values. "
    "Answer ONLY with JSON: {\"verdicts\": [{\"id\": 1, \"verdict\": \"implausible\", \"reason\": \"short\"}]}")


def _unit_for(row, col):
    """the unit of THIS column (the row stores one 'col:unit;col:unit' string for the whole record)"""
    unit = str(row.get("unit") or "")
    key = {"area_ha": "area", "production_t": "production", "yield_t_ha": "yield"}.get(col, col)
    for part in unit.split(";"):
        if part.startswith(key + ":"):
            return part.split(":", 1)[1]
    return unit if ":" not in unit else ("days" if col.startswith("rainy") else "")


def candidates(agent, rows):
    """-> [(row index, column, reason)] (code only)"""
    out = []
    for col in COLS[agent]:
        vals = [(i, r.get(col)) for i, r in enumerate(rows) if isinstance(r.get(col), (int, float))]
        nums = [v for _, v in vals]
        med = statistics.median(nums) if nums else 0
        mad = statistics.median([abs(v - med) for v in nums]) if nums else 0
        for i, v in vals:
            notes = rows[i].get("validation_notes") or ""
            if f"{col}_out_of_range" in notes:
                out.append((i, col, "outside the declared range"))
            elif len(nums) >= 12 and mad > 0 and abs(v - med) > Z * 1.4826 * mad and v > RATIO * max(med, 1e-9) and v > 0:
                out.append((i, col, f"extreme outlier in this file (median {med:g}, MAD {mad:g})"))
    for i, r in enumerate(rows):
        if "rainy_days_exceed_days_in_month" in (r.get("validation_notes") or ""):
            out.append((i, "rainy_days_ge_2_5mm", "more rainy days than days in the month"))
    seen, uniq = set(), []
    for c in out:
        if (c[0], c[1]) not in seen:
            seen.add((c[0], c[1]))
            uniq.append(c)
    return uniq


@traceable("plausibility_check", "chain")
def check(ai, per_agent):
    """per_agent: {agent: [row dicts]} -> ({(agent, row index): {...}}, notes). Only 'implausible' rows are returned."""
    items, meta = [], {}
    for agent, rows in per_agent.items():
        for i, col, why in candidates(agent, rows):
            if len(items) >= MAX_CANDIDATES:
                break
            nums = [r[col] for r in rows if isinstance(r.get(col), (int, float))]
            r = rows[i]
            ident = {k: r.get(k) for k in ("district", "period", "year_month", "crop", "village") if r.get(k) not in (None, "UNSPECIFIED")}
            others = {c: r[c] for c in COLS[agent] if c != col and isinstance(r.get(c), (int, float))}   # the rest of the row: consistency check
            meta[len(items) + 1] = (agent, i, col, why)
            items.append(f"{len(items) + 1} | {agent} | {ident} | {col}={r[col]} (unit: {_unit_for(r, col)}) | same row: {others or 'no other figures'} | {why} | "
                         f"file median {statistics.median(nums):g}, max {max(nums):g}")
    if not items:
        return {}, []
    if ai is None or not ai.available:
        return {}, [f"plausibility: {len(items)} candidate(s) flagged by code; no model, nothing quarantined"]
    ans = ai.ask("plausibility", SYSTEM, "Items (id | agent | identity | value | same row | why flagged | file context):\n" + "\n".join(items), {"candidates": len(items)})
    if not isinstance(ans, dict) or not isinstance(ans.get("verdicts"), list):
        return {}, [f"plausibility: {len(items)} candidate(s), model gave no verdict; nothing quarantined"]
    out = {}
    for v in ans["verdicts"]:
        if isinstance(v, dict) and v.get("id") in meta and v.get("verdict") == "implausible":
            agent, i, col, why = meta[v["id"]]
            out[(agent, i)] = {"field": col, "reason": f"{why}; model: {str(v.get('reason', ''))[:120]}", "flagged_by": "code+model"}
    return out, [f"plausibility: {len(items)} candidate(s), {len(out)} judged implausible and set aside for review"]


def review_row(agent, table, row, info):
    return {"agent": agent, "table_name": table, "source_file": row.get("source_file"), "source_hash": row.get("source_hash"),
            "locator": row.get("locator"), "district": row.get("district"), "field": info["field"], "value": row.get(info["field"]),
            "reason": info["reason"], "flagged_by": info["flagged_by"], "row_json": json.dumps(row, ensure_ascii=False, default=str)}
