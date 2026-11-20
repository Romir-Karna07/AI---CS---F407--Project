"""
Page selection for long PDFs ("which of these 476 pages should I read?").

The old reader read the first 40 pages and silently ignored the rest; choosing pages by hand (AGRI_PDF_PAGES) counted as a manual intervention.
Now:
  1. `prescan` reads EVERY page's text layer (cheap: ~0.02 s per page) and measures numeric density, table-like lines, key words and whether
     the page has any text at all.
  2. A deterministic score ranks pages and discards the obvious non-candidates (no figures, no key words).
  3. If a model is available it reads a ONE-LINE DIGEST per candidate page (first line, table-line count, key words, a sample numeric line;
     any language) in batches and returns the relevant pages and whether the figures sit in a table or in prose. Batches are capped, and every
     returned page number must be one of the candidates.
  4. The final list is at most `max_pages` pages (best deterministic score first), returned in page order.
Without a model the deterministic ranking alone is used. Scanned pages have no text layer to digest: they are ranked by position and the
result says so; a fully scanned long document is therefore limited to its first `max_pages` pages.
"""
import re

from .normalize import _districts
from .trace import traceable

SMALL_DOC = 15             # up to this many pages: read them all, no selection needed
BATCH = 40
MAX_BATCHES = 4
KEYWORDS = ("rainfall", "rain ", "precipitation", "nitrogen", "phosphorus", "potassium", "organic carbon", "soil", "production", "yield",
            "area", "hectare", "tonnes", "crop", "kharif", "rabi", "district", "manipur")
_DIST = [d["name"].lower() for d in _districts["districts"]]
_NUM = re.compile(r"\d[\d.,]*")


def prescan(path):
    import pymupdf
    infos = []
    doc = pymupdf.open(str(path))
    for i, page in enumerate(doc, 1):
        txt = page.get_text() or ""
        lines = [l.strip() for l in txt.splitlines() if l.strip()]
        words = txt.split()
        nums = [w for w in words if re.search(r"\d", w)]
        table_lines = [l for l in lines if len(_NUM.findall(l)) >= 3]
        low = txt.lower()
        kw = sorted({k.strip() for k in KEYWORDS if k in low} | {d for d in _DIST if d in low})
        infos.append({"page": i, "has_text": len(txt.strip()) >= 20, "chars": len(txt), "numeric_ratio": round(len(nums) / max(len(words), 1), 3),
                      "table_lines": len(table_lines), "kw": kw, "head": (lines[0][:80] if lines else ""),
                      "sample": (table_lines[0][:100] if table_lines else "")})
    doc.close()
    return infos


def score(p):
    if not p["has_text"]:
        return 0.5
    return min(p["table_lines"], 30) / 30 * 2.0 + min(p["numeric_ratio"], 0.6) + 0.4 * min(len(p["kw"]), 4)


def is_candidate(p):
    return (not p["has_text"]) or p["table_lines"] >= 2 or len(p["kw"]) >= 2 or p["numeric_ratio"] >= 0.25


SYSTEM = ("You choose which pages of a long document contain DATA about: soil test results (nutrients, pH), rainfall (monthly or yearly), or crop "
          "area / production / yield, at district or state level. Pages may be in any language or script. For each candidate page you get a "
          "digest: its first line, the number of table-like lines, key words found, and one sample numeric line. Ignore contents pages, indexes, "
          "narrative without figures and unrelated statistics (population, banking, roads ...). "
          "Answer ONLY with JSON: {\"pages\": [{\"page\": 12, \"kind\": \"table\" or \"prose\", \"reason\": \"short\"}]}. kind=table when the figures "
          "are laid out in rows and columns; kind=prose when they are inside sentences. List only relevant pages.")


def _digest(p):
    return f"[p{p['page']}] {p['head']!r} | tablelines={p['table_lines']} | kw={','.join(p['kw'][:5]) or '-'} | e.g. {p['sample']!r}"


@traceable("select_pages", "chain")
def select_pages(path, ai, max_pages=40):
    """-> {pages, prose_pages, method, total, candidates, notes}"""
    infos = prescan(path)
    total = len(infos)
    if total <= SMALL_DOC:
        return {"pages": [p["page"] for p in infos], "prose_pages": [], "method": "all pages (short document)", "total": total,
                "candidates": total, "notes": []}
    notes = []
    if not any(p["has_text"] for p in infos):
        notes.append("no page has a text layer (scanned document): pages cannot be ranked, the first pages are read")
        return {"pages": [p["page"] for p in infos[:max_pages]], "prose_pages": [], "method": "first pages (scanned)", "total": total,
                "candidates": total, "notes": notes}
    ranked = sorted((p for p in infos if is_candidate(p)), key=score, reverse=True)
    pool = ranked[:BATCH * MAX_BATCHES]
    by_page = {p["page"]: p for p in pool}
    chosen, prose, method = [], [], "deterministic score"
    if ai is not None and ai.available and pool:
        got_any = False
        ordered = sorted(pool, key=lambda p: p["page"])
        for b in range(0, len(ordered), BATCH):
            batch = ordered[b:b + BATCH]
            ans = ai.ask("page_select", SYSTEM, "Candidate pages:\n" + "\n".join(_digest(p) for p in batch),
                         {"batch": b // BATCH + 1, "pages": len(batch)})
            if not isinstance(ans, dict) or not isinstance(ans.get("pages"), list):
                continue
            got_any = True
            allowed = {p["page"] for p in batch}
            for item in ans["pages"]:
                pg = item.get("page") if isinstance(item, dict) else item
                if isinstance(pg, int) and pg in allowed:
                    chosen.append(pg)
                    if isinstance(item, dict) and item.get("kind") == "prose":
                        prose.append(pg)
        if got_any:
            method = "model over page digests"
        else:
            notes.append("page-selection model unavailable or unusable: deterministic ranking used")
    if not chosen:
        chosen = [p["page"] for p in ranked if score(p) >= 0.6][:max_pages] or [p["page"] for p in ranked[:max_pages]]
        prose = []
    if len(set(chosen)) > max_pages:
        keep = sorted(set(chosen), key=lambda pg: score(by_page.get(pg) or infos[pg - 1]), reverse=True)[:max_pages]
        notes.append(f"{len(set(chosen))} pages selected; the best {max_pages} are read")
        chosen = keep
    pages = sorted(set(chosen))
    return {"pages": pages, "prose_pages": sorted(set(prose) & set(pages)), "method": method, "total": total, "candidates": len(ranked),
            "notes": notes}
