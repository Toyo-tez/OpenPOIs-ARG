#   -------------------------------------------------------------
#   Copyright (c) Henry Spatial Analysis. All rights reserved.
#   Licensed under the MIT License. See LICENSE in project root for information.
#   -------------------------------------------------------------

"""
Country boundary download, filtering, dissolve and bbox helpers.

The generic functions drive any country profile:

- download_boundary: Downloads (or locates) a zipped shapefile or a
    single-file vector boundary (GeoJSON, GeoPackage, …).
- load_boundary: Reads the vector file and optionally filters it to one
    country (e.g. Natural Earth ``ADM0_A3``).
- boundary_unary_polygon: Dissolves the filtered areas into a single
    (multi)polygon and applies an outward buffer to include near-coastal
    POIs. Internal borders disappear on dissolve.
- boundary_bboxes: Returns coarse bounding boxes covering the buffered
    polygon, split at longitude 0 so antimeridian-crossing countries get one
    bbox per side.
- get_boundary: End-to-end download → filter → dissolve → bbox.

The older ``*_us_pr_*`` functions below are thin US + territories wrappers
kept for backwards compatibility; they filter the Census cartographic state
shapefile to the 50 states + DC + 5 inhabited territories (Puerto Rico, US
Virgin Islands, Guam, Northern Mariana Islands, American Samoa).
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import geopandas as gpd
import requests
from shapely.geometry import MultiPolygon, Polygon

# EPSG:6933 = World Equal-Area Cylindrical. Preserves area globally with
# <1% distortion at latitudes relevant to the US + territories, so buffering by N
# metres in this CRS gives a true N-metre buffer everywhere we care about.
_EQUAL_AREA_CRS = "EPSG:6933"

# STUSPS codes for the 50 states + DC + 5 inhabited US territories:
# PR = Puerto Rico, VI = US Virgin Islands, GU = Guam,
# MP = Northern Mariana Islands, AS = American Samoa.
_US_STATE_CODES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY", "PR", "VI", "GU", "MP", "AS",
})


# -----------------------------------------------------------------------------
# Generic country boundary
# -----------------------------------------------------------------------------


def download_boundary(
    source_url: str,
    cache_dir: Path,
    cache_name: str | None = None,
    zip_name: str | None = None,
    shp_name: str | None = None,
    overwrite: bool = False,
    timeout: int = 120,
) -> Path:
    """Download (or locate) a country boundary file.

    Supports both zipped shapefiles and single-file formats (GeoJSON,
    GeoPackage, FlatGeobuf, …).  A local ``source_url`` path is used directly.

    Args:
        source_url: HTTP(S) URL or local path.
        cache_dir: Directory for the cached download.
        cache_name: Output filename for a non-zip source.  Defaults to the
            URL basename.
        zip_name: Output filename for a zip archive.  Defaults to the URL
            basename.
        shp_name: Expected ``.shp`` inside the zip.  If omitted, the first
            ``.shp`` in the archive is used.
        overwrite: Re-download even if the cached file exists.
        timeout: Per-request timeout in seconds.

    Returns:
        Path to the cached vector file (``.shp`` for zip sources).
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents = True, exist_ok = True)

    local = Path(source_url).expanduser()
    if local.exists():
        return local

    is_zip = (zip_name is not None) or str(source_url).lower().endswith(".zip")
    target_name = (zip_name if is_zip else cache_name) or Path(
        str(source_url).split("?")[0]
    ).name
    target_path = cache_dir / target_name

    if is_zip:
        expected_shp = cache_dir / shp_name if shp_name else None
        if expected_shp and expected_shp.exists() and not overwrite:
            return expected_shp
        if target_path.exists() and not overwrite and not shp_name:
            with zipfile.ZipFile(target_path) as zf:
                members = [m for m in zf.namelist() if m.lower().endswith(".shp")]
            if members:
                return cache_dir / members[0]
    elif target_path.exists() and not overwrite:
        return target_path

    print(f"Downloading boundary from {source_url}...")
    resp = requests.get(source_url, stream = True, timeout = timeout)
    resp.raise_for_status()
    with open(target_path, "wb") as handle:
        for chunk in resp.iter_content(chunk_size = 1024 * 1024):
            handle.write(chunk)

    if not is_zip:
        return target_path

    print(f"Unzipping {target_path} to {cache_dir}...")
    with zipfile.ZipFile(target_path) as zf:
        zf.extractall(cache_dir)
        members = [m for m in zf.namelist() if m.lower().endswith(".shp")]

    if shp_name:
        shp_path = cache_dir / shp_name
        if not shp_path.exists():
            raise FileNotFoundError(
                f"Expected shapefile {shp_path} not found after unzipping "
                f"{target_path}. Contents: {list(cache_dir.iterdir())}"
            )
        return shp_path
    if not members:
        raise FileNotFoundError(
            f"No .shp found in {target_path}. Contents: {list(cache_dir.iterdir())}"
        )
    return cache_dir / members[0]


