#!/usr/bin/env python
"""Build the bundled ISO 3166-1 -> Geofabrik / Natural Earth index.

The runtime config loader (:mod:`openpois.config`) reads
``src/openpois/data/countries.csv`` so any ISO alpha-2 code can be turned into
a complete country profile without a hand-written ``countries/<CODE>.yaml``.

The CSV is generated from two public sources:

- Geofabrik's ``index-v1.json`` for the OSM extract, full-history and
  replication URLs.
- Natural Earth 1:10m admin-0 countries for the display name, ISO3 code and
  extent used by the site defaults.

Regenerate after Geofabrik adds or renames an extract::

    python scripts/build_country_index.py

Both inputs are downloaded unless ``--geofabrik-index`` / ``--natural-earth``
point at local copies.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import tempfile
import urllib.request
from pathlib import Path

import geopandas as gpd

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "src" / "openpois" / "data" / "countries.csv"
GEOFABRIK_INDEX_URL = "https://download.geofabrik.de/index-v1.json"
NATURAL_EARTH_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_10m_admin_0_countries.geojson"
)

# Geofabrik's index occasionally tags several unrelated extracts with the same
# ISO2 code (observed 2026-11 for MH and VU).  These explicit picks override
# the ambiguity; every other code maps to exactly one extract.
PREFERRED_EXTRACT = {
    "MH": "marshall-islands",
    "VU": "vanuatu",
}

FIELDNAMES = [
    "iso2",
    "iso3",
    "name",
    "geofabrik_id",
    "pbf_url",
    "history_url",
    "updates_url",
    "bbox_west",
    "bbox_south",
    "bbox_east",
    "bbox_north",
    "center_lon",
    "center_lat",
    "initial_zoom",
]


def _download(url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents = True, exist_ok = True)
    print(f"Downloading {url} ...")
    with urllib.request.urlopen(url, timeout = 120) as response:
        destination.write_bytes(response.read())
    return destination


def _iso_codes(properties: dict) -> list[str]:
    codes = properties.get("iso3166-1:alpha2") or []
    return [codes] if isinstance(codes, str) else list(codes)


def _geofabrik_extracts(features: list[dict]) -> dict[str, dict]:
    """Return ``iso2 -> Geofabrik feature``, resolving duplicate tags."""
    by_code: dict[str, list[dict]] = {}
    for feature in features:
        for code in _iso_codes(feature.get("properties", {})):
            by_code.setdefault(code, []).append(feature)

    extracts: dict[str, dict] = {}
    for code, candidates in sorted(by_code.items()):
        preferred = PREFERRED_EXTRACT.get(code)
        if preferred:
            matches = [
                feature
                for feature in candidates
                if feature["properties"].get("id") == preferred
            ]
            if not matches:
                raise ValueError(
                    f"Preferred extract {preferred!r} for {code} is missing "
                    "from the Geofabrik index."
                )
            chosen = matches[0]
        elif len(candidates) == 1:
            chosen = candidates[0]
        else:
            ids = [feature["properties"].get("id") for feature in candidates]
            raise ValueError(
                f"Ambiguous Geofabrik extracts for {code}: {ids}. "
                "Add an entry to PREFERRED_EXTRACT."
            )
        extracts[code] = chosen

    return extracts


def _natural_earth_rows(path: Path) -> dict[str, dict]:
    """Return ``iso2 -> largest Natural Earth feature`` for each code."""
    gdf = gpd.read_file(path)
    gdf = gdf.loc[gdf["ISO_A2_EH"].astype(str) != "-99"].to_crs("EPSG:6933")
    gdf["_area"] = gdf.geometry.area
    gdf = gdf.sort_values("_area", ascending = False).to_crs("EPSG:4326")

    rows: dict[str, dict] = {}
    for _, row in gdf.iterrows():
        code = str(row["ISO_A2_EH"])
        # Sorted by area, so the first row for a code is the main country
        # (France over Clipperton, Australia over its small dependencies, ...).
        if code in rows:
            continue
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        west, south, east, north = geometry.bounds
        # Centre the map on the largest landmass rather than letting a remote
        # dependency drag the point into the ocean (Norway -> Svalbard).
        parts = (
            list(geometry.geoms)
            if hasattr(geometry, "geoms")
            else [geometry]
        )
        main_part = max(parts, key = lambda part: part.area)
        point = main_part.representative_point()
        lon_span = max(east - west, 0.01)
        zoom = int(round(math.log2(360.0 / lon_span)))
        rows[code] = {
            "iso3": (
                str(row["ISO_A3_EH"])
                if str(row["ISO_A3_EH"]) != "-99"
                else str(row["ADM0_A3"])
            ),
            "name": str(row["NAME"]),
            "bbox_west": f"{west:.6f}",
            "bbox_south": f"{south:.6f}",
            "bbox_east": f"{east:.6f}",
            "bbox_north": f"{north:.6f}",
            "center_lon": f"{point.x:.6f}",
            "center_lat": f"{point.y:.6f}",
            "initial_zoom": str(min(max(zoom, 3), 8)),
        }
    return rows


def build_rows(features: list[dict], natural_earth: dict[str, dict]) -> list[dict]:
    """Join Geofabrik download URLs with Natural Earth display/extent data."""
    extracts = _geofabrik_extracts(features)
    rows: list[dict] = []
    for code, feature in sorted(extracts.items()):
        properties = feature["properties"]
        urls = properties.get("urls") or {}
        missing = [key for key in ("pbf", "history", "updates") if not urls.get(key)]
        if missing:
            raise ValueError(
                f"Geofabrik extract {properties.get('id')!r} ({code}) is "
                f"missing URLs: {missing}"
            )
        earth = natural_earth.get(code, {})
        rows.append({
            "iso2": code,
            "iso3": earth.get("iso3") or "",
            "name": earth.get("name") or properties.get("name") or code,
            "geofabrik_id": properties["id"],
            "pbf_url": urls["pbf"],
            "history_url": urls["history"],
            "updates_url": urls["updates"],
            "bbox_west": earth.get("bbox_west", ""),
            "bbox_south": earth.get("bbox_south", ""),
            "bbox_east": earth.get("bbox_east", ""),
            "bbox_north": earth.get("bbox_north", ""),
            "center_lon": earth.get("center_lon", ""),
            "center_lat": earth.get("center_lat", ""),
            "initial_zoom": earth.get("initial_zoom", "4"),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description = __doc__)
    parser.add_argument("--geofabrik-index", type = Path, default = None)
    parser.add_argument("--natural-earth", type = Path, default = None)
    parser.add_argument("--output", type = Path, default = DEFAULT_OUTPUT)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix = "openpois-country-index-") as tmp:
        geofabrik_path = args.geofabrik_index
        if geofabrik_path is None:
            geofabrik_path = _download(
                GEOFABRIK_INDEX_URL, Path(tmp) / "geofabrik-index.json"
            )
        natural_earth_path = args.natural_earth
        if natural_earth_path is None:
            natural_earth_path = _download(
                NATURAL_EARTH_URL,
                Path(tmp) / "ne_10m_admin_0_countries.geojson",
            )

        features = json.loads(
            geofabrik_path.read_text(encoding = "utf-8")
        )["features"]
        natural_earth = _natural_earth_rows(natural_earth_path)
        rows = build_rows(features, natural_earth)

    args.output.parent.mkdir(parents = True, exist_ok = True)
    with open(args.output, "w", encoding = "utf-8", newline = "") as handle:
        writer = csv.DictWriter(handle, fieldnames = FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    without_boundary = [
        row["iso2"] for row in rows if not row["bbox_west"]
    ]
    print(f"Wrote {len(rows)} countries to {args.output}")
    if without_boundary:
        print(
            "No Natural Earth extent for: "
            + ", ".join(without_boundary)
            + " (Geofabrik still provides the OSM extract; a custom boundary "
            "filter may be needed)."
        )


if __name__ == "__main__":
    main()
