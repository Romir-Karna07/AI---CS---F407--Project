"""LangChain tool: read any JSON / JSON Lines file (any nesting) into flat records with ancestor context."""
import json
from pathlib import Path

from langchain_core.tools import tool

from pipeline.readers import read_json


@tool
def read_json_data(file_path: str, max_rows: int = 50) -> str:
    """Reads a JSON file of any nesting and returns JSON: {"groups": [{"pattern", "total_records"}], "records"}.
    Each record has `locator` (JSON path), `data` (own fields plus ancestor fields such as district) and `labels`
    (dict keys that act as labels, e.g. a cycle name). At most `max_rows` records are returned inline."""
    try:
        parsed = read_json(Path(file_path))
        groups = [{"pattern": g["pattern"], "total_records": len(g["rows"])} for g in parsed["groups"]]
        sample = [r for g in parsed["groups"] for r in g["rows"]][:max_rows]
        return json.dumps({"notes": parsed["notes"], "groups": groups, "records": sample}, default=str)
    except Exception as e:
        return f"Tool Error reading JSON: {e}"
