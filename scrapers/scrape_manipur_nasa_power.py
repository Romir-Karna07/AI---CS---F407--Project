"""
Monthly rainfall (mm) for every Manipur district from NASA POWER (source S21), via its open daily API
(https://power.larc.nasa.gov/docs/services/api/ , no key needed).

One request per district at the CENTRE of the district's bounding box (taken from manipur_soil_health.json).
Parameter PRECTOTCORR = bias-corrected precipitation, mm/day, MERRA-2 based (~0.5 x 0.625 degree), so each value is a
modelled point estimate, NOT a gauge reading and NOT a district average. Daily values are summed per month here
(the daily -> monthly step the S21 row of the source map said was missing). -999 is POWER's fill value = no data.

Output has the same shape as manipur_rainfall_monthly.json so the unchanged rainfall_agent can read it:
  districts[].monthly[] = {month, rainfall_mm, rainy_days_ge_2.5mm, days_with_data, days_in_month}

Usage: python scrape_manipur_nasa_power.py [first_year] [last_year]
Output: manipur_rainfall_nasa_power.json (repo root)
"""
import calendar
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
API = "https://power.larc.nasa.gov/api/temporal/daily/point"
FIRST_YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2015
LAST_YEAR = int(sys.argv[2]) if len(sys.argv) > 2 else 2025
OUT_FILE = ROOT / "manipur_rainfall_nasa_power.json"

# the rainfall file spells Noney as NONEI; keep the same spelling set so aliases are exercised the same way
SOIL_TO_RAIN_NAME = {"NONEY": "NONEI"}


def district_points():
    doc = json.loads((ROOT / "manipur_soil_health.json").read_text(encoding="utf-8"))
    pts = {}
    for d in doc["districts"]:
        b = d["bbox"]
        pts[SOIL_TO_RAIN_NAME.get(d["district"], d["district"])] = (
            round((b["miny"] + b["maxy"]) / 2, 3), round((b["minx"] + b["maxx"]) / 2, 3))
    return dict(sorted(pts.items()))


def fetch(lat, lon):
    params = {"parameters": "PRECTOTCORR", "community": "AG", "longitude": lon, "latitude": lat,
              "start": f"{FIRST_YEAR}0101", "end": f"{LAST_YEAR}1231", "format": "JSON"}
    for attempt in range(4):
        try:
            r = requests.get(API, params=params, timeout=120)
            r.raise_for_status()
            return r.json()["properties"]["parameter"]["PRECTOTCORR"]
        except (requests.RequestException, KeyError, ValueError):
            if attempt == 3:
                raise
            time.sleep(3 * (attempt + 1))


def monthly(daily):
    by_month = defaultdict(list)
    for ymd, v in daily.items():
        by_month[ymd[:6]].append(None if v is None or v <= -998 else float(v))
    out = []
    for ym in sorted(by_month):
        vals = by_month[ym]
        y, m = int(ym[:4]), int(ym[4:])
        ok = [v for v in vals if v is not None]
        out.append({"month": f"{y:04d}-{m:02d}",
                    "rainfall_mm": round(sum(ok), 1) if ok else None,
                    "rainy_days_ge_2.5mm": sum(v >= 2.5 for v in ok),
                    "days_with_data": len(ok),
                    "days_in_month": calendar.monthrange(y, m)[1]})
    return out


def main():
    districts = []
    for name, (lat, lon) in district_points().items():
        districts.append({"district": name, "latitude": lat, "longitude": lon, "monthly": monthly(fetch(lat, lon))})
        print(f"{name:14} ({lat}, {lon})  {len(districts[-1]['monthly'])} months", flush=True)
        time.sleep(1)
    result = {
        "source": "NASA POWER daily API (PRECTOTCORR, community AG), https://power.larc.nasa.gov/docs/services/api/",
        "method": "daily precipitation at the centre of each district's bounding box, summed per month; "
                  "rainy day = daily value >= 2.5 mm",
        "unit": "mm",
        "caveat": "Modelled (MERRA-2 / satellite-derived) point estimate on a coarse grid, not a gauge reading or a district "
                  "average; weak for Manipur's hill microclimates. Not comparable one-to-one with the IMD-grid file.",
        "districts": districts,
    }
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Saved", OUT_FILE)


if __name__ == "__main__":
    main()
