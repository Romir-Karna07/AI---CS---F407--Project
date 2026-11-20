"""
Optional LLM fallback (Groq). The pipeline is deterministic first; the LLM is consulted only when
  (a) a group of records matches no domain, or
  (b) numeric column headers are not in the concept dictionary.
Key is read from the GROQ_API_KEY environment variable or a git-ignored .env file in the project root.
No key / package -> `get_llm()` returns None and those cases are reported as unmapped.
Override the model with AGRI_LLM_MODEL (default: openai/gpt-oss-120b, served by Groq).
"""
import json
import os
import re
import unicodedata
from pathlib import Path

from .concepts import FERTILIZER_TERMS, SYNONYMS, non_crop_term, words_of

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "openai/gpt-oss-120b"

CANONICAL_FIELDS = sorted(SYNONYMS)
FIELD_HELP = {
    "year_month": "calendar month of the record (month name, month number or YYYY-MM)", "period": "year, agricultural year (2019-20) or survey cycle",
    "district": "district name", "district_code": "district code", "state": "state name", "crop": "name of the CROP on each row (a column such as Particulars / Item / Commodity that lists crops); not animals, fish or money items", "season": "crop season (Kharif / Rabi ...)",
    "rainfall_mm": "amount of rainfall / precipitation", "rainy_days": "number of rainy days", "area": "cultivated / sown area",
    "production": "CROP production: quantity of a harvested plant crop (never milk, eggs, meat, fish or other livestock / fishery output)", "yield": "crop yield per unit area", "village": "village name", "tehsil": "tehsil / sub-district",
    "block": "block name", "category": "fertility rating (Low / Medium / High, Sufficient / Deficient)", "latitude": "latitude", "longitude": "longitude",
    "n": "available nitrogen measured IN THE SOIL (a soil test; NOT a fertiliser product such as urea)", "p": "available phosphorus in the soil (not DAP / SSP)", "k": "available potassium in the soil (not MOP)", "oc": "organic carbon", "ph": "soil pH", "ec": "electrical conductivity",
    "s": "sulphur", "zn": "zinc", "fe": "iron", "cu": "copper", "mn": "manganese", "b": "boron", "unit": "unit of measurement", "record_id": "record / sample identifier"}
DOMAINS = ["soil", "rainfall", "production"]


_LLM = {}


def get_llm():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    if not os.getenv("GROQ_API_KEY"):
        return None
    try:
        from langchain_groq import ChatGroq
        model = os.getenv("AGRI_LLM_MODEL", DEFAULT_MODEL)
        if model not in _LLM:      # building the client is slow: one per process
            kw = {}
            if "gpt-oss" in model:     # a reasoning model: "thinking" tokens count against the free-tier daily quota, so keep them low
                kw["reasoning_effort"] = os.getenv("AGRI_LLM_REASONING", "low")
            _LLM[model] = ChatGroq(model=model, temperature=0, max_retries=2, timeout=60, **kw)
        return _LLM[model]
    except Exception:
        return None


