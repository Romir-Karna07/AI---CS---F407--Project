"""
Crop or not? The production table stores CROP data only.

A table titled "Key production statistics" lists rice and pineapple next to milk, eggs, meat and fish, and a header such as "Production" fits
all of them. Which row labels are crops is decided here, in two steps:
  1. fixed code: a label that is a known crop name is a crop; a label containing a livestock / dairy / fishery word is not (pipeline.concepts);
  2. the model judges the labels code could not place (any language), answering with a closed category list. Its answer is used only for
     labels that really occur in the table and only with a category from the list.
Rows whose label is not a crop are left out of the production table and reported (never silently dropped, never stored).
"""
import re

from .concepts import NON_CROP_TERMS, words_of
from .normalize import _crop_syn

CATEGORIES = ["crop", "livestock", "fishery", "forestry", "other"]
# common crops and crop groups of Indian statistics (as printed); with the synonym file this is the fixed vocabulary
COMMON_CROPS = [
    "rice", "paddy", "wheat", "maize", "jowar", "bajra", "ragi", "barley", "millets", "small millets", "coarse cereals", "cereals", "foodgrains",
    "food grains", "total foodgrains", "pulses", "tur", "arhar", "gram", "urad", "moong", "masoor", "lentil", "pea", "peas", "rajma", "cowpea",
    "rice bean", "rice beans", "bean", "beans", "french bean", "oilseed", "oilseeds", "groundnut", "mustard", "rapeseed", "rapeseed and mustard",
    "rapeseed mustard", "sesamum", "sesame", "soybean", "soyabean", "linseed", "niger", "sunflower", "castorseed", "sugarcane", "sugar cane", "cotton",
    "jute", "mesta", "potato", "potatoes", "sweet potato", "tapioca", "cassava", "colocasia", "arum", "yam", "onion", "garlic", "ginger", "turmeric",
    "chilli", "chillies", "dry chillies", "red chillies", "coriander", "cardamom", "large cardamom", "black pepper", "king chilli", "tomato",
    "tomatoes", "cabbage", "cauliflower", "brinjal", "okra", "bhindi", "radish", "carrot", "pumpkin", "squash", "cucumber", "bottle gourd",
    "bitter gourd", "knol khol", "broccoli", "lettuce", "spinach", "mushroom", "fruit", "fruits", "vegetable", "vegetables", "spice", "spices",
    "banana", "pineapple", "orange", "oranges", "lemon", "lemons", "citrus", "papaya", "passion fruit", "mango", "litchi", "guava", "jackfruit",
    "plum", "peach", "pear", "kiwi", "apple", "grapes", "strawberry", "areca nut", "arecanut", "coconut", "tea", "coffee", "rubber", "cashew",
    "cashewnut", "flowers", "plantation crops", "horticulture crops"]
_KNOWN = set(_crop_syn) | {v.lower() for v in _crop_syn.values()} | set(COMMON_CROPS)
MAX_LABELS = 60

SYSTEM = (
    "The database you feed stores ONLY CROP data: harvested plant produce - cereals, pulses, oilseeds, fruits, vegetables, spices, plantation / "
    "fibre crops - and named groups of crops such as 'Coarse cereals'. For each row label of ONE table, say what it is: "
    "crop | livestock (milk, eggs, meat, poultry, animals, dairy, honey) | fishery | forestry | other (money, inputs such as seed or "
    "fertiliser, totals, anything that is not crop produce). Labels may be in any language; units or notes in brackets are only hints.\n"
    'Answer ONLY with JSON: {"labels": {"<label exactly as given>": "crop"}}')


def deterministic_verdict(label) -> str | None:
    """'crop' | 'non_crop' | None (cannot tell without the model)"""
    ws = words_of(re.sub(r"\([^)]*\)", " ", str(label)))
    if " ".join(ws) in _KNOWN:
        return "crop"
    if any(w in NON_CROP_TERMS for w in ws):
        return "non_crop"
    return None


def row_label(row, crop_src):
    """the text that names the crop of this record: the mapped crop column, else the structural label (dict key / heading) that is neither a
    year nor a district - the same rule the production agent uses to fill `crop`"""
    if crop_src is not None:
        return row["data"].get(crop_src)
    from .normalize import normalize_district
    from .readers import YEAR_RE
    from .normalize import indian_state
    for _, lab in row["labels"]:
        if not YEAR_RE.search(str(lab)) and normalize_district(lab)[2] not in ("exact", "alias", "fuzzy") and not indian_state(lab):
            return lab
    return None


def filter_rows(ai, rows, crop_src, context_lines, use_model):
    """-> (kept rows, excluded [{label, category, how}], unverified labels). use_model: whether labels the fixed vocabulary cannot place are put to the model"""
    labels = {}
    for r in rows:
        lab = row_label(r, crop_src)
        if lab is not None and str(lab).strip():
            labels.setdefault(str(lab).strip(), deterministic_verdict(lab))
    unknown = [l for l, v in labels.items() if v is None][:MAX_LABELS]
    judged = ai.memo.setdefault("crops", {}) if ai is not None else {}     # labels already judged earlier in this file are not asked again
    todo = [l for l in unknown if l not in judged]
    if todo and use_model and ai is not None and ai.available:
        user = "Table context:\n" + "\n".join(context_lines[:6])[:600] + "\n\nRow labels:\n" + "\n".join(f"- {l}" for l in todo)
        ans = ai.ask("crops", SYSTEM, user, {"labels": len(todo)})
        got = ans.get("labels") if isinstance(ans, dict) else None
        if isinstance(got, dict):
            judged.update({k: v for k, v in got.items() if k in todo and v in CATEGORIES})
    model_cat = {l: judged[l] for l in unknown if l in judged}
    kept, excluded, seen = [], [], set()
    for r in rows:
        lab = row_label(r, crop_src)
        key = str(lab).strip() if lab is not None else ""
        v = labels.get(key)
        cat = "crop" if v == "crop" else ("livestock / fishery" if v == "non_crop" else model_cat.get(key))
        if cat is not None and cat != "crop":
            if key not in seen:
                seen.add(key)
                excluded.append({"label": key, "category": cat, "how": "vocabulary" if v == "non_crop" else "model"})
            continue
        kept.append(r)
    unverified = [l for l in unknown if l not in model_cat]
    return kept, excluded, unverified
