"""LangChain tool: read any CSV (single-row or multi-row/wide headers) into normalised records."""
import json
from pathlib import Path

from langchain_core.tools import tool

from pipeline.readers import read_csv_file


@tool
def read_csv_data(file_path: str, max_rows: int = 50) -> str:
    """Reads a CSV file (tidy or wide/multi-row headers) and returns JSON: {"notes", "total_records", "records"}.
    Each record has `locator` (line/columns in the file), `data` (field -> value) and `units`. At most `max_rows`
    records are returned inline; the pipeline itself reads the full file."""
    try:
        parsed = read_csv_file(Path(file_path))
        rows = parsed["groups"][0]["rows"]
        return json.dumps({"notes": parsed["notes"], "total_records": len(rows), "records": rows[:max_rows]},
                          default=str)
    except Exception as e:
        return f"Tool Error reading CSV: {e}"