def load_boundary(
    path: Path,
    filter_field: str | None = None,
    filter_values: list | tuple | set | None = None,
) -> gpd.GeoDataFrame:
    """Read a boundary vector file and optionally filter it to a country.

    Args:
        path: Vector file readable by GeoPandas (GeoJSON, shapefile, …).
        filter_field: Attribute column to filter on (e.g. ``ADM0_A3``).
        filter_values: Allowed values for ``filter_field``.

    Returns:
        GeoDataFrame in EPSG:4326.

    Raises:
        ValueError: If a filter is configured but matches no rows.
    """
    gdf = gpd.read_file(path)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")
    if filter_field and filter_values:
        allowed = {str(value) for value in filter_values}
        gdf = gdf.loc[gdf[filter_field].astype(str).isin(allowed)].copy()
        if gdf.empty:
            raise ValueError(
                f"No boundary rows matched {filter_field} in {sorted(allowed)} "
                f"in {path}; check the filter in the country profile."
            )
    return gdf.reset_index(drop = True)


def boundary_unary_polygon(
    boundary_gdf: gpd.GeoDataFrame,
    coastline_buffer_m: float = 100.0,
    equal_area_crs: str = _EQUAL_AREA_CRS,
) -> gpd.GeoDataFrame:
    """Dissolve a boundary layer into one (multi)polygon and buffer it outward."""
    dissolved = boundary_gdf.dissolve()[["geometry"]]
    if coastline_buffer_m > 0:
        dissolved = dissolved.to_crs(equal_area_crs)
        dissolved["geometry"] = dissolved.buffer(coastline_buffer_m)
        dissolved = dissolved.to_crs("EPSG:4326")
    return dissolved.reset_index(drop = True)


def _split_parts(geometry) -> list:
    if isinstance(geometry, (MultiPolygon, Polygon)) or hasattr(geometry, "geoms"):
        return list(geometry.geoms)
    return [geometry]


def boundary_bboxes(polygon_gdf: gpd.GeoDataFrame) -> list[dict]:
    """Return coarse bboxes covering a dissolved boundary polygon.

    Parts are split by centroid longitude at 0 so antimeridian-crossing
    countries (e.g. the US with its western Pacific territories) get one bbox
    per side instead of a useless (-180, 180) extent.
    """
    geom = polygon_gdf.geometry.iloc[0]
    neg_parts, pos_parts = [], []
    for part in _split_parts(geom):
        (neg_parts if part.centroid.x < 0 else pos_parts).append(part)

    bboxes: list[dict] = []
    for group in (neg_parts, pos_parts):
        if not group:
            continue
        bboxes.append({
            "xmin": min(p.bounds[0] for p in group),
            "ymin": min(p.bounds[1] for p in group),
            "xmax": max(p.bounds[2] for p in group),
            "ymax": max(p.bounds[3] for p in group),
        })
    return bboxes


