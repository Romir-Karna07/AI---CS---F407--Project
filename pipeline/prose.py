"""
Prose extraction: figures that sit inside sentences ("In 2019-20 Thoubal produced 9,000 tonnes of rice from 3,500 hectares ...").

The model reads a chunk of text (any language) and returns structured facts, each with the VERBATIM sentence it came from. Fixed code then
refuses anything it cannot ground:
  * the quote must occur in the source text (whitespace-insensitive) - its character offsets become the stored provenance locator
    (`chars 120-260`, or `p5 chars 120-260` inside a PDF page), so a value can be opened at the exact place;
  * every number the model returns must literally occur in that quote (numbers written only in words are dropped, not guessed);
  * for text in a Latin script the district / year / crop must also occur in the chunk (a non-Latin chunk is translated by the model, so the
    identity check cannot be done and is skipped - the numbers and the quote are still verified);
  * units must come from a fixed vocabulary; district names are normalised by the usual lookup.
Facts become the same neutral records the table readers produce, so the unchanged classifier, agents and loader take over.
"""
import re
import unicodedata

from .normalize import UNSPECIFIED, _districts, _key, normalize_district
from .readers import read_text, to_number
from .trace import traceable

CHUNK = 3000
CANON = [d["name"] for d in _districts["districts"]]
UNITS = {"area": ["ha", "acre", "sq km", "'000 ha", "lakh ha"], "production": ["t", "kg", "quintal", "'000 t", "lakh t"],
         "yield": ["t/ha", "kg/ha", "q/ha"], "rainfall": ["mm", "cm", "inch"]}
SOIL_FIELDS = ["n", "p", "k", "ph", "oc", "ec", "s", "zn", "fe", "cu", "mn", "b"]
SYSTEM = (
    "You extract agricultural facts from text that may be in ANY language or script. Extract only facts the text states explicitly.\n"
    "Fact types:\n"
    "- production: district, crop, period (e.g. 2019-20 or 2019), values area / production / yield\n"
    "- rainfall: district, period as YYYY-MM (monthly values only), value rainfall\n"
    "- soil: district, village, period, values among n p k ph oc ec s zn fe cu mn b\n"
    f"District: translate to English and choose from {CANON}; null if the text names none of them.\n"
    "Crop: English name. If one sentence gives figures for several districts or periods, output ONE fact per district / period. "
    "Numbers: copy EXACTLY as written, as a JSON string (e.g. \"9,000\"; keep the original digits of any script); never convert, round or "
    "compute; if a number is written in words, skip that value. "
    "If the text says 'thousand hectares' use the unit \"'000 ha\"; likewise \"lakh ha\", \"'000 t\", \"lakh t\".\n"
    f"Allowed units: area {UNITS['area']}, production {UNITS['production']}, yield {UNITS['yield']}, rainfall {UNITS['rainfall']}.\n"
    "quote = the exact sentence or clause copied from the text, in the ORIGINAL language, containing the numbers.\n"
    "Answer ONLY with JSON: {\"facts\":[{\"type\":\"production\",\"district\":\"Thoubal\",\"crop\":\"Rice\",\"period\":\"2019-20\",\"village\":null,"
    "\"values\":{\"area\":{\"value\":3500,\"unit\":\"ha\"},\"production\":{\"value\":9000,\"unit\":\"t\"}},\"quote\":\"...\"}]}. "
    "A rainfall fact looks like {\"type\":\"rainfall\",\"district\":\"Ukhrul\",\"crop\":null,\"period\":\"2019-01\",\"village\":null,"
    "\"values\":{\"rainfall\":{\"value\":\"12.5\",\"unit\":\"mm\"}},\"quote\":\"...\"}. "
    "If there are no such facts answer {\"facts\":[]}.")


def _nfkc(s):
    return unicodedata.normalize("NFKC", s)


def numbers_in(text):
    out = set()
    for m in re.finditer(r"\d[\d,.٫٬]*", _nfkc(text)):
        v = to_number(m.group(0).rstrip(".,"))
        if v is not None:
            out.add(round(v, 6))
    return out