def _json_from(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def llm_classify(llm, keys, sample_row) -> str | None:
    prompt = (
        "Classify an agricultural data table into exactly one of: soil (soil-health card / nutrient tests), "
        "rainfall (precipitation by period), production (crop area / production / yield), or none.\n"
        f"Column names: {list(keys)[:40]}\nOne sample row: {json.dumps(sample_row, default=str)[:600]}\n"
        'Answer only JSON like {"domain": "soil"}.')
    try:
        d = _json_from(llm.invoke(prompt).content).get("domain")
    except Exception:
        return None
    return d if d in DOMAINS else None


def headers_prompt(unmapped_keys, sample_row, sample_rows=None, context=None):
    system = ("Map source column names to canonical fields. Column names and cell values may be in ANY language or script: first work out what "
              "each column means, then map it. Only map a column if you are confident; omit the rest.\n"
              "The database holds three kinds of data: SOIL TEST results, monthly RAINFALL, and CROP area / production / yield. Do NOT map a column "
              "that is about something else: livestock, milk, eggs, meat, fish, fertiliser or seed quantities, money, schemes, people. "
              "A column that names the crop of each row (Particulars, Item, Commodity ...) is `crop`; a state name is `state`. "
              "Use ONLY the canonical field names listed below, written exactly (a column of years is `period`, a column of months is `year_month`).")
    ctx = ("Text printed around the table / on the cover:\n" + "\n".join(context)[:500] + "\n") if context else ""
    rows = sample_rows or [sample_row]
    user = (ctx + "Canonical fields (name: meaning):\n" + "\n".join(f"- {f}: {FIELD_HELP.get(f, '')}" for f in CANONICAL_FIELDS) +
            "\nMap EVERY column whose meaning matches a canonical field, including month and year columns.\n"
            f"Source columns: {list(unmapped_keys)[:40]}\n"
            f"Sample rows: {json.dumps(rows[:4], default=str, ensure_ascii=False)[:900]}\n"
            'Answer only JSON like {"Nitrogen_avail": "n", "Dist": "district"}.')
    return system, user


# names the model often uses for a canonical field (it answered "year" for a year column when the field is called `period`):
# accepted and translated here, so the pipeline does not depend on the model's exact wording
FIELD_ALIASES = {"year": "period", "years": "period", "month": "year_month", "date": "year_month", "rainfall": "rainfall_mm",
                 "precipitation": "rainfall_mm", "rain": "rainfall_mm", "crop_name": "crop", "district_name": "district",
                 "village_name": "village", "state_name": "state", "id": "record_id", "sample_id": "record_id"}


def parse_headers(raw, unmapped_keys) -> dict:
    if not isinstance(raw, dict):
        return {}
    keys = set(unmapped_keys)
    out = {}
    for k, v in raw.items():
        v = FIELD_ALIASES.get(str(v).strip().lower(), v) if isinstance(v, str) else v
        if k in keys and v in SYNONYMS:
            out[k] = v
    return out


def _ascii_digits(s):
    return "".join(str(unicodedata.digit(c)) if c.isdigit() else c for c in str(s))


def verify_header_proposal(proposal, rows):
    """The model proposes, the data decides: a column that holds only four-digit years is the YEAR (period), never the month (year_month),
    whatever the model called it. (Digits of any script are read: '2019', '٢٠١٩', '২০১৯'.)"""
    out = {}
    for key, canon in proposal.items():
        vals = [str(r["data"].get(key)).strip() for r in rows[:50] if r["data"].get(key) not in (None, "")]
        if canon == "year_month" and vals and all(re.fullmatch(r"(1[89]|20)\d{2}", _ascii_digits(v)) for v in vals):
            canon = "period"
        if not _fits_data(key, canon, vals):
            continue
        out[key] = canon
    return out


_NUMERIC = {"n", "p", "k", "oc", "ph", "ec", "s", "zn", "fe", "cu", "mn", "b", "rainfall_mm", "rainy_days", "area", "production", "yield",
            "latitude", "longitude"}
_TEXT = {"crop", "season", "village", "tehsil", "block", "state", "district", "category"}


def _is_num(v) -> bool:
    try:
        float(re.sub(r"[,\s]", "", _ascii_digits(v)))
        return True
    except ValueError:
        return False


def _fits_data(key, canon, vals) -> bool:
    """The model proposes, the data decides. A column is not what the model says if its header or its cells contradict it:
    a fertiliser product is not a soil nutrient, a milk / fish column is not crop production, a `crop` column does not hold numbers."""
    if canon in ("n", "p", "k", "s", "zn", "fe", "cu", "mn", "b", "oc", "ec", "ph") and FERTILIZER_TERMS & set(words_of(key)):
        return False
    if canon in ("production", "area", "yield") and non_crop_term(key):
        return False
    if not vals:
        return True
    share_num = sum(_is_num(v) for v in vals) / len(vals)
    if canon in _NUMERIC and share_num < 0.6:
        return False
    if canon in _TEXT and share_num > 0.6:
        return False
    return True


def classify_prompt(keys, sample_row, sample_rows=None, context=None):
    system = ("Classify an agricultural data table into exactly one of: soil (soil-health card / soil nutrient TEST results; not fertiliser sales "
              "or consumption), rainfall (precipitation by period), production (CROP area / production / yield; not livestock, dairy, fish, "
              "money, schemes or seed / fertiliser distribution), or none.")
    ctx = ("Text printed around the table / on the cover:\n" + "\n".join(context)[:500] + "\n") if context else ""
    rows = sample_rows or [sample_row]
    user = (ctx + f"Column names: {list(keys)[:40]}\nSample rows: {json.dumps(rows[:4], default=str, ensure_ascii=False)[:800]}\n"
            'Answer only JSON like {"domain": "soil"}.')
    return system, user


def parse_domain(raw):
    d = raw.get("domain") if isinstance(raw, dict) else None
    return d if d in DOMAINS else None


def llm_map_headers(llm, unmapped_keys, sample_row) -> dict:
    prompt = (
        "Map source column names to canonical fields. Column names and cell values may be in ANY language or script: first work out what "
        "each column means, then map it. Only map a column if you are confident; omit the rest.\n"
        "Canonical fields (name: meaning):\n" + "\n".join(f"- {f}: {FIELD_HELP.get(f, '')}" for f in CANONICAL_FIELDS) +
        "\nMap EVERY column whose meaning matches a canonical field, including month and year columns.\n"
        f"Source columns: {list(unmapped_keys)[:40]}\n"
        f"One sample row: {json.dumps(sample_row, default=str)[:600]}\n"
        'Answer only JSON like {"Nitrogen_avail": "n", "Dist": "district"}.')
    try:
        raw = _json_from(llm.invoke(prompt).content)
    except Exception:
        return {}
    return {k: v for k, v in raw.items() if k in set(unmapped_keys) and v in SYNONYMS}
