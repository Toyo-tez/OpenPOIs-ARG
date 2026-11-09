"""Country-profile resolution and derived-path tests for openpois.config."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from openpois.config import (
    country_code,
    extract_file_keys,
    find_config_path,
    indicators_enabled,
    load_config,
    os_extracts,
)

BASE_CONFIG = {
    "country": "US",
    "versions": {
        "conflation": "20260101",
        "osm_data": "20260101",
        "snapshot_osm": "20260101",
    },
    "paths": {"data_root": "~/data/openpois"},
    "download": {
        "osm": {
            "pbf_url": "https://example.com/us-latest.osm.pbf",
            "history_pbf_url": "https://example.com/us-internal.osh.pbf",
        }
    },
    "directories": {
        "osm_data": {
            "versioned": True,
            "path": "~/data/openpois/osm_data",
            "files": {"osm_versions": "osm_versions.parquet"},
        },
        "snapshot_osm": {
            "versioned": True,
            "path": "~/data/openpois/snapshots/osm",
            "files": {"raw_pbf": "us-latest.osm.pbf"},
        },
        "conflation": {
            "versioned": True,
            "path": "~/data/openpois/conflation",
            "files": {},
        },
    },
    "publish": {
        "bucket": "acct",
        "repo_prefix": "openpois",
        "public_base_url": "https://data.example/${SOURCE_ACCOUNT:-acct}/openpois",
    },
    "conflation": {"calibration": {"enabled": True}},
}


def _write_config(tmp_path: Path, base: dict, profiles: dict[str, dict]) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(base), encoding = "utf-8")
    profile_dir = tmp_path / "countries"
    profile_dir.mkdir()
    for code, profile in profiles.items():
        (profile_dir / f"{code}.yaml").write_text(
            yaml.safe_dump(profile), encoding = "utf-8"
        )
    return config_path


def test_iso_code_selects_country_profile(tmp_path, monkeypatch):
    config_path = _write_config(
        tmp_path,
        BASE_CONFIG,
        {
            "AR": {
                "country": {"iso2": "AR", "name": "Argentina"},
                "paths": {"data_root": "~/data/openpois/AR"},
                "publish": {"repo_prefix": "openpois-AR"},
            }
        },
    )

    config = load_config(config_path, country = "ar")

    assert country_code(config) == "AR"
    assert config.get("country", "name") == "Argentina"
    assert config.get("publish", "repo_prefix") == "openpois-AR"
    assert config.config["country_profile_path"].endswith("countries/AR.yaml")


def test_env_var_selects_country(tmp_path, monkeypatch):
    config_path = _write_config(
        tmp_path,
        BASE_CONFIG,
        {"AR": {"country": {"iso2": "AR", "name": "Argentina"}}},
    )
    monkeypatch.setenv("OPENPOIS_COUNTRY", "AR")

    assert country_code(load_config(config_path)) == "AR"


def test_deep_merge_keeps_base_keys(tmp_path):
    config_path = _write_config(
        tmp_path,
        BASE_CONFIG,
        {"AR": {"country": {"iso2": "AR"}, "conflation": {"test_bbox": {"xmin": -1}}}},
    )

    config = load_config(config_path, country = "AR")

    # Profile overrides one nested value ...
    assert config.get("conflation", "test_bbox", "xmin") == -1
    # ... while untouched siblings survive.
    assert config.get("conflation", "calibration", "enabled") is True


def test_data_root_rebases_directories(tmp_path):
    config_path = _write_config(
        tmp_path,
        BASE_CONFIG,
        {
            "AR": {
                "country": {"iso2": "AR"},
                "paths": {"data_root": "~/data/openpois/AR"},
            }
        },
    )

    config = load_config(config_path, country = "AR")
    expected_root = Path("~/data/openpois/AR").expanduser()

    assert config.get_dir_path("conflation").parent == expected_root / "conflation"
    assert config.get_dir_path("osm_data").parent == expected_root / "osm_data"


def test_data_root_rebases_from_custom_base_root(tmp_path):
    base = {
        **BASE_CONFIG,
        "paths": {"data_root": "/srv/openpois"},
        "directories": {
            **BASE_CONFIG["directories"],
            "osm_data": {
                "versioned": True,
                "path": "/srv/openpois/osm_data",
                "files": {},
            },
        },
    }
    config_path = _write_config(
        tmp_path,
        base,
        {"AR": {"country": {"iso2": "AR"}, "paths": {"data_root": "/srv/openpois/AR"}}},
    )

    config = load_config(config_path, country = "AR")

    assert config.get_dir_path("osm_data").parent == Path("/srv/openpois/AR/osm_data")


def test_explicit_extracts_derive_file_stubs(tmp_path):
    base = {
        **BASE_CONFIG,
        "country": None,
        "download": {
            "osm": {
                "extracts": [
                    {
                        "name": "argentina",
                        "snapshot_url": "https://example.com/argentina-latest.osm.pbf",
                        "history_url": "https://example.com/argentina-internal.osh.pbf",
                        "replication_url": "https://example.com/argentina-updates/",
                    }
                ]
            }
        },
    }
    config = load_config(_write_config(tmp_path, base, {}))

    extracts = os_extracts(config)
    assert [extract["name"] for extract in extracts] == ["argentina"]
    assert config.get_file_path("snapshot_osm", "raw_pbf").name == (
        "argentina-latest.osm.pbf"
    )
    assert config.get_file_path("osm_data", "raw_history_pbf").name == (
        "argentina-internal.osh.pbf"
    )


def test_legacy_extract_keys_do_not_override_existing_stubs(tmp_path):
    base = {**BASE_CONFIG, "country": None}
    config = load_config(_write_config(tmp_path, base, {}))

    extracts = os_extracts(config)
    assert [extract["name"] for extract in extracts] == ["us"]
    # Base file stub is preserved for legacy configs.
    assert config.get_file_path("snapshot_osm", "raw_pbf").name == "us-latest.osm.pbf"


def test_extract_file_keys_first_unsuffixed_then_prefixed():
    first = extract_file_keys("us", 0)
    second = extract_file_keys("pr", 1)

    assert first["raw_pbf"] == "raw_pbf"
    assert first["raw_history_pbf"] == "raw_history_pbf"
    assert second["raw_pbf"] == "raw_pr_pbf"
    assert second["raw_history_pbf"] == "raw_pr_history_pbf"


def test_indicators_default_true_and_profile_can_disable(tmp_path):
    config_path = _write_config(
        tmp_path,
        BASE_CONFIG,
        {
            "US": {"country": {"iso2": "US"}},
            "AR": {"country": {"iso2": "AR"}, "indicators": {"enabled": False}},
        },
    )

    assert indicators_enabled(load_config(config_path, country = "US")) is True
    assert indicators_enabled(load_config(config_path, country = "AR")) is False


def test_env_default_expansion(tmp_path, monkeypatch):
    monkeypatch.delenv("SOURCE_ACCOUNT", raising = False)
    config_path = _write_config(
        tmp_path,
        BASE_CONFIG,
        {"AR": {"country": {"iso2": "AR"}}},
    )

    config = load_config(config_path, country = "AR")
    assert config.get("publish", "public_base_url") == (
        "https://data.example/acct/openpois"
    )


def test_iso_only_auto_profile(tmp_path):
    """A bare ISO code derives downloads, paths and non-US defaults."""
    config_path = _write_config(tmp_path, BASE_CONFIG, {})

    config = load_config(config_path, country = "ar")

    assert country_code(config) == "AR"
    assert config.get("country", "name") == "Argentina"
    assert config.config["country_profile_source"] == "auto"

    expected_root = Path("~/data/openpois/AR").expanduser()
    assert config.get_dir_path("conflation").parent == expected_root / "conflation"

    extracts = os_extracts(config)
    assert [extract["name"] for extract in extracts] == ["argentina"]
    assert extracts[0]["snapshot_url"].endswith("argentina-latest.osm.pbf")
    assert extracts[0]["history_url"].endswith("argentina-internal.osh.pbf")

    boundary = config.get("download", "general", "boundary")
    assert boundary["filter_field"] == "ISO_A2_EH"
    assert boundary["filter_values"] == ["AR"]
    assert boundary["zip_name"] is None

    assert config.get("versions", "snapshot_osm") == "latest"
    assert config.get(
        "download", "overture", "release_date", fail_if_none = False
    ) is None
    assert indicators_enabled(config) is False
    assert config.get("conflation", "calibration", "enabled") is False
    assert config.get(
        "osm_turnover_model", "random_effects", "terms", "msa", "enabled"
    ) is False


def test_auto_profile_normalises_lowercase_code(tmp_path):
    config_path = _write_config(tmp_path, BASE_CONFIG, {})

    config = load_config(config_path, country = "gb")

    assert country_code(config) == "GB"
    assert config.get("country", "name") == "United Kingdom"
    assert os_extracts(config)[0]["snapshot_url"].endswith(
        "united-kingdom-latest.osm.pbf"
    )


def test_unknown_iso_code_raises(tmp_path):
    config_path = _write_config(tmp_path, BASE_CONFIG, {})

    with pytest.raises(FileNotFoundError):
        load_config(config_path, country = "ZZ")


def test_explicit_profile_suppresses_auto_defaults(tmp_path):
    """An existing profile keeps the base values it relies on."""
    config_path = _write_config(
        tmp_path,
        BASE_CONFIG,
        {"AR": {"country": {"iso2": "AR"}, "publish": {"repo_prefix": "openpois-AR"}}},
    )

    config = load_config(config_path, country = "AR")

    assert "country_profile_source" not in config.config
    # No auto extract list was injected; the base legacy URL remains active.
    assert [extract["name"] for extract in os_extracts(config)] == ["us"]
    assert config.get("publish", "repo_prefix") == "openpois-AR"


def test_find_config_path_prefers_explicit(tmp_path):
    config_path = _write_config(tmp_path, BASE_CONFIG, {})

    assert find_config_path(config_path) == config_path