def is_latin_text(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    return sum(1 for c in letters if "LATIN" in unicodedata.name(c, "")) / len(letters) >= 0.7


def locate(quote, text):
    """(start, end) of the quote inside text, tolerant to whitespace differences; None if it is not there"""
    toks = quote.split()
    if not toks:
        return None
    m = re.search(r"\s+".join(re.escape(t) for t in toks), text)
    return (m.start(), m.end()) if m else None


def chunks_of(text, size=CHUNK):
    """[(offset, chunk)] split at paragraph / sentence boundaries; chunks without any digit are dropped"""
    out, pos = [], 0
    while pos < len(text):
        end = min(len(text), pos + size)
        if end < len(text):
            cut = max(text.rfind("\n\n", pos, end), text.rfind(". ", pos, end), text.rfind("\n", pos, end))
            end = cut + 1 if cut > pos + size // 2 else end
        piece = text[pos:end]
        if re.search(r"\d", piece):
            out.append((pos, piece))
        pos = end
    return out


def _grounded(fact, chunk, latin):
    """identity fields must occur in a Latin-script chunk"""
    key = _key(chunk)
    d = fact.get("district")
    if latin and d:
        canon, code, _ = normalize_district(d)
        toks = [canon] + [a for dd in _districts["districts"] if dd["name"] == canon for a in dd["aliases"]] if code != UNSPECIFIED else [d]
        if not any(_key(t) in key for t in toks if t):
            return False
    per = str(fact.get("period") or "")
    y = re.search(r"(19|20)\d{2}", per)
    if latin and y and y.group(0) not in _nfkc(chunk):
        return False
    crop = fact.get("crop")
    if latin and crop and not any(w.lower() in chunk.lower() for w in re.findall(r"[A-Za-z]{3,}", str(crop))):
        return False
    return True


def _record(fact, locator):
    t = fact["type"]
    vals = fact["values"]
    data, label = {}, {}
    if fact.get("district"):
        data["District"] = fact["district"]
    if t == "production":
        if fact.get("crop"):
            data["Crop"] = fact["crop"]
        if fact.get("period"):
            data["Period"] = fact["period"]
        for k, name in (("area", "Area"), ("production", "Production"), ("yield", "Yield")):
            if k in vals:
                unit = vals[k].get("unit")
                data[f"{name} ({unit})" if unit else name] = vals[k]["value"]
    elif t == "rainfall":
        if fact.get("period"):
            data["Month"] = fact["period"]
        if "rainfall" in vals:
            unit = vals["rainfall"].get("unit")
            data[f"Rainfall ({unit})" if unit else "Rainfall"] = vals["rainfall"]["value"]
    else:
        if fact.get("village"):
            data["Village"] = fact["village"]
        if fact.get("period"):
            data["Period"] = fact["period"]
        for k in SOIL_FIELDS:
            if k in vals:
                data[k.upper() if k != "ph" else "pH"] = vals[k]["value"]
    return {"locator": locator, "data": data, "labels": [], "units": {}, "context": []}


def verify_facts(answer, chunk, source_text, prefix):
    """-> (records by type, rejected: [(reason, quote)])"""
    recs, rejected = {}, []
    latin = is_latin_text(chunk)
    facts = answer.get("facts") if isinstance(answer, dict) else None
    for f in facts or []:
        if isinstance(f, dict) and not isinstance(f.get("values"), dict) and "value" in f and f.get("type") == "rainfall":
            f = dict(f, values={"rainfall": {"value": f["value"], "unit": f.get("unit")}})          # flat shape for the one-variable fact type
        if not isinstance(f, dict) or f.get("type") not in ("production", "rainfall", "soil") or not isinstance(f.get("values"), dict):
            rejected.append(("fact is not in the expected shape", str(f)[:60]))
            continue
        quote = str(f.get("quote") or "")
        span = locate(quote, source_text) if quote else None
        if span is None:
            rejected.append(("quote is not in the source", quote[:60]))
            continue
        qnums = numbers_in(quote)
        clean = {}
        for k, v in f["values"].items():
            if not isinstance(v, dict):
                continue
            raw = v.get("value")
            num = to_number(raw) if isinstance(raw, str) else (float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None)
            if num is None:
                continue
            if round(num, 6) not in qnums:                       # the figure must literally occur in the quoted text
                rejected.append((f"value {raw} is not in the quote", quote[:60]))
                continue
            unit = v.get("unit")
            clean[k] = {"value": num, "unit": unit if unit in UNITS.get(k, []) else None}
        if not clean:
            continue
        f = dict(f, values=clean)
        if not _grounded(f, chunk, latin):
            rejected.append(("district / period / crop is not in the text", quote[:60]))
            continue
        if f.get("district"):
            canon, code, _ = normalize_district(f["district"])
            f["district"] = canon if code != UNSPECIFIED else f["district"]
        rec = _record(f, f"{prefix}chars {span[0]}-{span[1]}")
        recs.setdefault(f["type"], []).append(rec)
    return recs, rejected


@traceable("prose_extract", "chain")
def extract(ai, text, prefix=""):
    """-> (groups, notes). One model call per chunk (capped by the AI budget)."""
    groups_by_type, notes, rej_total, n_chunks = {}, [], 0, 0
    for offset, chunk in chunks_of(text):
        n_chunks += 1
        ans = ai.ask("prose", SYSTEM, f"TEXT\n{chunk}", {"chunk": n_chunks, "chars": len(chunk), "at": prefix + str(offset)})
        if ans is None:
            if ai.last_error == "budget exhausted":
                notes.append(f"prose: stopped after {n_chunks - 1} chunk(s), model budget exhausted")
                break
            continue
        recs, rejected = verify_facts(ans, chunk, chunk, prefix)
        # offsets inside `chunk` -> offsets in the whole text
        for t, rs in recs.items():
            for r in rs:
                m = re.search(r"chars (\d+)-(\d+)$", r["locator"])
                a, b = int(m.group(1)) + offset, int(m.group(2)) + offset
                r["locator"] = r["locator"][:m.start()] + f"chars {a}-{b}"
                groups_by_type.setdefault(t, []).append(r)
        rej_total += len(rejected)
    groups = [{"pattern": f"prose:{t}", "rows": rs} for t, rs in groups_by_type.items()]
    for g in groups:
        for r in g["rows"]:
            r["pattern"] = g["pattern"]
    notes.append(f"prose: {n_chunks} chunk(s), {sum(len(g['rows']) for g in groups)} verified fact(s), {rej_total} rejected")
    return groups, notes


def read_prose_file(path, ai):
    text, enc = read_text(path)
    groups, notes = extract(ai, text, "")
    if not groups:
        raise ValueError("no verifiable agricultural facts found in the text" + (f" ({'; '.join(notes)})" if notes else ""))
    return {"format": "prose", "encoding": enc, "notes": notes, "groups": groups}
