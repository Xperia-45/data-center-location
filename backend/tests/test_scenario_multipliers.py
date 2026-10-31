"""
Scenario multipliers: configurable, bounds-validated, transparently labelled modelled values.

FIXTURE-ONLY: no HTTP, no upstream contact.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import scenario_multipliers as sm
from app.services.scenarios import PRESETS

c = TestClient(app)


def test_registry_is_explicitly_uncalibrated_and_lists_unsupported_effects():
    body = c.get("/api/v1/scenario-multipliers").json()
    assert body["data_class"] == "modelled"
    assert body["calibrated"] is False
    assert "NOT calibrated" in body["label"]
    assert body["not_modelled"]
    forbidden = {e["id"] for e in body["not_modelled"]}
    assert {"groundwater_drawdown", "flood_risk_change", "biodiversity_loss_magnitude",
            "noise_propagation", "ghg_emissions"} <= forbidden
    for key, spec in body["presets"]["baseline"]["multipliers"].items():
        assert spec["calibrated"] is False
        assert spec["basis"]
        assert len(spec["bounds"]) == 2


def test_multiplier_config_agrees_with_the_stage2_presets():
    """
    The multiplier registry is the single source of truth; the Stage 2 PRESETS must not
    drift away from it, otherwise the UI and the impact layer would disagree.
    """
    cfg = sm.load_config()
    base = cfg["presets"]["baseline"]["multipliers"]
    for preset, spec in PRESETS.items():
        resolved = cfg["presets"][preset]["multipliers"]
        assert resolved["water_intensity_factor"] == pytest.approx(spec["water_mld"] / PRESETS["baseline"]["water_mld"])
        assert resolved["energy_intensity_factor"] == pytest.approx(spec["power_mw"] / PRESETS["baseline"]["power_mw"])
    # The frontend Timescape already scales the campus by these factors (timescapeMath.ts).
    assert cfg["presets"]["high-growth"]["multipliers"]["land_take_factor"] == 1.35
    assert cfg["presets"]["mitigation"]["multipliers"]["land_take_factor"] == 0.9
    assert base["land_take_factor"] == 1.0


def test_out_of_bounds_overrides_are_rejected(monkeypatch):
    monkeypatch.setenv("SCENARIO_MULTIPLIER_OVERRIDES", '{"high-growth": {"land_take_factor": 99}}')
    with pytest.raises(sm.MultiplierConfigError):
        sm.multipliers_for("high-growth")
    resp = c.get("/api/v1/scenario-multipliers")
    assert resp.status_code == 400


def test_unknown_multiplier_names_are_rejected():
    with pytest.raises(sm.MultiplierConfigError):
        sm.validate_overrides({"baseline": {"made_up_factor": 1.0}})
    with pytest.raises(sm.MultiplierConfigError):
        sm.validate_overrides({"no-such-preset": {"land_take_factor": 1.0}})


def test_valid_overrides_are_applied_and_flagged(monkeypatch):
    monkeypatch.setenv("SCENARIO_MULTIPLIER_OVERRIDES", '{"mitigation": {"vegetation_retention_factor": 0.6}}')
    values = sm.multipliers_for("mitigation")
    assert values["vegetation_retention_factor"] == 0.6
    body = c.get("/api/v1/scenario-multipliers").json()
    assert body["presets"]["mitigation"]["multipliers"]["vegetation_retention_factor"]["value"] == 0.6
    assert body["presets"]["mitigation"]["multipliers"]["vegetation_retention_factor"]["overridden"] is True
    assert body["overrides_applied"] == {"mitigation": {"vegetation_retention_factor": 0.6}}


def test_impact_layer_refuses_to_estimate_vegetation_without_land_cover():
    """A missing WorldCover layer must produce UNAVAILABLE, never a number."""
    impact = sm.build_impact_layer(
        base_fingerprint="sha256:test",
        base_content_digest="sha256:test",
        base_datasets=["terrain"],
        project={"footprint_ha": 20.0, "water_mld": 2.0, "power_mw": 40.0},
        preset="high-growth",
        land_cover={"status": "unavailable", "reason": "rasterio_unavailable", "detail": "not installed"},
        parcel=None,
    )
    veg = next(i for i in impact["indications"] if i["id"] == "indicative_vegetation_cleared_ha")
    assert veg["status"] == "unavailable"
    assert veg["value"] is None
    assert veg["data_class"] == "unavailable"
    assert "rasterio_unavailable" in veg["reason"]
    assert impact["data_class"] == "modelled"
    assert impact["not_modelled"]


def test_impact_layer_arithmetic_is_shown_with_its_derivation():
    impact = sm.build_impact_layer(
        base_fingerprint="sha256:test",
        base_content_digest="sha256:test",
        base_datasets=["terrain", "land_cover"],
        project={"footprint_ha": 20.0, "water_mld": 2.0, "power_mw": 40.0},
        preset="high-growth",
        land_cover={"status": "ok", "classes": [{"value": 10, "label": "Tree cover", "area_ha": 100.0}]},
        parcel=None,
    )
    by_id = {i["id"]: i for i in impact["indications"]}
    assert by_id["indicative_land_take_ha"]["value"] == pytest.approx(20.0 * 1.35)
    assert by_id["indicative_water_demand_mld"]["value"] == pytest.approx(2.0 * 3.5)
    assert by_id["indicative_power_mw"]["value"] == pytest.approx(40.0 * 3.0)
    # high-growth retains no vegetation -> the full mapped tree-cover area is indicated
    assert by_id["indicative_vegetation_cleared_ha"]["value"] == pytest.approx(100.0)
    assert "x land_take_factor 1.35" in by_id["indicative_land_take_ha"]["derivation"]
    assert all(i["data_class"] in ("estimated", "unavailable") for i in impact["indications"])
    assert impact["project_inputs"]["data_class"] == "user_input"