def get_boundary(
    source_url: str,
    cache_dir: Path,
    coastline_buffer_m: float = 100.0,
    cache_name: str | None = None,
    zip_name: str | None = None,
    shp_name: str | None = None,
    filter_field: str | None = None,
    filter_values: list | tuple | set | None = None,
) -> tuple[gpd.GeoDataFrame, list[dict]]:
    """Download/load, filter, dissolve and bbox a country boundary."""
    path = download_boundary(
        source_url = source_url,
        cache_dir = cache_dir,
        cache_name = cache_name,
        zip_name = zip_name,
        shp_name = shp_name,
    )
    boundary_gdf = load_boundary(
        path,
        filter_field = filter_field,
        filter_values = filter_values,
    )
    polygon_gdf = boundary_unary_polygon(
        boundary_gdf,
        coastline_buffer_m = coastline_buffer_m,
    )
    return polygon_gdf, boundary_bboxes(polygon_gdf)


# -----------------------------------------------------------------------------
# Download
# -----------------------------------------------------------------------------


def download_us_pr_boundary(
    source_url: str,
    cache_dir: Path,
    zip_name: str = "cb_2023_us_state_5m.zip",
    shp_name: str = "cb_2023_us_state_5m.shp",
    overwrite: bool = False,
) -> Path:
    """
    Downloads the Census cartographic-boundary state shapefile and unzips it.

    The file is cached in ``cache_dir``. If the expected ``.shp`` already
    exists (and ``overwrite`` is False) this function is a no-op and simply
    returns the path.

    Args:
        source_url: URL of the Census cartographic boundary zip file.
        cache_dir: Directory where the zip file and unzipped shapefile
            components are stored.
        zip_name: Filename of the downloaded zip file within ``cache_dir``.
        shp_name: Filename of the target shapefile within ``cache_dir``.
        overwrite: If True, always re-download and re-unzip.

    Returns:
        Path to the unzipped ``.shp`` file in ``cache_dir``.

    Raises:
        requests.HTTPError: If the download fails.
    """
    return download_boundary(
        source_url = source_url,
        cache_dir = cache_dir,
        zip_name = zip_name,
        shp_name = shp_name,
        overwrite = overwrite,
    )


# -----------------------------------------------------------------------------
# Loading
# -----------------------------------------------------------------------------


def load_us_pr_boundary(
    shp_path: Path,
) -> gpd.GeoDataFrame:
    """
    Reads the state shapefile and returns the 50 states + DC + 5 inhabited
    US territories.

    Args:
        shp_path: Path to the unzipped Census state shapefile (``.shp``).

    Returns:
        GeoDataFrame in EPSG:4326 containing 56 rows (50 states + DC + PR
        + VI + GU + MP + AS), with the Census shapefile's columns preserved
        (``STUSPS``, ``NAME``, ``GEOID``, etc.) plus the ``geometry`` column.

    Raises:
        ValueError: If fewer than 56 rows match ``_US_STATE_CODES`` (suggests
            the source file is wrong or corrupted, e.g. the 1:20M variant
            that omits territories).
    """
    gdf = load_boundary(shp_path)
    gdf = gdf.loc[gdf["STUSPS"].isin(_US_STATE_CODES)].copy()
    if len(gdf) != len(_US_STATE_CODES):
        raise ValueError(
            f"Expected {len(_US_STATE_CODES)} rows after filtering to "
            f"US + DC + territories, got {len(gdf)}. Missing codes: "
            f"{sorted(_US_STATE_CODES - set(gdf['STUSPS']))}"
        )
    return gdf.reset_index(drop = True)


