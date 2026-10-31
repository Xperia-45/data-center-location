"""
Stage 3A — configurable, transparently labelled scenario multipliers.

The multipliers live in `app/config/scenario_multipliers.json` (override with
`SCENARIO_MULTIPLIERS_PATH`) and can be adjusted per preset with
`SCENARIO_MULTIPLIER_OVERRIDES` (JSON). Every value is bounds-validated, and every
number the multipliers produce is labelled `modelled` with its derivation shown.

Integrity:
  * `calibrated: false` is part of the payload - these are planning assumptions.
  * Scenario output is never a forecast and never a compliance statement.
  * An effect that needs data we do not have is reported as NOT MODELLED, not estimated.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .datasets import NOT_MODELLED_EFFECTS

CONFIG_PATH = Path(os.getenv(
    "SCENARIO_MULTIPLIERS_PATH",
    str(Path(__file__).resolve().parent.parent / "config" / "scenario_multipliers.json"),
))
DEFAULT_PRESET = "baseline"

MODELLED_LABEL = "Modelled scenario - illustrative multipliers, not a measurement or a forecast"


class MultiplierConfigError(ValueError):
    """Raised when a multiplier override is unknown or outside its declared bounds."""


def load_config(path: Path | str | None = None) -> dict:
    cfg_path = Path(path) if path else CONFIG_PATH
    cfg = json.loads(cfg_path.read_text())
    for key in ("schema_version", "multiplier_definitions", "presets"):
        if key not in cfg:
            raise MultiplierConfigError(f"Scenario multiplier config is missing '{key}'")
    return cfg


def _override_map(overrides: dict | str | None) -> dict:
    if not overrides:
        raw = os.getenv("SCENARIO_MULTIPLIER_OVERRIDES", "").strip()
        if not raw:
            return {}
        overrides = raw
    if isinstance(overrides, str):
        try:
            overrides = json.loads(overrides)
        except ValueError as exc:
            raise MultiplierConfigError(f"SCENARIO_MULTIPLIER_OVERRIDES is not valid JSON: {exc}") from exc
    if not isinstance(overrides, dict):
        raise MultiplierConfigError("Multiplier overrides must be an object keyed by preset name.")
    return overrides


def validate_overrides(overrides: dict, config: dict | None = None) -> dict:
    """Validates `{preset: {multiplier: value}}` against the declared bounds."""
    cfg = config or load_config()
    defs = cfg["multiplier_definitions"]
    cleaned: dict[str, dict[str, float]] = {}
    for preset, values in overrides.items():
        if preset not in cfg["presets"]:
            raise MultiplierConfigError(f"Unknown scenario preset '{preset}'.")
        if not isinstance(values, dict):
            raise MultiplierConfigError(f"Overrides for '{preset}' must be an object.")
        cleaned[preset] = {}
        for key, value in values.items():
            if key not in defs:
                raise MultiplierConfigError(f"Unknown multiplier '{key}' for preset '{preset}'.")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise MultiplierConfigError(f"Multiplier '{key}' must be a number.")
            low, high = defs[key]["bounds"]
            if not (float(low) <= float(value) <= float(high)):
                raise MultiplierConfigError(
                    f"Multiplier '{key}'={value} for preset '{preset}' is outside bounds [{low}, {high}]."
                )
            cleaned[preset][key] = float(value)
    return cleaned


def multipliers_for(preset: str, overrides: dict | str | None = None, config: dict | None = None) -> dict[str, float]:
    cfg = config or load_config()
    preset_key = preset if preset in cfg["presets"] else DEFAULT_PRESET
    values = {k: float(v) for k, v in cfg["presets"][preset_key]["multipliers"].items()}
    validated = validate_overrides(_override_map(overrides), cfg)
    values.update(validated.get(preset_key, {}))
    return values


def registry_payload(overrides: dict | str | None = None) -> dict:
    cfg = load_config()
    validated = validate_overrides(_override_map(overrides))
    presets = {}
    for name, spec in cfg["presets"].items():
        resolved = {k: float(v) for k, v in spec["multipliers"].items()}
        resolved.update(validated.get(name, {}))
        presets[name] = {
            "label": spec.get("label", name),
            "description": spec.get("description", ""),
            "multipliers": {
                key: {
                    "value": resolved[key],
                    **{k: v for k, v in cfg["multiplier_definitions"][key].items() if k != "label"},
                    "label": cfg["multiplier_definitions"][key]["label"],
                    "overridden": key in validated.get(name, {}),
                }
                for key in resolved
            },
        }
    return {
        "schema_version": cfg["schema_version"],
        "data_class": cfg["data_class"],
        "calibrated": cfg["calibrated"],
        "label": cfg["label"],
        "provenance": cfg["provenance"],
        "usage_note": cfg["usage_note"],
        "config_path": str(CONFIG_PATH),
        "env_overrides": {
            "SCENARIO_MULTIPLIERS_PATH": "Path to an alternate multiplier JSON",
            "SCENARIO_MULTIPLIER_OVERRIDES": 'JSON, e.g. {"high-growth": {"land_take_factor": 1.5}}',
        },
        "overrides_applied": validated,
        "presets": presets,
        "not_modelled": NOT_MODELLED_EFFECTS,
        "disclaimer": (
            "Scenario output is modelled with configurable planning assumptions. It is not a "
            "measurement, not a calibrated model, not a scientific forecast, and not a compliance statement."
        ),
    }


def _indication(ind_id: str, label: str, value: float | None, unit: str, derivation: str,
                data_class: str = "estimated", status: str = "ok", reason: str | None = None) -> dict:
    return {
        "id": ind_id,
        "label": label,
        "value": None if value is None else round(float(value), 4),
        "unit": unit,
        "status": status,
        "reason": reason,
        "data_class": data_class if status == "ok" else "unavailable",
        "derivation": derivation,
    }


def build_impact_layer(
    *,
    base_fingerprint: str,
    base_content_digest: str,
    base_datasets: list[str],
    project: dict,
    preset: str,
    land_cover: dict | None = None,
    parcel: dict | None = None,
) -> dict:
    """
    Development impact layer: scenario multipliers applied to the *geographic base layer*.
    The base layer itself is never modified here - that is the whole point of the split.
    """
    cfg = load_config()
    resolved = multipliers_for(preset, config=cfg)
    preset_key = preset if preset in cfg["presets"] else DEFAULT_PRESET

    footprint_ha = float(project.get("footprint_ha") or 0.0)
    water_mld = float(project.get("water_mld") or 0.0)
    power_mw = float(project.get("power_mw") or 0.0)
    site_area_ha = float((parcel or {}).get("area_ha") or 0.0) or footprint_ha
    site_area_basis = "user parcel geometry" if (parcel or {}).get("area_ha") else "user-declared footprint"

    indications = [
        _indication(
            "indicative_land_take_ha", "Indicative land take",
            site_area_ha * resolved["land_take_factor"], "ha",
            f"{site_area_basis} {round(site_area_ha, 3)} ha x land_take_factor {resolved['land_take_factor']}",
        ),
        _indication(
            "indicative_water_demand_mld", "Indicative water demand",
            water_mld * resolved["water_intensity_factor"], "MLD",
            f"user water demand {water_mld} MLD x water_intensity_factor {resolved['water_intensity_factor']}",
        ),
        _indication(
            "indicative_power_mw", "Indicative power demand",
            power_mw * resolved["energy_intensity_factor"], "MW",
            f"user power demand {power_mw} MW x energy_intensity_factor {resolved['energy_intensity_factor']}",
        ),
        _indication(
            "indicative_disturbed_area_ha", "Indicative construction-disturbed area",
            site_area_ha * resolved["site_disturbance_factor"], "ha",
            f"{site_area_basis} {round(site_area_ha, 3)} ha x site_disturbance_factor {resolved['site_disturbance_factor']}",
        ),
        _indication(
            "indicative_impervious_area_ha", "Indicative impervious (sealed) area",
            site_area_ha * resolved["impervious_surface_factor"], "ha",
            f"{site_area_basis} {round(site_area_ha, 3)} ha x impervious_surface_factor {resolved['impervious_surface_factor']}",
        ),
    ]

    # Vegetation clearance needs real mapped tree cover. Without it we report UNAVAILABLE
    # rather than guessing a vegetation area.
    tree = None
    if isinstance(land_cover, dict) and land_cover.get("status") == "ok":
        for cls in land_cover.get("classes", []) or []:
            if cls.get("value") == 10:
                tree = cls
                break
    if tree is not None and site_area_ha > 0:
        retained = float(tree["area_ha"]) * resolved["vegetation_retention_factor"]
        indications.append(_indication(
            "indicative_vegetation_cleared_ha", "Indicative tree-cover clearance inside the AOI",
            max(0.0, float(tree["area_ha"]) - retained), "ha",
            "ESA WorldCover tree-cover area in the AOI "
            f"{tree['area_ha']} ha x (1 - vegetation_retention_factor {resolved['vegetation_retention_factor']}). "
            "Whole-AOI tree cover is an upper bound - it is not clipped to the parcel.",
        ))
    else:
        reason = "land_cover_unavailable"
        detail = "ESA WorldCover land cover is unavailable for this site, so no vegetation area is estimated."
        if isinstance(land_cover, dict) and land_cover.get("reason"):
            reason = land_cover["reason"]
            detail = land_cover.get("detail") or detail
        indications.append(_indication(
            "indicative_vegetation_cleared_ha", "Indicative tree-cover clearance inside the AOI",
            None, "ha", "Requires mapped tree cover (ESA WorldCover class 10).",
            status="unavailable", reason=f"{reason}: {detail}",
        ))

    return {
        "layer_model": "development_impact",
        "status": "modelled_only",
        "data_class": "modelled",
        "label": MODELLED_LABEL,
        "preset": preset_key,
        "preset_label": cfg["presets"][preset_key].get("label", preset_key),
        "multipliers_used": {
            key: {
                "value": value,
                "label": cfg["multiplier_definitions"][key]["label"],
                "unit": cfg["multiplier_definitions"][key]["unit"],
                "bounds": cfg["multiplier_definitions"][key]["bounds"],
                "basis": cfg["multiplier_definitions"][key]["basis"],
                "data_class": "modelled",
                "calibrated": False,
            }
            for key, value in resolved.items()
        },
        "project_inputs": {
            "footprint_ha": footprint_ha,
            "water_mld": water_mld,
            "power_mw": power_mw,
            "site_area_ha": round(site_area_ha, 4),
            "site_area_basis": site_area_basis,
            "data_class": "user_input",
        },
        "geographic_basis": {
            "base_fingerprint": base_fingerprint,
            "base_content_digest": base_content_digest,
            "datasets": base_datasets,
            "note": "The geographic base layer is unchanged by scenarios; only this impact layer varies.",
        },
        "indications": indications,
        "not_modelled": NOT_MODELLED_EFFECTS,
        "disclaimer": (
            "Modelled with configurable planning assumptions. Not a measurement, not a calibrated "
            "model, not a scientific forecast, and not a compliance statement."
        ),
    }
