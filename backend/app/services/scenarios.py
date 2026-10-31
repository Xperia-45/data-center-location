PRESETS={"baseline":{"footprint_ha":10,"water_mld":1.0,"power_mw":20,"cooling":"air","intensity":1.0},"high-growth":{"footprint_ha":25,"water_mld":3.5,"power_mw":60,"cooling":"water","intensity":1.8},"mitigation":{"footprint_ha":10,"water_mld":0.4,"power_mw":22,"cooling":"hybrid-dry","intensity":0.9}}
def simulate(site, inp):
    p=PRESETS.get(inp.preset, PRESETS["baseline"])
    water=inp.water_mld; footprint=inp.footprint_ha; power=inp.power_mw
    mitigated=len(inp.mitigation)
    water_eff=max(0.1, water - mitigated*0.08)
    return {"preset":inp.preset,"label":"Modelled scenario - not an observed measurement","inputs":inp.model_dump(),"outputs":{"water_demand_mld":round(water_eff,2),"annual_water_million_litres":round(water_eff*365,1),"land_pressure_index":round(footprint/10*p["intensity"],2),"energy_mw":power,"community_exposure_note":"Exposure rises with footprint/water demand; compare against settlement findings from assessment.","regulatory_note":"Higher water/built-up values are more likely to cross EIA/CGWA trigger thresholds - re-run Legal tab after changing assumptions."},"mitigation_applied":inp.mitigation}
