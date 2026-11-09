#   -------------------------------------------------------------
#   Copyright (c) Henry Spatial Analysis. All rights reserved.
#   Licensed under the MIT License. See LICENSE in project root for information.
#   -------------------------------------------------------------

"""Country-aware configuration loading.

The pipeline is driven by ``config.yaml`` at the repository root.  Country
specific values (boundary source, Geofabrik extracts, data directory namespace,
map centre, …) live in ``countries/<slug>.yaml`` and are deep-merged over the
base config at load time.

Resolution order
----------------

1. ``OPENPOIS_CONFIG`` environment variable, or an explicit ``path`` argument.
2. ``config.yaml`` found by walking up from the current working directory (and
   from the installed package location).
3. ``~/repos/openpois/config.yaml`` as a last-resort backwards-compatible
   fallback.

The country is selected with, in order:

1. the ``country`` argument;
2. the ``OPENPOIS_COUNTRY`` environment variable;
3. the top-level ``country`` key in ``config.yaml``.

Country profiles are read from ``countries/<country>.yaml`` beside the config
file.  A relative/absolute path in ``country`` (or ``country_profile``) is also
accepted.  Profiles are optional: when the selected value is an ISO 3166-1
alpha-2 code and no ``countries/<CODE>.yaml`` exists, a complete profile is
synthesized from the bundled ``data/countries.csv`` index (Geofabrik extract,
full-history and replication URLs) plus Natural Earth (boundary filter, name
and extent).  Selecting a new country therefore only requires the ISO code.

Paths
-----

Every ``directories.*.path`` under the base config's data root (by default
``~/data/openpois``) is rebased onto ``paths.data_root`` from the merged
config.  That keeps one checkout usable for multiple countries without
duplicating dozens of paths::

    paths:
      data_root: ~/data/openpois/argentina

Environment variables (``${VAR}``) and ``~`` are expanded in directory paths,
cookie files and credentials paths.
"""

from __future__ import annotations

import copy
import csv
import os
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import yaml
from config_versioned import Config

CONFIG_ENV_VAR = "OPENPOIS_CONFIG"
COUNTRY_ENV_VAR = "OPENPOIS_COUNTRY"
DEFAULT_CONFIG_NAME = "config.yaml"
DEFAULT_DATA_ROOT = "~/data/openpois"
_CONFIG_SEARCH_DEPTH = 8
_COUNTRY_INDEX_PATH = Path(__file__).resolve().parent / "data" / "countries.csv"

# Natural Earth 1:10m admin-0 is public domain and carries an ISO alpha-2 field
# (``ISO_A2_EH``) that fixes the ``-99`` placeholders on France, Norway and
# Kosovo.  One download serves every auto-generated country profile.
_NATURAL_EARTH_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_10m_admin_0_countries.geojson"
)
_NATURAL_EARTH_CACHE_NAME = "ne_10m_admin_0_countries.geojson"

# ``${VAR}`` or ``${VAR:-default}``.  The default form is handy in country
# profiles for Source Cooperative account names that differ per maintainer.
_ENV_PATTERN = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}"
)


