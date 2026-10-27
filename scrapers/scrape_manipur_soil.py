"""
Scrape Soil Health Card (SHC) data for every Manipur district from
https://soilhealth.dac.gov.in/slusi-visualisation/

How the portal works (reverse-engineered from its JS bundle):
  * GraphQL (soilhealth4.dac.gov.in)  -> list of districts for a state
  * GET  /public/layers               -> SHC cycles + bbox per district
  * GeoServer WMS (layer "<state>_<district>_shc_<cycle>") -> SHC points.
    WFS is disabled, so the only way to read attributes is GetFeatureInfo
    (what the portal does on a map click). We render the layer with GetMap,
    find every point in the image, then run GetFeatureInfo on each one.

Output: manipur_soil_health.json
"""
import io
import json
import time

import numpy as np
import requests
from PIL import Image
from scipy import ndimage

STATE_ID = "63f870acd2e83876f03f0b46"  # MANIPUR
STATE_CODE = "14"
OUT_FILE = "manipur_soil_health.json"
IMG_PX = 2400  # longest side of the GetMap render

BASE = "https://soilhealth.dac.gov.in/"
LAYERS_URL = BASE + "q8ZdH3f0mX1y7nJrP2K5BvW9aQpLb-6TsFcYzC4oUtN_MwRiDgGZ0xVsJe7Xy8nMk2TjPqFbD1C5LvOr9WQ6Xa3lYsN7V1sRmJez4OtUbY0Qn9hPk6WfLd2Y8oSvK3UtGmX5C7bT9Pn6xV0JfZ1TzQm8LrV9aGkM2JpXeN4fUoQ8SwCiRzN7VtPk1XgW5/public/layers"
WMS_URL = BASE + "jW8X3zM5Y7pQvLr4K2Tn6HqPbD0tZmN9R6JfO1wCiG8xV5eTk2CdMoF9YsQr0Z7LmN1YxU4pTb2K5LvHqX7F3aCmGzR4Pw0D8UtYnJ9oZ2SvNlQ7Tz1PjR5LcX0Qf8HkV9OrG4V7YxU3pJk6TnMm5CdX8B9tRi1Lw2Qn7F4ZzJk8WvP1GrZ6Sx0JoH5C3oV7fNi2/shc/wms"
GRAPHQL_URL = "https://soilhealth4.dac.gov.in/"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://soilhealth.dac.gov.in/slusi-visualisation/",
}
session = requests.Session()
session.headers.update(HEADERS)


def get(url, **kw):
    for attempt in range(4):
        try:
            r = session.get(url, timeout=180, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


def get_districts():
    query = (
        "query GetdistrictAndSubdistrictBystate($getdistrictAndSubdistrictBystateId: String, "
        "$name: String, $state: ID, $subdistrict: Boolean, $code: String, "
        "$aspirationaldistrict: Boolean) {\n  getdistrictAndSubdistrictBystate("
        "id: $getdistrictAndSubdistrictBystateId name: $name state: $state "
        "subdistrict: $subdistrict code: $code aspirationaldistrict: $aspirationaldistrict)\n}"
    )
    body = {
        "operationName": "GetdistrictAndSubdistrictBystate",
        "variables": {"state": STATE_ID},
        "query": query,
    }
    r = session.post(GRAPHQL_URL, json=body, timeout=60)
    r.raise_for_status()
    return r.json()["data"]["getdistrictAndSubdistrictBystate"]


def feature_info(layer, bbox, size, x, y):
    minx, miny, maxx, maxy = bbox
    params = {
        "service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo",
        "format": "image/png", "transparent": True,
        "layers": layer, "query_layers": layer, "srs": "EPSG:4326",
        "width": size[0], "height": size[1], "X": x, "Y": y,
        "feature_count": 50, "info_format": "application/json",
        "bbox": f"{minx},{miny},{maxx},{maxy}",
    }
    return get(WMS_URL, params=params).json().get("features", [])


def scrape_layer(layer, bbox):
    """Return {feature_id: record} for one SHC layer."""
    minx, miny, maxx, maxy = bbox
    # pad the bbox a little so edge points aren't clipped
    padx, pady = (maxx - minx) * 0.02, (maxy - miny) * 0.02
    bbox = (minx - padx, miny - pady, maxx + padx, maxy + pady)
    minx, miny, maxx, maxy = bbox
    ratio = (maxx - minx) / (maxy - miny)
    w, h = (IMG_PX, max(1, int(IMG_PX / ratio))) if ratio >= 1 else (max(1, int(IMG_PX * ratio)), IMG_PX)

    img = get(WMS_URL, params={
        "service": "WMS", "version": "1.1.1", "request": "GetMap",
        "layers": layer, "styles": "", "format": "image/png", "transparent": True,
        "srs": "EPSG:4326", "width": w, "height": h,
        "bbox": f"{minx},{miny},{maxx},{maxy}",
    }).content
    mask = np.array(Image.open(io.BytesIO(img)).convert("RGBA"))[..., 3] > 0
    labels, n = ndimage.label(mask)

    records = {}
    for i in range(1, n + 1):
        ys, xs = np.nonzero(labels == i)
        # query the centre plus extremities, in case several points merged into one blob
        probes = {(int(xs.mean()), int(ys.mean()))}
        if len(xs) > 150:
            for k in (xs.argmin(), xs.argmax(), ys.argmin(), ys.argmax()):
                probes.add((int(xs[k]), int(ys[k])))
        for px, py in probes:
            for f in feature_info(layer, bbox, (w, h), px, py):
                lon = minx + (px + 0.5) / w * (maxx - minx)
                lat = maxy - (py + 0.5) / h * (maxy - miny)
                rec = dict(f["properties"])
                rec["feature_id"] = f["id"]
                rec["approx_longitude"] = round(lon, 5)
                rec["approx_latitude"] = round(lat, 5)
                records.setdefault(f["id"], rec)
    return records, n


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v if v != "" else None


def main():
    districts = sorted(get_districts(), key=lambda d: d["name"])
    result = {"source": "https://soilhealth.dac.gov.in/slusi-visualisation/",
              "state": "MANIPUR", "state_code": STATE_CODE, "districts": []}
    for d in districts:
        meta = get(LAYERS_URL, params={"state_code": STATE_CODE, "district_code": d["code"]}).json()
        bbox = tuple(meta["bbox"][k] for k in ("minx", "miny", "maxx", "maxy"))
        entry = {"district": d["name"], "district_code": d["code"],
                 "aspirational": bool(d.get("aspirationaldistrict")),
                 "bbox": meta["bbox"], "shc_cycles": {}}
        for cycle in meta["shcLayers"]:
            cycle = cycle["cycle"] if isinstance(cycle, dict) else cycle
            layer = f"{STATE_CODE}_{d['code']}_shc_{cycle}"
            records, blobs = scrape_layer(layer, bbox)
            rows = [{k: (to_float(v) if k not in ("DISTRICT", "STATE", "TEHSIL", "VILLAGE", "Category",
                                                  "feature_id", "DISTRICT_L", "STATE_LGD") else v)
                     for k, v in r.items()} for r in records.values()]
            rows.sort(key=lambda r: int(r["feature_id"].rsplit(".", 1)[-1]))
            entry["shc_cycles"][cycle] = rows
            print(f"{d['name']:14} {cycle:8} blobs={blobs:4} records={len(rows)}", flush=True)
        if not meta["shcLayers"]:
            print(f"{d['name']:14} no SHC data published on portal", flush=True)
        result["districts"].append(entry)

    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print("Saved", OUT_FILE)


if __name__ == "__main__":
    main()