def us_pr_unary_polygon(
    shp_path: Path,
    coastline_buffer_m: float = 100.0,
) -> gpd.GeoDataFrame:
    """
    Returns a single-row GeoDataFrame containing the dissolved, buffered
    US + DC + territories polygon in EPSG:4326.

    Dissolving removes all internal borders, so the resulting polygon's
    exterior is either coastline/water or international land border (Canada,
    Mexico). A uniform outward buffer therefore effectively buffers only the
    coastline — the Canada/Mexico land border is a tiny fraction of the total
    exterior and captures essentially no POIs within 100 m.

    The buffer is applied in EPSG:6933 (World Equal-Area Cylindrical) to keep
    the distance metric accurate across the very different latitudes of
    CONUS, Alaska, Hawaii, the Caribbean territories, and the western
    Pacific territories.

    Args:
        shp_path: Path to the unzipped Census state shapefile.
        coastline_buffer_m: Outward buffer distance in metres. Set to 0 to
            disable the buffer.

    Returns:
        Single-row GeoDataFrame in EPSG:4326 with a single ``geometry`` column.
    """
    states = load_us_pr_boundary(shp_path = shp_path)
    return boundary_unary_polygon(
        states,
        coastline_buffer_m = coastline_buffer_m,
    )


# -----------------------------------------------------------------------------
# Bounding boxes (coarse pre-filters)
# -----------------------------------------------------------------------------


def us_pr_bboxes(
    shp_path: Path,
    coastline_buffer_m: float = 100.0,
) -> list[dict]:
    """
    Returns coarse bounding boxes covering the buffered US + territories
    polygon.

    Two bboxes are returned to handle the antimeridian. The negative-longitude
    bbox covers CONUS, AK mainland, HI, PR, USVI, and American Samoa
    (~-170 W). The positive-longitude bbox covers the western Aleutian "Near
    Islands" (~+172 E), Guam (~+144 E), and the Northern Mariana Islands
    (~+145 E). ``gpd.total_bounds`` on a multipolygon that crosses the
    antimeridian reports ``(~-180, ..., ~180)`` which is useless as a coarse
    prefilter.

    This function separates the positive-longitude parts from the rest of
    the polygon using an x-split at longitude 0 (via per-part centroid) and
    computes a bbox for each side. Callers that can only apply bbox
    predicates (e.g., DuckDB predicate pushdown on Overture's ``bbox``
    struct) should OR the returned bboxes together.

    Args:
        shp_path: Path to the unzipped Census state shapefile.
        coastline_buffer_m: Outward buffer distance applied before computing
            the bboxes. Matches the buffer used by
            :func:`us_pr_unary_polygon`.

    Returns:
        List of one or two dicts, each with keys ``xmin, ymin, xmax, ymax``
        in WGS-84 degrees.
    """
    polygon_gdf = us_pr_unary_polygon(
        shp_path = shp_path,
        coastline_buffer_m = coastline_buffer_m,
    )
    return boundary_bboxes(polygon_gdf)


# -----------------------------------------------------------------------------
# Convenience orchestrator
# -----------------------------------------------------------------------------


def get_us_pr_boundary(
    source_url: str,
    cache_dir: Path,
    coastline_buffer_m: float = 100.0,
    zip_name: str = "cb_2023_us_state_5m.zip",
    shp_name: str = "cb_2023_us_state_5m.shp",
) -> tuple[gpd.GeoDataFrame, list[dict]]:
    """
    Convenience wrapper: downloads the boundary file if needed and returns
    both the buffered polygon and the coarse bboxes.

    Args:
        source_url: URL of the Census cartographic boundary zip file.
        cache_dir: Directory for the cached shapefile.
        coastline_buffer_m: Outward buffer distance in metres.
        zip_name: Filename of the downloaded zip within ``cache_dir``.
        shp_name: Filename of the target shapefile within ``cache_dir``.

    Returns:
        Tuple of (single-row buffered polygon GeoDataFrame in EPSG:4326,
        list of coarse bbox dicts).
    """
    return get_boundary(
        source_url = source_url,
        cache_dir = cache_dir,
        coastline_buffer_m = coastline_buffer_m,
        zip_name = zip_name,
        shp_name = shp_name,
        filter_field = "STUSPS",
        filter_values = sorted(_US_STATE_CODES),
    )
