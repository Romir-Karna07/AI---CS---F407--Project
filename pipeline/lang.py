"""
Language support WITHOUT language-specific code.

Nothing here knows Hindi, Bengali, Manipuri (Meitei Mayek) or any other language. It detects scripts generically (Unicode character names),
sends the categorical values that the English lookups cannot resolve to the model, and verifies the answers against closed lists:
  * header names are interpreted by the model (llm.llm_map_headers: "any language or script"),
  * district values -> must be one of the 16 reference districts (or null),
  * month names -> an integer 1-12 (or null),
  * crop / season names -> a short English name (or null); the usual crop-synonym table then standardises it.
Numbers in other numeral systems and separators are handled in readers.to_number (Unicode decimal digits, Arabic separators).
The original text is kept in the row (`__original_*`, shown in `extra`), so nothing is lost by translating.
"""
import re
import unicodedata

from .normalize import MONTHS, UNSPECIFIED, _districts, normalize_district
from .readers import is_missing, to_number
from .trace import traceable

CANON = [d["name"] for d in _districts["districts"]]
UNIT_VOCAB = ["mm", "cm", "inch", "ha", "acre", "sq km", "'000 ha", "lakh ha", "t", "kg", "quintal", "'000 t", "lakh t", "t/ha", "kg/ha", "q/ha"]
UNIT_FOR = {"rainfall_mm": {"mm", "cm", "inch"}, "area": {"ha", "acre", "sq km", "'000 ha", "lakh ha"},
            "production": {"t", "kg", "quintal", "'000 t", "lakh t"}, "yield": {"t/ha", "kg/ha", "q/ha"}}
SYSTEM = (
    "You translate categorical values from a data table (any language or script) into the English forms used by a database.\n"
    f"- district: choose one of {CANON}, or null if it is none of them.\n"
    "- crop: the common English crop name, or null.\n"
    "- season: English season (Kharif, Rabi, Summer, Whole Year, Annual), or null.\n"
    "- month: the calendar month as an integer 1-12, or null.\n"
    f"- unit: the unit word, as one of {UNIT_VOCAB}, or null.\n"
    "Answer ONLY with JSON: {\"district\": {\"<raw value>\": \"UKHRUL\"}, \"crop\": {...}, \"season\": {...}, \"month\": {\"<raw>\": 1}, "
    "\"unit\": {\"<raw>\": \"mm\"}}. "
    "Include only the raw values you were given.")


def scripts_in(texts):
    """{script: count} of the alphabetic characters that are not Latin (e.g. DEVANAGARI, BENGALI, TAMIL, MEETEI) - generic, from Unicode names"""
    out = {}
    for t in texts:
        for ch in str(t):
            if ch.isalpha():
                name = unicodedata.name(ch, "")
                script = name.split(" ")[0] if name else "UNKNOWN"
                if script != "LATIN":
                    out[script] = out.get(script, 0) + 1
    return out


def has_non_ascii_letters(s):
    return any(ch.isalpha() and ord(ch) > 127 for ch in str(s))


def _month_ok(v):
    s = str(v).strip()
    return to_number(s) is not None or s[:3].lower() in MONTHS


def unresolved(rows, mapping):
    """{category: {raw value: [src keys]}} the English lookups cannot resolve"""
    cats = {"district": {}, "crop": {}, "season": {}, "month": {}, "unit": {}}
    src_for = {canon: src for src, canon in mapping.items()}
    for canon in UNIT_FOR:                       # unit words ("मिमी") written in the header or a unit row
        src = src_for.get(canon)
        if not src:
            continue
        m = re.search(r"\(([^)]*)\)", str(src))
        text = (m.group(1) if m else (rows[0]["units"].get(src, "") if rows else "")).strip("() ")
        if text and has_non_ascii_letters(text):
            cats["unit"].setdefault(text, (src, canon))
    for cat, canon in (("district", "district"), ("crop", "crop"), ("season", "season"), ("month", "year_month")):
        src = src_for.get(canon)
        if not src:
            continue
        for r in rows:
            v = r["data"].get(src)
            if is_missing(v) or not isinstance(v, str):
                continue
            if cat == "district" and has_non_ascii_letters(v) and normalize_district(v)[2] == "unmatched":
                cats[cat].setdefault(v.strip(), src)
            elif cat in ("crop", "season") and has_non_ascii_letters(v):
                cats[cat].setdefault(v.strip(), src)
            elif cat == "month" and any(ch.isalpha() for ch in v) and not _month_ok(v) and not any(ch.isdigit() for ch in v):
                cats[cat].setdefault(v.strip(), src)
    return cats


@traceable("localize_values", "chain")
def localize(ai, groups):
    """groups: [(rows, mapping)] for the recognised groups. Translates unresolved categorical values in place. -> notes"""
    todo = [(rows, unresolved(rows, mapping)) for rows, mapping in groups]
    merged = {"district": set(), "crop": set(), "season": set(), "month": set(), "unit": set()}
    for _, cats in todo:
        for c, vals in cats.items():
            merged[c] |= set(vals)
    if not any(merged.values()) or ai is None or not ai.available:
        return []
    ans = ai.ask("language", SYSTEM, "Values:\n" + "\n".join(f"{c}: {sorted(v)[:60]}" for c, v in merged.items() if v),
                 {k: len(v) for k, v in merged.items() if v})
    if not isinstance(ans, dict):
        return ["language: model did not answer; non-English values left as they are"]
    maps = {"district": {}, "crop": {}, "season": {}, "month": {}, "unit": {}}
    for raw, u in (ans.get("unit") or {}).items():
        if raw in merged["unit"] and isinstance(u, str) and u in UNIT_VOCAB:
            maps["unit"][raw] = u
    for raw, en in (ans.get("district") or {}).items():
        if raw in merged["district"] and isinstance(en, str) and normalize_district(en)[1] != UNSPECIFIED:
            maps["district"][raw] = normalize_district(en)[0]
    for cat in ("crop", "season"):
        for raw, en in (ans.get(cat) or {}).items():
            if raw in merged[cat] and isinstance(en, str) and 0 < len(en.strip()) <= 40:
                maps[cat][raw] = en.strip()
    for raw, m in (ans.get("month") or {}).items():
        if raw in merged["month"] and isinstance(m, (int, float)) and not isinstance(m, bool) and 1 <= int(m) <= 12:
            maps["month"][raw] = str(int(m))
    n = 0
    for rows, cats in todo:
        for raw, (src, canon) in cats["unit"].items():          # a unit must fit the variable (no 'kg' for rainfall)
            if raw in maps["unit"] and maps["unit"][raw] in UNIT_FOR[canon]:
                for r in rows:
                    r["units"][src] = maps["unit"][raw]
                n += 1
        for cat, vals in cats.items():
            if cat == "unit":
                continue
            for raw, src in vals.items():
                if raw not in maps[cat]:
                    continue
                for i, r in enumerate(rows):
                    if isinstance(r["data"].get(src), str) and r["data"][src].strip() == raw:
                        r["data"][f"__original_{cat}"] = raw
                        r["data"][src] = maps[cat][raw]
                        n += 1
    return [f"language: translated {n} value(s) in {sum(1 for m in maps.values() for _ in m)} distinct form(s)"]
