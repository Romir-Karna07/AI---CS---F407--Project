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
from pathlib import Path

from .concepts import SYNONYMS

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "openai/gpt-oss-120b"

CANONICAL_FIELDS = sorted(SYNONYMS)
DOMAINS = ["soil", "rainfall", "production"]


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
        return ChatGroq(model=os.getenv("AGRI_LLM_MODEL", DEFAULT_MODEL), temperature=0, max_retries=2, timeout=30)
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


def llm_map_headers(llm, unmapped_keys, sample_row) -> dict:
    prompt = (
        "Map source column names to canonical fields. Only map a column if you are confident; omit the rest.\n"
        f"Canonical fields: {CANONICAL_FIELDS}\nSource columns: {list(unmapped_keys)[:40]}\n"
        f"One sample row: {json.dumps(sample_row, default=str)[:600]}\n"
        'Answer only JSON like {"Nitrogen_avail": "n", "Dist": "district"}.')
    try:
        raw = _json_from(llm.invoke(prompt).content)
    except Exception:
        return {}
    return {k: v for k, v in raw.items() if k in set(unmapped_keys) and v in SYNONYMS}
