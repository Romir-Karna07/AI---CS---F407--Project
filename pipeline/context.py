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

from .normalize import STATE_NAME, UNSPECIFIED, normalize_district
from .trace import traceable

BASES = {"area": ["ha", "acre", "sq km"], "production": ["t", "kg", "quintal"], "yield": ["t/ha", "kg/ha", "q/ha"], "rainfall_mm": ["mm", "cm", "inch"]}
MULTIPLIERS = {1, 10, 100, 1000, 10000, 100000, 1000000, 10000000}
SYSTEM = (
    "You read the title / caption lines printed around ONE table (any language or script) and report what they say about the table. "
    "Report only what is stated; use null when nothing is stated.\n"
    "- units: for each requested variable, the unit the table's values are in, as {\"multiplier\": 1000, \"unit\": \"ha\", \"quote\": \"...\"}. "
    "'thousand hectares' or \"'000 ha\" = multiplier 1000, unit ha; 'lakh tonnes' = 100000, t. Allowed units per variable are given. "
    "Give an entry for EVERY requested variable whose unit the lines state - one sentence can state several "
    "('Area in thousand hectares; production in thousand tonnes' = area AND production) - and omit only the variables the lines do not mention.\n"
    "- district: English name if the lines say which district the table is about.\n"
    "- period: 'YYYY-MM' for a single month, 'YYYY-YY' for an agricultural / financial year (2019-20), 'YYYY' for a calendar year.\n"
    "- crop: English crop name if the whole table is about one crop.\n"
    "Every non-null item needs a quote: the exact words from the lines (original language). "
    "Answer ONLY with JSON: {\"units\": {\"area\": {\"multiplier\": 1000, \"unit\": \"ha\", \"quote\": \"...\"}}, "
    "\"district\": {\"value\": \"Thoubal\", \"quote\": \"...\"} or null, \"period\": {...} or null, \"crop\": {...} or null}")


def _in_text(quote, text):
    toks = str(quote or "").split()
    return bool(toks) and re.search(r"\s+".join(re.escape(t) for t in toks), text) is not None


_SCALE = r"(?P<scale>['‘’`]\s*000|thousand|th\.|lakh|lakhs|crore)"
_BASE_WORDS = {"production": r"(?P<base>mt|metric\s+tonnes?|tonnes?|tons?|t)", "area": r"(?P<base>ha|hectares?|hect\.?)"}
_SCALE_MULT = {"000": 1000, "thousand": 1000, "th.": 1000, "lakh": 100000, "lakhs": 100000, "crore": 10000000}
_PERIOD_RE = re.compile(r"\b((?:19|20)\d{2})\s*[-–/]\s*(\d{2}|\d{4})\b")


_CROP_GROUP_RE = re.compile(r"\b(?:of|for)\s+([A-Z][a-z]+)\s+Crops\b")      # "Area and Production of Fruit Crops", "... of Spice Crops"


def _fixed_crop(text):
    """a table about one named group of crops, read from its title: 'Fruit Crops' -> 'Fruits' (quote kept)"""
    titles = [l for l in text.splitlines() if re.match(r"\s*Table\s+[\w.]+\s*[:.-]", l, re.I)]     # the table's own title first,
    m = next((x for x in (_CROP_GROUP_RE.search(t) for t in titles) if x), None) or _CROP_GROUP_RE.search(text)   # not a running page header
    if not m or m.group(1).lower() in ("major", "all", "other", "principal", "food", "field", "selected", "horticulture", "agricultural"):
        return None, None
    word = m.group(1)
    return (word if word.endswith("s") else word + "s"), m.group(0)


def _fixed_facts(text, want_units, want_period):
    """facts plain code can read from a caption, before any model is asked: "Crops ('000 MT), 2023-24", "Area in thousand hectares".
    Exactly what the model would have to quote, so the same evidence rule holds; the model is asked only for what is left."""
    units, evidence, period = {}, {}, None
    for var in want_units:
        if var not in _BASE_WORDS:
            continue
        m = re.search(_SCALE + r"\s*\(?\s*" + _BASE_WORDS[var] + r"\b", text, re.I)
        if m:
            scale = re.sub(r"['‘’`\s]", "", m.group("scale").lower())
            mult = _SCALE_MULT.get(scale)
            if mult:
                units[var] = f"{mult}*{'t' if var == 'production' else 'ha'}"
                evidence[f"unit:{var}"] = m.group(0)
    if want_period:
        found = {(m.group(1), m.group(2)) for m in _PERIOD_RE.finditer(text)}
        if len(found) == 1:
            y, e = found.pop()
            period = f"{y}-{e[-2:]}"
            evidence["period"] = next(_PERIOD_RE.finditer(text)).group(0)
    return units, period, evidence


