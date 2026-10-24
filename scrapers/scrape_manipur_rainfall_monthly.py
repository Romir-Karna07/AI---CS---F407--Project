"""
Monthly rainfall (mm) for every Manipur district, from IMD Pune's official gridded
daily rainfall product (0.25 x 0.25 degree, https://imdpune.gov.in/), downloaded with
the `imdlib` package.

District values are DERIVED, not station records: for each district, the daily grid
values are averaged over every grid cell the district overlaps (weighted by overlap
area, using IMD's own district polygons from mausam.imd.gov.in), then summed per month.
Grid cells are ~25 km wide, so small districts (Imphal West/East, Bishnupur, Thoubal,
Kakching) are represented by a handful of cells and are smooth estimates.

Why this source: the portal pages only show the current period, India-WRIS has a revoked
TLS certificate, the India Data Portal mirror of WRIS district data has corrupt "actual"
values (a constant ~1/32 of "normal"), and IndiaStat/NEDFi tables are login-gated.

Usage: python scrape_manipur_rainfall_monthly.py [first_year] [last_year]
Output: manipur_rainfall_monthly.json
"""
import json
import sys
from datetime import date

import imdlib as imd
import numpy as np
import requests
from shapely.geometry import box, shape
from shapely.validation import make_valid

SHAPE_URL = "https://mausam.imd.gov.in/imd_latest/contents/district_shapefiles/Manipur.json"
CACHE_DIR = "imd_grid_cache"
OUT_FILE = "manipur_rainfall_monthly.json"
FIRST_YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2015
LAST_YEAR = int(sys.argv[2]) if len(sys.argv) > 2 else date.today().year


def load_year(year):
    try:
        return imd.get_data("rain", year, year, fn_format="yearwise", file_dir=CACHE_DIR).get_xarray()
    except Exception as e:  # year not published yet
        print(f"{year}: not available ({type(e).__name__}: {e})")
        return None


def district_weights(geom, lats, lons, step=0.25):
    """{(lat_idx, lon_idx): overlap area} for grid cells (centres lats/lons) touching geom."""
    w = {}
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            cell = box(lo - step / 2, la - step / 2, lo + step / 2, la + step / 2)
            if cell.intersects(geom):
                a = cell.intersection(geom).area
                if a > 0:
                    w[(i, j)] = a
    return w


def main():
    features = requests.get(SHAPE_URL, timeout=60).json()["features"]
    districts = {f["properties"]["DISTRICT"]: make_valid(shape(f["geometry"])) for f in features}

    records = {name: {} for name in districts}  # name -> {"YYYY-MM": {...}}
    weights = None
    for year in range(FIRST_YEAR, LAST_YEAR + 1):
        ds = load_year(year)
        if ds is None:
            continue
        ds = ds.sel(lat=slice(23.5, 26.0), lon=slice(92.5, 95.0))
        lats, lons = ds["lat"].values, ds["lon"].values
        if weights is None:
            weights = {n: district_weights(g, lats, lons) for n, g in districts.items()}
            print({n: len(w) for n, w in weights.items()}, "grid cells per district")
        rain = ds["rain"].values  # (time, lat, lon), NaN = no data
        months = ds["time"].values.astype("datetime64[M]")
        days = ds["time"].values.astype("datetime64[D]")
        for name, w in weights.items():
            idx = list(w)
            wt = np.array([w[k] for k in idx])
            cells = np.stack([rain[:, i, j] for i, j in idx], axis=1)  # (time, ncells)
            valid = ~np.isnan(cells)
            daily = np.where(valid, cells, 0) @ wt / np.where(valid @ wt == 0, np.nan, valid @ wt)
            for m in np.unique(months):
                sel = months == m
                d = daily[sel]
                records[name][str(m)] = {
                    "rainfall_mm": None if np.isnan(d).all() else round(float(np.nansum(d)), 1),
                    "rainy_days_ge_2.5mm": int(np.nansum(d >= 2.5)),
                    "days_with_data": int((~np.isnan(d)).sum()),
                    "days_in_month": int(sel.sum()),
                }
        print(f"{year}: done ({days[0]} .. {days[-1]})", flush=True)

    result = {
        "source": "IMD Pune gridded daily rainfall (0.25 deg), https://imdpune.gov.in/, via imdlib",
        "method": "area-weighted mean of overlapping grid cells per district (IMD district polygons), "
                  "daily values summed per month",
        "unit": "mm",
        "caveat": "Derived estimates from a gridded analysis, not rain-gauge readings; "
                  "can differ from IMD's district station-based statistics.",
        "districts": [
            {"district": n, "monthly": [{"month": m, **v} for m, v in sorted(rec.items())]}
            for n, rec in sorted(records.items())
        ],
    }
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print("Saved", OUT_FILE)


if __name__ == "__main__":
    main()