def _expand_env(value: str) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` and ``$VAR`` in a string."""
    def replace(match: re.Match) -> str:
        name = match.group("name")
        if name in os.environ:
            return os.environ[name]
        default = match.group("default")
        return default if default is not None else match.group(0)

    expanded = _ENV_PATTERN.sub(replace, value)
    return os.path.expandvars(expanded)


def _expand_env_strings(value):
    """Recursively expand environment placeholders in a config tree."""
    if isinstance(value, str):
        return _expand_env(value)
    if isinstance(value, list):
        return [_expand_env_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env_strings(item) for key, item in value.items()}
    return value


# -----------------------------------------------------------------------------
# Locating the config file
# -----------------------------------------------------------------------------


def _candidate_starts() -> list[Path]:
    """Directories to start the upward search from, most specific first."""
    starts = [Path.cwd()]
    # src/openpois/config.py -> repository root (editable installs and source
    # checkouts).  ``parents[2]`` is the directory containing src/.
    here = Path(__file__).resolve()
    if len(here.parents) >= 3:
        starts.append(here.parents[2])
    return starts


def find_config_path(explicit: str | Path | None = None) -> Path:
    """Return the path to the active ``config.yaml``.

    Args:
        explicit: Optional config path (from a CLI flag or caller).  Takes
            precedence over ``OPENPOIS_CONFIG``.

    Raises:
        FileNotFoundError: If no config file can be found.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        return path

    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path:
        path = Path(env_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(
                f"{CONFIG_ENV_VAR} points at a missing config file: {path}"
            )
        return path

    for start in _candidate_starts():
        for parent in [start, *list(start.parents)[:_CONFIG_SEARCH_DEPTH]]:
            candidate = parent / DEFAULT_CONFIG_NAME
            # Prefer a repository root: one that also has a countries/ folder
            # or a src/openpois package.  This avoids picking up an unrelated
            # config.yaml in a parent directory.
            if candidate.is_file() and (
                (parent / "countries").is_dir()
                or (parent / "src" / "openpois").is_dir()
            ):
                return candidate

    fallback = Path("~/repos/openpois/config.yaml").expanduser()
    if fallback.is_file():
        return fallback

    raise FileNotFoundError(
        f"Could not find {DEFAULT_CONFIG_NAME}. Run from the repository root, "
        f"pass a path, or set {CONFIG_ENV_VAR}."
    )


# -----------------------------------------------------------------------------
# Merging / path helpers
# -----------------------------------------------------------------------------


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``.

    Dictionaries merge key-by-key; every other value (including lists) is
    replaced wholesale.
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _country_code(value) -> str | None:
    """Normalise the ``country`` config value to an ISO 3166-1 alpha-2 code.

    Two-letter codes are upper-cased. Longer values are accepted as legacy
    profile names and returned unchanged (lower-case).
    """
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("iso2") or value.get("code") or value.get("slug")
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return text.lower()


def _find_existing_profile(config_path: Path, code: str) -> Path | None:
    """Return ``countries/<CODE>.yaml`` if it exists, without guessing."""
    profile_dir = config_path.parent / "countries"
    for candidate in (
        profile_dir / f"{code}.yaml",
        profile_dir / f"{code.upper()}.yaml",
        profile_dir / f"{code.lower()}.yaml",
    ):
        if candidate.is_file():
            return candidate
    return None


def _resolve_profile_path(
    config_path: Path,
    country_value,
    country_profile: str | None,
) -> tuple[Path | None, str | None, bool]:
    """Resolve the country profile YAML and the ISO code for auto defaults.

    Returns ``(profile_path, auto_code, explicit)``.  ``explicit`` is true when
    the caller named a profile path directly; a missing explicit profile is an
    error.  For a bare ISO alpha-2 code, ``profile_path`` is the existing
    ``countries/<CODE>.yaml`` (or ``None``) and ``auto_code`` is set so the
    loader can synthesize one when the file is absent.
    """
    if country_profile:
        profile = Path(country_profile).expanduser()
        if not profile.is_absolute():
            profile = config_path.parent / profile
        return profile, None, True
    if country_value is None:
        return None, None, False
    if isinstance(country_value, str) and (
        country_value.endswith((".yaml", ".yml")) or "/" in country_value
    ):
        profile = Path(country_value).expanduser()
        if not profile.is_absolute():
            profile = config_path.parent / profile
        return profile, None, True
    code = _country_code(country_value)
    if not code:
        return None, None, False
    if len(code) == 2 and code.isalpha():
        return _find_existing_profile(config_path, code), code, False
    # Last resort: keep the old full-name convention.
    return config_path.parent / "countries" / f"{code}.yaml", None, False


# -----------------------------------------------------------------------------
# Automatic country profiles (ISO-only operation)
# -----------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_country_index() -> dict[str, dict]:
    """Read the bundled ISO2 -> Geofabrik / Natural Earth index."""
    if not _COUNTRY_INDEX_PATH.is_file():
        return {}
    rows: dict[str, dict] = {}
    with open(_COUNTRY_INDEX_PATH, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("iso2") or "").strip().upper()
            if code:
                rows[code] = row
    return rows


def _index_float(row: dict, key: str) -> float | None:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Versioned subtrees that have no meaningful default for a brand-new country.
# ``latest`` is a valid folder name and keeps repeated downloads in one place
# until the user pins a dated release.
_AUTO_VERSION_KEYS = (
    "osm_data",
    "model_output",
    "snapshot_osm",
    "snapshot_overture",
    "conflation",
    "calibration",
    "source_coop",
    "ghost_osm",
    "manual_overrides",
    "id_registry",
)


def _auto_country_profile(code: str) -> dict | None:
    """Synthesize a complete country profile from the bundled index.

    Only the ISO alpha-2 code is required: everything else (Geofabrik snapshot,
    history and replication URLs, the Natural Earth boundary filter, the data
    directory and non-US model defaults) is derived here.
    """
    row = _load_country_index().get(code.upper())
    if row is None:
        return None

    name = row.get("name") or code.upper()
    iso3 = (row.get("iso3") or "").strip()
    extract_name = row.get("geofabrik_id") or code.lower()
    pbf_url = row.get("pbf_url")
    history_url = row.get("history_url")
    updates_url = row.get("updates_url")
    if not (pbf_url and history_url and updates_url):
        return None

    west = _index_float(row, "bbox_west")
    south = _index_float(row, "bbox_south")
    east = _index_float(row, "bbox_east")
    north = _index_float(row, "bbox_north")
    center_lon = _index_float(row, "center_lon")
    center_lat = _index_float(row, "center_lat")
    try:
        zoom = int(row.get("initial_zoom") or 4)
    except (TypeError, ValueError):
        zoom = 4

    country: dict = {"iso2": code.upper(), "name": name}
    if iso3:
        country["iso3"] = iso3

    profile: dict = {
        "country": country,
        "paths": {"data_root": f"~/data/openpois/{code.upper()}"},
        "versions": {key: "latest" for key in _AUTO_VERSION_KEYS},
        "indicators": {"enabled": False},
        "download": {
            "general": {
                "boundary": {
                    "source_url": _NATURAL_EARTH_URL,
                    "cache_name": _NATURAL_EARTH_CACHE_NAME,
                    "coastline_buffer_m": 100,
                    # Clear the US zip-shapefile names inherited from the base.
                    "zip_name": None,
                    "shp_name": None,
                    "filter_field": "ISO_A2_EH",
                    "filter_values": [code.upper()],
                },
            },
            "osm": {
                "start_date": "2016-01-01",
                "end_date": date.today().isoformat(),
                "extracts": [{
                    "name": extract_name,
                    "snapshot_url": pbf_url,
                    "history_url": history_url,
                    "replication_url": updates_url,
                }],
                # Legacy single-extract keys kept in sync for older scripts.
                "pbf_url": pbf_url,
                "history_pbf_url": history_url,
                "pr_pbf_url": None,
                "usvi_pbf_url": None,
                "american_oceania_pbf_url": None,
                "pr_history_pbf_url": None,
                "usvi_history_pbf_url": None,
                "american_oceania_history_pbf_url": None,
                # A new country starts with a full-history build.
                "history_mode": "full",
                "incremental_history": {
                    "base_version": None,
                    "replication_urls": {
                        "us": None,
                        "pr": None,
                        "usvi": None,
                        "american_oceania": None,
                        extract_name: updates_url,
                    },
                },
            },
            "overture": {
                # No country profile has pinned a release: use the latest
                # public Overture Places release at download time.
                "release_date": None,
            },
        },
        "osm_data": {"apply_model": {"model_stub": "latest"}},
        "osm_turnover_model": {
            "random_effects": {
                "terms": {
                    "msa": {"enabled": False},
                    "amenity_msa": {"enabled": False},
                    "urbanicity": {"enabled": False},
                },
                "delta_terms": {"msa": {"enabled": False}},
            },
        },
        "conflation": {"calibration": {"enabled": False}},
        "site": {
            "title": f"OpenPOIs {name}",
            "locale": "en-US",
            "timezone": "UTC",
            "initial_zoom": zoom,
            "geocoder_country": code.upper(),
        },
        "publish": {
            "bucket": "${OPENPOIS_SOURCE_COOP_ACCOUNT:-henryspatialanalysis}",
            "repo_prefix": f"openpois-{code.upper()}",
            "public_base_url": (
                "https://data.source.coop/"
                "${OPENPOIS_SOURCE_COOP_ACCOUNT:-henryspatialanalysis}/"
                f"openpois-{code.upper()}"
            ),
            "version_metadata": {
                "osm_snapshot_date": None,
                "overture_release": None,
                "model_commit": None,
            },
        },
    }

    if None not in (west, south, east, north):
        # A near-global or antimeridian-crossing bbox is worse than no clamp.
        if west <= east and (east - west) <= 180:
            profile["site"]["bbox"] = [west, south, east, north]
    if center_lon is not None and center_lat is not None:
        profile["site"]["initial_center"] = [center_lon, center_lat]
        profile["conflation"]["test_bbox"] = {
            "xmin": round(center_lon - 0.10, 6),
            "ymin": round(center_lat - 0.075, 6),
            "xmax": round(center_lon + 0.10, 6),
            "ymax": round(center_lat + 0.075, 6),
        }

    return profile


def _expand_path(value):
    """Expand ``~`` and ``$VAR``/``${VAR}`` in a path-like config value."""
    if not isinstance(value, str):
        return value
    if not (value.startswith(("~", "$")) or "${" in value):
        return value
    return os.path.expanduser(_expand_env(value))


def _expand_paths_in_place(config: dict) -> None:
    """Expand environment variables in the path-valued config keys."""
    for dir_info in (config.get("directories") or {}).values():
        if isinstance(dir_info, dict) and dir_info.get("path"):
            dir_info["path"] = _expand_path(dir_info["path"])

    publish = config.get("publish") or {}
    if publish.get("credentials_file"):
        publish["credentials_file"] = _expand_path(publish["credentials_file"])

    osm = (config.get("download") or {}).get("osm") or {}
    if osm.get("history_cookie_file"):
        osm["history_cookie_file"] = _expand_path(osm["history_cookie_file"])

    overrides = (config.get("conflation") or {}).get("manual_overrides") or {}
    if overrides.get("path"):
        overrides["path"] = _expand_path(overrides["path"])


def _rebase_data_directories(config: dict, source_root: str | None = None) -> None:
    """Move data directories under the profile's ``paths.data_root``.

    ``source_root`` is the base config's data root (default
    ``~/data/openpois``); only paths beneath it are rebased, so repository
    fixtures (calibration data, test inputs) are left alone.
    """
    data_root = (
        (config.get("paths") or {}).get("data_root")
        or (config.get("country") or {}).get("data_root")
    )
    if not data_root:
        return
    root = Path(_expand_path(str(data_root)))
    default_root = Path(_expand_path(str(source_root or DEFAULT_DATA_ROOT)))

    for dir_info in (config.get("directories") or {}).values():
        if not isinstance(dir_info, dict) or not dir_info.get("path"):
            continue
        current = Path(_expand_path(str(dir_info["path"])))
        try:
            relative = current.relative_to(default_root)
        except ValueError:
            continue
        dir_info["path"] = str(root / relative)


# -----------------------------------------------------------------------------
# OSM extracts
# -----------------------------------------------------------------------------

_LEGACY_EXTRACT_KEYS = (
    ("us", "pbf_url", "history_pbf_url"),
    ("pr", "pr_pbf_url", "pr_history_pbf_url"),
    ("usvi", "usvi_pbf_url", "usvi_history_pbf_url"),
    ("american_oceania", "american_oceania_pbf_url",
     "american_oceania_history_pbf_url"),
)


def extract_file_keys(name: str, index: int) -> dict[str, str]:
    """Return the file-stub keys used for one OSM extract.

    The first extract keeps the historical unsuffixed names (``raw_pbf``,
    ``filtered_pbf`` …); later extracts are prefixed with their name
    (``raw_pr_pbf`` …).
    """
    suffix = "" if index == 0 else f"{name}_"
    return {
        "raw_pbf": f"raw_{suffix}pbf",
        "filtered_pbf": f"filtered_{suffix}pbf",
        "residential_filtered_pbf": f"residential_{suffix}filtered_pbf",
        "raw_history_pbf": f"raw_{suffix}history_pbf",
        "filtered_history_pbf": f"filtered_{suffix}history_pbf",
        "time_filtered_history_pbf": f"time_filtered_{suffix}history_pbf",
        "versions": f"{name}_versions",
        "changes": f"{name}_changes",
    }


def _legacy_extracts(osm_cfg: dict) -> list[dict]:
    """Build an extract list from the pre-0.3 ``*_pbf_url`` keys."""
    replication = (
        (osm_cfg.get("incremental_history") or {}).get("replication_urls") or {}
    )
    extracts = []
    for name, snapshot_key, history_key in _LEGACY_EXTRACT_KEYS:
        snapshot_url = osm_cfg.get(snapshot_key)
        history_url = osm_cfg.get(history_key)
        if not snapshot_url and not history_url:
            continue
        extracts.append({
            "name": name,
            "snapshot_url": snapshot_url,
            "history_url": history_url,
            "replication_url": replication.get(name),
        })
    return extracts


def _file_stub_from_url(url: str | None, default: str) -> str:
    if not url:
        return default
    name = Path(urlparse(str(url)).path).name
    return name or default


def _ensure_extract_files(config: dict) -> None:
    """Populate ``download.osm.extracts`` and the derived file stubs.

    An explicit ``download.osm.extracts`` list wins.  Otherwise the legacy
    per-territory URL keys are converted into the same list so older configs
    keep working.
    """
    osm_cfg = config.setdefault("download", {}).setdefault("osm", {})
    explicit = osm_cfg.get("extracts")
    extracts = explicit or _legacy_extracts(osm_cfg)

    directories = config.setdefault("directories", {})
    snapshot_files = directories.setdefault("snapshot_osm", {}).setdefault(
        "files", {}
    )
    osm_data_files = directories.setdefault("osm_data", {}).setdefault("files", {})

    for index, extract in enumerate(extracts):
        name = str(extract.get("name") or f"extract_{index}")
        keys = extract_file_keys(name, index)
        snapshot_url = extract.get("snapshot_url") or extract.get("pbf_url")
        history_url = extract.get("history_url")

        generated = {
            keys["raw_pbf"]: _file_stub_from_url(
                snapshot_url, f"{name}-latest.osm.pbf"
            ),
            keys["filtered_pbf"]: f"{name}-pois.osm.pbf",
            keys["residential_filtered_pbf"]: f"{name}-residential.osm.pbf",
        }
        for key, value in generated.items():
            # With an explicit list, overwrite stale US stubs.  With the
            # legacy list, only fill gaps so existing data directories keep
            # their historical filenames.
            if explicit or key not in snapshot_files:
                snapshot_files[key] = value

        history_generated = {
            keys["raw_history_pbf"]: _file_stub_from_url(
                history_url, f"{name}-internal.osh.pbf"
            ),
            keys["filtered_history_pbf"]: f"{name}-pois.osh.pbf",
            keys["time_filtered_history_pbf"]: f"{name}-pois-timefilt.osh.pbf",
            keys["versions"]: f"{name}_versions.parquet",
            keys["changes"]: f"{name}_changes.parquet",
        }
        for key, value in history_generated.items():
            if explicit or key not in osm_data_files:
                osm_data_files[key] = value

    osm_cfg["extracts"] = extracts


def os_extracts(config: Config | dict) -> list[dict]:
    """Return the normalised OSM extract list from a loaded config."""
    data = config.config if isinstance(config, Config) else config
    return (
        data.get("download", {}).get("osm", {}).get("extracts", [])
        or _legacy_extracts(data.get("download", {}).get("osm", {}))
    )


# -----------------------------------------------------------------------------
# Public loader
# -----------------------------------------------------------------------------


def load_config(
    path: str | Path | None = None,
    country: str | None = None,
) -> Config:
    """Load the base config, deep-merge its country profile, return a Config.

    Args:
        path: Explicit path to ``config.yaml``.  Defaults to
            ``OPENPOIS_CONFIG`` or the repository root.
        country: ISO 3166-1 alpha-2 code (e.g. ``AR``) or profile path.
            Defaults to ``OPENPOIS_COUNTRY`` or the config's top-level
            ``country`` key.

    Returns:
        A :class:`config_versioned.Config` wrapping the merged dictionary.
    """
    config_path = find_config_path(path)
    with open(config_path, encoding="utf-8") as handle:
        base = yaml.safe_load(handle) or {}
    if not isinstance(base, dict):
        raise TypeError(f"{config_path} must contain a YAML mapping")

    configured_country = base.get("country")
    selected_country = (
        country
        or os.environ.get(COUNTRY_ENV_VAR)
        or configured_country
    )
    profile_path, auto_code, explicit = _resolve_profile_path(
        config_path,
        selected_country,
        base.get("country_profile"),
    )
    # Directory paths in the base config are written relative to its data
    # root; a profile may move that root (e.g. ~/data/openpois/AR).
    base_data_root = (base.get("paths") or {}).get("data_root") or DEFAULT_DATA_ROOT

    merged = copy.deepcopy(base)
    if profile_path is not None:
        if not profile_path.is_file():
            raise FileNotFoundError(
                f"Country profile not found: {profile_path}. "
                f"Create it or change the 'country' key in {config_path}."
            )
    elif auto_code is not None:
        # No hand-written profile: derive a complete one from the ISO code.
        # Auto defaults intentionally apply only when the file is absent, so
        # an existing profile keeps the base config values it relies on.
        auto_profile = _auto_country_profile(auto_code)
        if auto_profile is None:
            known = ", ".join(sorted(_load_country_index())) or "none"
            expected = config_path.parent / "countries" / f"{auto_code}.yaml"
            raise FileNotFoundError(
                f"No country profile for {auto_code!r} and no built-in "
                f"Geofabrik extract. Add {expected} with "
                f"download.osm.extracts. Known ISO codes: {known}."
            )
        merged = _deep_merge(merged, auto_profile)
        merged["country_profile_source"] = "auto"

    if profile_path is not None:
        with open(profile_path, encoding="utf-8") as handle:
            profile = yaml.safe_load(handle) or {}
        if not isinstance(profile, dict):
            raise TypeError(f"{profile_path} must contain a YAML mapping")
        merged = _deep_merge(merged, profile)
        merged["country_profile_path"] = str(profile_path)

    # Record which profile was used and make sure the merged country block
    # always carries the ISO code.
    code = _country_code(selected_country)
    if isinstance(merged.get("country"), str):
        merged["country"] = {"iso2": _country_code(merged["country"])}
    elif isinstance(merged.get("country"), dict):
        merged["country"].setdefault("iso2", code)
    elif code:
        merged["country"] = {"iso2": code}

    _ensure_extract_files(merged)
    _rebase_data_directories(merged, base_data_root)
    _expand_paths_in_place(merged)
    merged = _expand_env_strings(merged)
    return Config(merged)


def country_code(config: Config | dict) -> str:
    """Return the active ISO 3166-1 alpha-2 code, defaulting to ``US``."""
    data = config.config if isinstance(config, Config) else config
    value = data.get("country")
    if isinstance(value, dict):
        value = value.get("iso2") or value.get("code")
    code = _country_code(value)
    return code.upper() if code and len(code) == 2 else (code or "US")


def country_slug(config: Config | dict) -> str:
    """Backwards-compatible alias for :func:`country_code`."""
    return country_code(config)


def indicators_enabled(config: Config | dict) -> bool:
    """Whether Census-style ``msa_code`` / ``urban_rural`` enrichment is on.

    Defaults to ``True`` for backwards compatibility.  Country profiles
    without US Census reference areas can set::

        indicators:
          enabled: false
    """
    data = config.config if isinstance(config, Config) else config
    block = data.get("indicators")
    if isinstance(block, dict) and "enabled" in block:
        return bool(block["enabled"])
    return True

