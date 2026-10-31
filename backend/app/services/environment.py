import math, httpx
def haversine(a,b,c,d):
    R=6371; p1,p2=math.radians(a),math.radians(c); dp=math.radians(c-a); dl=math.radians(d-b)
    x=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(x))
async def overpass_features(lat,lon,radius_km):
    """Real OSM data via Overpass. Returns (features, ok)."""
    q=f"""[out:json][timeout:12];(node(around:{int(radius_km*1000)},{lat},{lon})[natural=water];way(around:{int(radius_km*1000)},{lat},{lon})[natural=water];way(around:{int(radius_km*1000)},{lat},{lon})[waterway];way(around:{int(radius_km*1000)},{lat},{lon})[landuse=forest];way(around:{int(radius_km*1000)},{lat},{lon})[natural=wetland];way(around:{int(radius_km*1000)},{lat},{lon})[leisure=nature_reserve];node(around:{int(radius_km*1000)},{lat},{lon})[place~"village|town|city|hamlet"];);out center 200;"""
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            r=await cl.post("https://overpass-api.de/api/interpreter", data={"data":q}, headers={"User-Agent":"EnvIntelligencePlatform/0.1 hackathon"})
            r.raise_for_status(); els=r.json().get("elements",[])
    except Exception:
        return [], False
    feats=[]
    for e in els:
        c=e.get("center") or {"lat":e.get("lat"),"lon":e.get("lon")}
        if c.get("lat") is None: continue
        tags=e.get("tags",{})
        kind="water" if tags.get("natural")=="water" or "waterway" in tags else "forest" if tags.get("landuse")=="forest" else "wetland" if tags.get("natural")=="wetland" else "protected" if "nature_reserve" in str(tags.get("leisure")) else "settlement"
        feats.append({"name":tags.get("name","Unnamed OSM feature"),"kind":kind,"lat":c["lat"],"lon":c["lon"],"distance_km":round(haversine(lat,lon,c["lat"],c["lon"]),2),"source":"OpenStreetMap / Overpass API"})
    return feats, True

def classify(count, kind, nearest):
    if kind in ("wetland","protected") and nearest is not None and nearest<1: return "Critical"
    if nearest is not None and nearest<1: return "High"
    if nearest is not None and nearest<3: return "Moderate"
    if count: return "Low"
    return "Low"

async def analyze(site):
    feats, ok = await overpass_features(site["lat"], site["lon"], site["radius_km"])
    data_label = "Verified real data (OpenStreetMap)" if ok else "Synthetic demonstration data (OSM unavailable - labelled, do not use for decisions)"
    if not ok:
        # deterministic labelled synthetic fallback
        feats=[{"name":"Demo water body (synthetic)","kind":"water","lat":site["lat"]+0.02,"lon":site["lon"]+0.02,"distance_km":2.5,"source":"Synthetic demo fallback"},{"name":"Demo settlement (synthetic)","kind":"settlement","lat":site["lat"]-0.015,"lon":site["lon"]+0.01,"distance_km":1.8,"source":"Synthetic demo fallback"}]
    by={}
    for f in feats: by.setdefault(f["kind"],[]).append(f)
    def finding(cat, key, expl):
        lst=by.get(key,[]); nearest=min([x["distance_km"] for x in lst], default=None)
        risk=classify(len(lst),key,nearest)
        return {"category":cat,"finding":f"{len(lst)} {key} feature(s) within {site['radius_km']} km" + (f"; nearest {nearest} km" if nearest is not None else ""),"risk":risk,"evidence":lst[:10],"source":"OpenStreetMap contributors via Overpass API" if ok else "Synthetic demo fallback","data_quality":"Third-party crowd-sourced; verify on ground" if ok else "Synthetic - not a measurement","last_updated":"Live query at analysis time" if ok else "n/a","confidence":"Medium" if ok else "Low - synthetic","explanation":expl,"data_label":data_label,"trigger_keys": triggers_for(key, site)}
    findings=[
        finding("Water","water","Proximity to surface water affects cooling-water options, flood review and discharge consenting."),
        finding("Land and ecology","wetland","Wetlands trigger Wetlands Rules 2017 review; proximity raises ecological sensitivity."),
        finding("Land and ecology","forest","Forest proximity triggers forest-diversion and biodiversity review."),
        finding("Land and ecology","protected","Protected/nature-reserve proximity raises EIA and clearance sensitivity."),
        finding("Community","settlement","Nearby settlements indicate community exposure to noise, water demand and traffic."),
    ]
    # climate: explicitly unavailable, not fabricated
    findings.append({"category":"Climate and hazards","finding":"Authoritative flood/heat/drought layer not connected in prototype","risk":"UNKNOWN","evidence":[],"source":"WRI Aqueduct / IMD / CWC - not yet integrated (see Data Sources)","data_quality":"Unavailable","last_updated":"n/a","confidence":"Unknown","explanation":"No hazard claim is made without a connected authoritative dataset.","data_label":"Unavailable data","trigger_keys":[]})
    if site["water_mld"]>=0.5:
        findings.append({"category":"Water","finding":f"Project water demand {site['water_mld']} MLD - groundwater/surface abstraction approval screening required","risk":"Moderate" if site["water_mld"]<3 else "High","evidence":[],"source":"User-provided project input","data_quality":"User input, unverified","last_updated":"At submission","confidence":"Medium","explanation":"Cooling water demand is a key consenting and community-conflict driver in water-stressed regions.","data_label":"User-provided input","trigger_keys":["water_extraction"]})
    return {"site_id":site["id"],"findings":findings,"features":feats,"data_label":data_label,"disclaimer":"Screening tool only. Not environmental clearance or legal advice."}

def triggers_for(kind, site):
    m={"wetland":["wetland_proximity"],"forest":["forest_proximity"],"protected":["wetland_proximity"],"water":[],"settlement":[],"water_body":[]}
    return m.get(kind,[])
