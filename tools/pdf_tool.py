"""LangChain tool: read tables from a PDF (text layer or scan) into normalised records - the document_agent's reader."""
import json
from pathlib import Path

from langchain_core.tools import tool

from pipeline.pdf_reader import read_pdf_file


@tool
def read_pdf_data(file_path: str, max_rows: int = 50, backend: str = "auto", pages: str = "") -> str:
    """Reads the tables of a PDF and returns JSON: {"notes", "groups": [{"pattern", "total_records"}], "records"}.
    Each group is one table of one page; each record has `locator` ('p3 t1 line 7, cols B-F' = page 3, table 1, row 7),
    `data` (column -> value), `labels` (district / period read from the caption above the table) and `units`.
    backend="auto" uses ruled-table detection, then whitespace tables, then OCR for pages without a text layer;
    backend="ocr" forces OCR on every page. `pages` ("53-60" or "161") limits the pages read; default is the first 40. At most `max_rows` records are returned inline."""
    try:
        parsed = read_pdf_file(Path(file_path), backend=backend, pages=pages or None)
        groups = [{"pattern": g["pattern"], "total_records": len(g["rows"])} for g in parsed["groups"]]
        sample = [r for g in parsed["groups"] for r in g["rows"]][:max_rows]
        return json.dumps({"notes": parsed["notes"], "groups": groups, "records": sample}, default=str)
    except Exception as e:
        return f"Tool Error reading PDF: {e}"
