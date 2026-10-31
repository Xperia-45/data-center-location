"""
Dataset provenance & availability registry (Stage 3A).

FIXTURE-ONLY: no HTTP, no upstream contact.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.services.datasets import DATASETS, dataset_registry, registry_payload

c = TestClient(app)

REQUIRED_IDS = {
    "copernicus_dem_glo90",
    "openstreetmap_overpass",
    "esa_worldcover_2021_v200",
    "terralex_scenario_multipliers",
    "user_project_inputs",
    "hazard_layers_wri_imd_cwc",
}


def test_data_sources_legacy_contract_is_preserved():
    """The Stage 1/2 endpoint keeps its exact keys - the frontend renders them verbatim."""
    sources = c.get("/api/v1/data-sources").json()
    assert isinstance(sources, list) and sources
    for entry in sources:
        assert set(entry) == {"provider", "dataset", "url", "license", "quality", "status"}
    # Pinned by the pre-existing Stage 2 test as well; kept explicit here as a contract test.
    assert any("Copernicus DEM" in s["dataset"] and "Open-Meteo" in s["provider"] for s in sources)
    assert any("WorldCover" in s["dataset"] for s in sources)


def test_datasets_registry_endpoint_reports_every_dataset():
    body = c.get("/api/v1/datasets").json()
    assert body["schema_version"]
    ids = {d["id"] for d in body["datasets"]}
    assert REQUIRED_IDS <= ids
    assert set(body["data_class_definitions"]) == {
        "measured", "observation_derived", "mapped", "estimated", "modelled",
        "user_input", "unavailable",
    }
    assert body["not_modelled_effects"]
    assert all(e["status"] == "NOT MODELLED" for e in body["not_modelled_effects"])


def test_copernicus_dem_is_labelled_as_a_surface_model_not_bare_earth():
    dem = next(d for d in DATASETS if d["id"] == "copernicus_dem_glo90")
    assert dem["data_class"] == "observation_derived"
    assert dem["native_resolution_m"] == 90
    assert "Digital Surface Model" in dem["surface_model"]
    assert "NOT bare-earth" in dem["surface_model"]
    rules = registry_payload()["integrity_rules"]
    assert any("does not increase its native resolution" in rule for rule in rules)
    assert any("NOT MODELLED" in rule for rule in rules)
    assert dem["citation_doi"] == "10.5270/ESA-c5d3d65"


def test_worldcover_provenance_matches_the_published_product():
    wc = next(d for d in DATASETS if d["id"] == "esa_worldcover_2021_v200")
    assert wc["data_class"] == "mapped"
    assert wc["native_resolution_m"] == 10
    assert "76.7" in wc["accuracy"]
    assert wc["citation_doi"] == "10.5281/zenodo.7254221"
    assert "ESA WorldCover project 2021" in wc["attribution"]
    assert "Copernicus Sentinel data (2021)" in wc["attribution"]
    assert wc["license"] == "CC BY 4.0"


def test_unintegrated_hazard_layers_are_never_reported_as_available():
    hazard = next(d for d in DATASETS if d["id"] == "hazard_layers_wri_imd_cwc")
    assert hazard["status"] == "not_integrated"
    assert hazard["data_class"] == "unavailable"
    runtime = dataset_registry({"hazard_layers_wri_imd_cwc": {"available": False, "status": "not_integrated",
                                                             "detail": "probe", "observed_at": "now"}})
    entry = next(d for d in runtime if d["id"] == "hazard_layers_wri_imd_cwc")
    assert entry["availability"]["available"] is False


def test_registry_records_observed_degradation_per_request():
    runtime = dataset_registry({
        "openstreetmap_overpass": {
            "available": False, "status": "degraded",
            "detail": "Overpass returned HTTP 504.", "observed_at": "2026-10-31T00:00:00Z",
        }
    })
    entry = next(d for d in runtime if d["id"] == "openstreetmap_overpass")
    assert entry["availability"]["status"] == "degraded"
    assert entry["availability"]["available"] is False
    assert "504" in entry["availability"]["detail"]
