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

from .concepts import SYNONYMS

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "openai/gpt-oss-120b"

CANONICAL_FIELDS = sorted(SYNONYMS)
FIELD_HELP = {
    "year_month": "calendar month of the record (month name, month number or YYYY-MM)", "period": "year, agricultural year (2019-20) or survey cycle",
    "district": "district name", "district_code": "district code", "state": "state name", "crop": "crop name", "season": "crop season (Kharif / Rabi ...)",
    "rainfall_mm": "amount of rainfall / precipitation", "rainy_days": "number of rainy days", "area": "cultivated / sown area",
    "production": "crop production (quantity harvested)", "yield": "crop yield per unit area", "village": "village name", "tehsil": "tehsil / sub-district",
    "block": "block name", "category": "fertility rating (Low / Medium / High, Sufficient / Deficient)", "latitude": "latitude", "longitude": "longitude",
    "n": "available nitrogen", "p": "available phosphorus", "k": "available potassium", "oc": "organic carbon", "ph": "soil pH", "ec": "electrical conductivity",
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


def headers_prompt(unmapped_keys, sample_row):
    system = ("Map source column names to canonical fields. Column names and cell values may be in ANY language or script: first work out what "
              "each column means, then map it. Only map a column if you are confident; omit the rest.")
    user = ("Canonical fields (name: meaning):\n" + "\n".join(f"- {f}: {FIELD_HELP.get(f, '')}" for f in CANONICAL_FIELDS) +
            "\nMap EVERY column whose meaning matches a canonical field, including month and year columns.\n"
            f"Source columns: {list(unmapped_keys)[:40]}\n"
            f"One sample row: {json.dumps(sample_row, default=str, ensure_ascii=False)[:600]}\n"
            'Answer only JSON like {"Nitrogen_avail": "n", "Dist": "district"}.')
    return system, user


def parse_headers(raw, unmapped_keys) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in set(unmapped_keys) and v in SYNONYMS}


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
        out[key] = canon
    return out


def classify_prompt(keys, sample_row):
    system = ("Classify an agricultural data table into exactly one of: soil (soil-health card / nutrient tests), "
              "rainfall (precipitation by period), production (crop area / production / yield), or none.")
    user = (f"Column names: {list(keys)[:40]}\nOne sample row: {json.dumps(sample_row, default=str, ensure_ascii=False)[:600]}\n"
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