@traceable("context_facts", "chain")
def facts_for(ai, context_lines, columns, want_units, want_district, want_period, want_crop=False):
    """-> verified {units: {var: '1000*ha'}, district, period, crop, evidence} or None"""
    text = "\n".join(l for l in context_lines if l and l.strip())
    if not text.strip() or not (want_units or want_district or want_period or want_crop):
        return None
    f_units, f_period, f_ev = _fixed_facts(text, want_units, want_period)
    f_crop, crop_quote = _fixed_crop(text) if want_crop else (None, None)
    if crop_quote:
        f_ev["crop"] = crop_quote
    if (set(want_units) <= set(f_units)) and (f_period or not want_period) and not want_district and (f_crop or not want_crop):
        out = {"units": f_units, "evidence": f_ev, "how": "fixed code"}
        if f_period:
            out["period"] = f_period
        if f_crop:
            out["crop"] = f_crop
        return out if (f_units or f_period or f_crop) else None
    ans = _model_facts(ai, text, columns, want_units, want_district, want_period, want_crop)
    out = ans or {"units": {}, "evidence": {}}
    out["units"] = {**out.get("units", {}), **f_units}          # what code read wins
    out["evidence"] = {**out.get("evidence", {}), **f_ev}
    if f_period:
        out["period"] = f_period
    if f_crop:
        out["crop"] = f_crop
    return out if (out["units"] or any(k in out for k in ("district", "period", "crop"))) else None


def _model_facts(ai, text, columns, want_units, want_district, want_period, want_crop):
    allowed = {v: BASES[v] for v in want_units if v in BASES}
    user = (f"Context lines:\n{text[:1500]}\n\nTable columns: {columns[:12]}\n"
            f"Requested: units for {allowed or 'none'}; district: {want_district}; period: {want_period}; crop: {want_crop}")
    ans = ai.ask("context", SYSTEM, user, {"lines": text.count(chr(10)) + 1})
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


# ----------------------------------------------------------------------------- state (document level)
STATE_SYSTEM = (
    "You read the cover / title lines and the file name of ONE document (any language or script) and say which Indian state its data is about. "
    "Answer only if the lines state it; otherwise null. The value must be the English state name; the quote must be the exact words from the "
    'lines (original language). Answer ONLY with JSON: {"state": {"value": "Manipur", "quote": "..."} or null}')
_STATE_RE = re.compile(r"\b" + re.escape(STATE_NAME.lower()) + r"\b", re.I)


def find_state(ai, doc_lines, table_lines):
    """-> {'state': 'MANIPUR', 'evidence': quote} or None.
    The state is rarely printed next to a table: it is on the cover, in the file name or in the title. Fixed code looks for the name in those
    lines first (free); only if it is not there is the model asked, once per document, and its quote must occur in the lines."""
    lines = [l.strip() for l in list(table_lines or []) + list(doc_lines or []) if l and l.strip()]
    text = "\n".join(lines)
    if not text:
        return None
    for l in lines:
        if _STATE_RE.search(l):
            return {"state": STATE_NAME, "evidence": l[:100]}
    if ai is None or not ai.available:
        return None
    if "state" in ai.memo:
        return ai.memo["state"]
    ans = ai.ask("state", STATE_SYSTEM, f"Lines:\n{text[:1200]}", {"lines": len(lines)})
    item = ans.get("state") if isinstance(ans, dict) else None
    found = None
    if isinstance(item, dict) and str(item.get("value", "")).strip().lower() == STATE_NAME.lower() and _in_text(item.get("quote"), text):
        found = {"state": STATE_NAME, "evidence": str(item["quote"])[:100]}
    ai.memo["state"] = found
    return found


def apply_state(rows, found):
    """fill `state` from the document (only where the table itself has none - the agent checks that); recorded in the row's audit trail"""
    for r in rows:
        ctx = dict(r.get("ctx") or {"units": {}})
        ctx["state"] = found["state"]
        ctx["evidence"] = {**(ctx.get("evidence") or {}), "state": found["evidence"]}
        r["ctx"] = ctx
