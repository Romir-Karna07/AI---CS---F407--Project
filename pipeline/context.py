"""
Context facts: what the text AROUND a table says about the table.

"Area in thousand hectares", "Rainfall statistics for January 2019", "Thoubal district - 2019-20", a sheet called "Ukhrul" ... The columns often
carry no unit, no district and no period because the title carries them. Left alone, a value in thousand hectares would be stored as hectares
(1000x too small) with `unit_status = assumed` - a silent error. The model reads the context lines (title rows, captions, headings, sheet names)
and returns facts, each with the VERBATIM words that justify it. Fixed code accepts a fact only if
  * the quoted words occur in the context text,
  * a unit is one of a fixed vocabulary with a multiplier from a fixed set (1, 10, 100 ... 10^7),
  * a district resolves through the reference list.
Facts are used only where the table itself gives nothing (explicit units / columns win), and they are written to the row's `extra` for audit.
"""
import re

from .normalize import UNSPECIFIED, normalize_district
from .trace import traceable

BASES = {"area": ["ha", "acre", "sq km"], "production": ["t", "kg", "quintal"], "yield": ["t/ha", "kg/ha", "q/ha"], "rainfall_mm": ["mm", "cm", "inch"]}
MULTIPLIERS = {1, 10, 100, 1000, 10000, 100000, 1000000, 10000000}
SYSTEM = (
    "You read the title / caption lines printed around ONE table (any language or script) and report what they say about the table. "
    "Report only what is stated; use null when nothing is stated.\n"
    "- units: for each requested variable, the unit the table's values are in, as {\"multiplier\": 1000, \"unit\": \"ha\", \"quote\": \"...\"}. "
    "'thousand hectares' or \"'000 ha\" = multiplier 1000, unit ha; 'lakh tonnes' = 100000, t. Allowed units per variable are given.\n"
    "- district: English name if the lines say which district the table is about.\n"
    "- period: 'YYYY-MM' for a single month, 'YYYY-YY' for an agricultural / financial year (2019-20), 'YYYY' for a calendar year.\n"
    "- crop: English crop name if the whole table is about one crop.\n"
    "Every non-null item needs a quote: the exact words from the lines (original language). "
    "Answer ONLY with JSON: {\"units\": {\"area\": {\"multiplier\": 1000, \"unit\": \"ha\", \"quote\": \"...\"}}, "
    "\"district\": {\"value\": \"Thoubal\", \"quote\": \"...\"} or null, \"period\": {...} or null, \"crop\": {...} or null}")


def _in_text(quote, text):
    toks = str(quote or "").split()
    return bool(toks) and re.search(r"\s+".join(re.escape(t) for t in toks), text) is not None


@traceable("context_facts", "chain")
def facts_for(ai, context_lines, columns, want_units, want_district, want_period, want_crop=False):
    """-> verified {units: {var: '1000*ha'}, district, period, crop, evidence} or None"""
    text = "\n".join(l for l in context_lines if l and l.strip())
    if not text.strip() or not (want_units or want_district or want_period or want_crop):
        return None
    allowed = {v: BASES[v] for v in want_units if v in BASES}
    user = (f"Context lines:\n{text[:1500]}\n\nTable columns: {columns[:12]}\n"
            f"Requested: units for {allowed or 'none'}; district: {want_district}; period: {want_period}; crop: {want_crop}")
    ans = ai.ask("context", SYSTEM, user, {"lines": len(context_lines)})
    if not isinstance(ans, dict):
        return None
    out, evidence = {"units": {}}, {}
    for var, item in (ans.get("units") or {}).items():
        if var in allowed and isinstance(item, dict) and item.get("unit") in allowed[var] and _in_text(item.get("quote"), text):
            mult = item.get("multiplier", 1)
            if isinstance(mult, (int, float)) and not isinstance(mult, bool) and int(mult) in MULTIPLIERS:
                out["units"][var] = f"{int(mult)}*{item['unit']}"
                evidence[f"unit:{var}"] = item["quote"]
    for key, want in (("district", want_district), ("period", want_period), ("crop", want_crop)):
        item = ans.get(key)
        if want and isinstance(item, dict) and item.get("value") and _in_text(item.get("quote"), text):
            val = str(item["value"]).strip()
            if key == "district":
                canon, code, _ = normalize_district(val)
                if code == UNSPECIFIED:
                    continue
                val = canon
            elif key == "period" and not re.fullmatch(r"(19|20)\d{2}(-\d{2}(\d{2})?)?", val):
                continue
            out[key] = val
            evidence[key] = item["quote"]
    out["evidence"] = evidence
    return out if (out["units"] or any(k in out for k in ("district", "period", "crop"))) else None


def apply_to_rows(rows, facts):
    for r in rows:
        r["ctx"] = {k: v for k, v in facts.items() if k in ("district", "period", "crop", "evidence")} | {"units": facts["units"]}
