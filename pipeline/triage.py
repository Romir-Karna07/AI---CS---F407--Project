"""
Triage: what kind of file is this, and which reader should take it?

Deterministic first - magic bytes (PDF, ZIP/xlsx, images ...), then a content profile of the text (HTML tags, JSON braces, delimiter consistency,
sentence-like lines). Only when the profile is genuinely ambiguous does the model decide (`llm_triage`), and its answer must be one of the routes
the code can actually read. Files the pipeline cannot read are refused with a clear message instead of being fed to the wrong parser.

Routes: csv | json | xlsx | html | pdf | image | prose        (anything else -> unsupported, run fails cleanly with the reason)
"""
import zipfile
from pathlib import Path

from .trace import traceable

SUPPORTED = "CSV/TSV, JSON/JSONL, Excel (.xlsx), HTML, PDF (text or scanned), images of tables, plain text with figures"
ROUTES = ("csv", "json", "xlsx", "html", "pdf", "image", "prose")
IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"II*\x00", b"MM\x00*", b"BM")


def _decode(head: bytes):
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return head.decode("utf-16")
        except UnicodeDecodeError:
            return None
    for cut in (0, 1, 2, 3):
        try:
            return (head[:len(head) - cut] if cut else head).decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(head).best()
        return str(best) if best is not None else None
    except Exception:
        return None


def _text_profile(text: str):
    lines = [l for l in text.splitlines() if l.strip()][:200]
    if not lines:
        return {"lines": 0}
    best = (0.0, None, 0)
    for d in (",", ";", "\t", "|"):
        counts = [l.count(d) for l in lines]
        mode = max(set(counts), key=counts.count)
        share = counts.count(mode) / len(counts)
        if mode >= 1 and share > best[0]:
            best = (share, d, mode)
    avg_len = sum(len(l) for l in lines) / len(lines)
    words = sum(len(l.split()) for l in lines) / len(lines)
    sentences = sum(l.count(". ") + l.rstrip().endswith(".") for l in lines) / len(lines)
    return {"lines": len(lines), "csv_share": round(best[0], 2), "delim": best[1], "fields": best[2], "avg_len": round(avg_len),
            "avg_words": round(words, 1), "sentence_rate": round(sentences, 2)}


def sniff(path):
    """-> {type, confidence, how, scripts, head_text, profile, reason}. type is one of ROUTES or 'unsupported'."""
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(262144)
    ext = path.suffix.lower()
    out = {"type": None, "confidence": "high", "how": "magic", "size": size, "ext": ext, "head_text": None, "profile": {}, "reason": ""}
    if head[:5] == b"%PDF-":
        out["type"] = "pdf"
        return out
    if head[:4] == b"PK\x03\x04":
        try:
            names = zipfile.ZipFile(path).namelist()
        except zipfile.BadZipFile:
            out.update(type="unsupported", reason="corrupt zip / Office file")
            return out
        if any(n.startswith("xl/") for n in names):
            out["type"] = "xlsx"
        elif any(n.startswith("word/") for n in names):
            out.update(type="unsupported", reason="Word document (.docx): save the table as .xlsx or .csv, or export to PDF")
        else:
            out.update(type="unsupported", reason="zip archive: extract it first")
        return out
    if head[:4] == b"\xd0\xcf\x11\xe0":
        out.update(type="unsupported", reason="legacy Office file (.xls / .doc): save it as .xlsx")
        return out
    if head[:2] == b"\x1f\x8b":
        out.update(type="unsupported", reason="gzip-compressed file: decompress it first")
        return out
    if any(head.startswith(m) for m in IMAGE_MAGIC) or head[8:12] == b"WEBP":
        out["type"] = "image"
        return out
    if not head.strip():
        out.update(type="csv", how="empty")                      # empty / blank file: the CSV reader fails it cleanly
        return out
    text = _decode(head)
    if text is None or (b"\x00" in head[:4096] and head[:2] not in (b"\xff\xfe", b"\xfe\xff")):
        out.update(type="unsupported", reason="binary file (audio, video or unknown format): not a table or a text document")
        return out
    out["head_text"] = text
    out["how"] = "content"
    low = text.lstrip().lower()
    if low.startswith(("<!doctype html", "<html", "<table", "<body", "<head", "<?xml")) or ("<table" in low[:20000] and "</" in low[:20000]):
        out["type"] = "html"
        return out
    if text.lstrip()[:1] in ("{", "[") or ext in (".json", ".geojson", ".jsonl", ".ndjson"):
        out["type"] = "json"
        return out
    prof = _text_profile(text)
    out["profile"] = prof
    if prof["lines"] == 0:
        out["type"] = "csv"
        return out
    tabular = prof["csv_share"] >= 0.5 and prof["lines"] >= 3 and prof["avg_words"] <= 25
    proselike = prof["avg_len"] >= 60 and (prof["sentence_rate"] >= 0.5 or prof["avg_words"] >= 12)
    if prof["csv_share"] >= 0.9 and prof["lines"] >= 3 and prof["avg_words"] <= 8:
        out["type"] = "csv"                                          # fixed-width exports (many empty fields) are tables, not prose
    elif ext in (".csv", ".tsv") and not proselike:
        out["type"] = "csv"
    elif tabular and not proselike:
        out["type"] = "csv"
    elif proselike and not tabular:
        out["type"] = "prose"
    else:
        out.update(type="csv", confidence="low", how="ambiguous")   # legacy default, unless the model decides otherwise
    return out


TRIAGE_SYSTEM = ("You route a file to the right reader in a data-extraction pipeline. The text may be in any language or script. "
                 "Routes: csv (rows of delimited fields, possibly with title lines above the header), prose (sentences or paragraphs that "
                 "state figures; not a table), html (markup), json, unsupported (not tabular and not a text document with figures). "
                 "Answer ONLY with JSON: {\"route\": \"csv|prose|html|json|unsupported\", \"reason\": \"short\", \"confidence\": 0.0-1.0}")


@traceable("triage", "chain")
def llm_triage(ai, info, name):
    """ask the model only for an ambiguous text file; answer must be an allowed route"""
    sample = (info.get("head_text") or "")[:1500]
    ans = ai.ask("triage", TRIAGE_SYSTEM, f"File name: {name}\nProfile: {info.get('profile')}\nFirst characters:\n{sample}", {"file": name})
    route = ans.get("route") if isinstance(ans, dict) else None
    if route in ("csv", "prose", "html", "json"):
        return route, str(ans.get("reason", ""))[:120]
    if route == "unsupported":
        return "unsupported", str(ans.get("reason", ""))[:120]
    return None, ""
