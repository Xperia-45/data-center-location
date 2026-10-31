import uuid, json, datetime
from typing import Annotated
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from .db import init_db, conn, get_storage_persistence_info
from .schemas import SiteIn, ScenarioIn, LegalSearch
from .services.elevation import DEFAULT_ELEVATION_GRID_SIZE, get_elevation_grid
from .services.environment import analyze, sort_features_by_distance
from .services.legal import evaluate
from .services.scenarios import simulate, PRESETS

app = FastAPI(title="Environmental Intelligence Platform", version="1.0.0", docs_url="/api/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
init_db()

def now():
    return datetime.datetime.utcnow().isoformat() + "Z"

def get_site(sid):
    c = conn()
    r = c.execute("SELECT * FROM sites WHERE id=?", (sid,)).fetchone()
    c.close()
    if not r:
        raise HTTPException(404, "Site not found")
    return dict(r)

@app.get("/api/v1/health")
def health():
    return {
        "status": "ok",
        "service": "environmental-intelligence",
        "time": now(),
        "storage": get_storage_persistence_info(),
    }

@app.post("/api/v1/sites", status_code=201)
def create_site(s: SiteIn):
    sid = str(uuid.uuid4())
    c = conn()
    c.execute(
        "INSERT INTO sites VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sid, s.name, s.lat, s.lon, s.radius_km, s.footprint_ha, s.water_mld, s.power_mw, s.cooling, s.built_up_m2, now()),
    )
    c.commit()
    c.close()
    return get_site(sid)

@app.get("/api/v1/sites")
def list_sites():
    c = conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM sites ORDER BY created_at DESC")]
    c.close()
    return rows

@app.get("/api/v1/sites/{site_id}")
def site_detail(site_id: str):
    return get_site(site_id)

@app.post("/api/v1/sites/{site_id}/analyze")
async def do_analyze(
    site_id: str,
    grid_size: Annotated[int | None, Query(ge=2, le=32)] = None,
):
    site = get_site(site_id)
    result = await analyze(site, elevation_grid_size=grid_size)
    aid = str(uuid.uuid4())
    c = conn()
    c.execute("INSERT INTO analyses VALUES (?,?,?,?)", (aid, site_id, json.dumps(result), now()))
    c.commit()
    c.close()
    return result

@app.get("/api/v1/sites/{site_id}/environment")
async def env(
    site_id: str,
    grid_size: Annotated[int | None, Query(ge=2, le=32)] = None,
):
    site = get_site(site_id)
    c = conn()
    r = c.execute(
        "SELECT id, payload FROM analyses WHERE site_id=? ORDER BY created_at DESC LIMIT 1",
        (site_id,),
    ).fetchone()
    c.close()
    if r:
        aid = r[0]
        payload = json.loads(r[1])
        if isinstance(payload.get("features"), list):
            payload["features"] = sort_features_by_distance(payload["features"])
        existing_elev = payload.get("elevation")
        desired_grid = grid_size if grid_size is not None else (existing_elev.get("grid_size") if isinstance(existing_elev, dict) else None)
        if (
            not isinstance(existing_elev, dict)
            or existing_elev.get("status") != "ok"
            or (grid_size is not None and existing_elev.get("grid_size") != grid_size)
        ):
            refreshed = await get_elevation_grid(
                site["lat"], site["lon"], site["radius_km"], grid_size=desired_grid
            )
            payload["elevation"] = refreshed
            if refreshed.get("status") == "ok" and grid_size is None:
                c2 = conn()
                c2.execute("UPDATE analyses SET payload=? WHERE id=?", (json.dumps(payload), aid))
                c2.commit()
                c2.close()
        return payload
    return await do_analyze(site_id, grid_size=grid_size)

@app.get("/api/v1/elevation")
async def elevation_endpoint(
    response: Response,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(default=5.0, gt=0, le=50),
    grid_size: int = Query(default=DEFAULT_ELEVATION_GRID_SIZE, ge=2, le=32),
):
    grid = await get_elevation_grid(lat, lon, radius_km, grid_size=grid_size)
    if grid.get("status") == "ok":
        response.headers["Cache-Control"] = "public, max-age=604800, stale-while-revalidate=86400"
    else:
        response.headers["Cache-Control"] = "no-store"
    return grid

@app.post("/api/v1/sites/{site_id}/scenarios")
def scenario(site_id: str, inp: ScenarioIn):
    site = get_site(site_id)
    out = simulate(site, inp)
    c = conn()
    c.execute("INSERT INTO scenarios VALUES (?,?,?,?)", (str(uuid.uuid4()), site_id, json.dumps(out), now()))
    c.commit()
    c.close()
    return out

@app.get("/api/v1/sites/{site_id}/legal")
async def legal_for_site(site_id: str):
    site = get_site(site_id)
    c = conn()
    r = c.execute("SELECT payload FROM analyses WHERE site_id=? ORDER BY created_at DESC LIMIT 1", (site_id,)).fetchone()
    c.close()
    envdata = json.loads(r[0]) if r else await do_analyze(site_id)
    return {
        "site_id": site_id,
        "results": evaluate(site, envdata["findings"]),
        "disclaimer": "Regulatory screening only - not legal advice or compliance certification. Verify current text at official source.",
    }

@app.get("/api/v1/legal/legislation")
def legislation():
    c = conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM legislation")]
    c.close()
    return rows

@app.get("/api/v1/legal/legislation/{legislation_id}")
def legislation_one(legislation_id: str):
    c = conn()
    r = c.execute("SELECT * FROM legislation WHERE id=?", (legislation_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Legislation not found")
    prov = [dict(x) for x in c.execute("SELECT * FROM provisions WHERE legislation_id=?", (legislation_id,))]
    amend = [dict(x) for x in c.execute("SELECT * FROM amendments WHERE legislation_id=?", (legislation_id,))]
    c.close()
    d = dict(r)
    d["provisions"] = prov
    d["amendments"] = amend
    return d

@app.get("/api/v1/legal/provisions/{provision_id}")
def provision(provision_id: str):
    c = conn()
    r = c.execute("SELECT * FROM provisions WHERE id=?", (provision_id,)).fetchone()
    c.close()
    if not r:
        raise HTTPException(404, "Provision not found")
    return dict(r)

@app.post("/api/v1/legal/search")
def search(q: LegalSearch):
    like = f"%{q.query}%"
    c = conn()
    rows = [
        dict(r)
        for r in c.execute(
            "SELECT p.*, l.title, l.source_url FROM provisions p JOIN legislation l ON l.id=p.legislation_id WHERE p.original_text LIKE ? OR p.explanation LIKE ? OR l.title LIKE ?",
            (like, like, like),
        )
    ]
    c.close()
    return rows

@app.get("/api/v1/legal/import/template")
def import_template():
    return {
        "format": "JSON with legislation[], provisions[], requirements[] - see backend/app/legal_seed.json",
        "note": "Manual import pipeline: validate JSON, preserve original text, record source_url/retrieved/verified, then load via seed/import script. No frontend change needed for new laws.",
    }

@app.get("/api/v1/data-sources")
def sources():
    return [
        {
            "provider": "OpenStreetMap / Overpass API",
            "dataset": "Water, wetland, forest, settlements",
            "url": "https://overpass-api.de",
            "license": "ODbL",
            "quality": "Third-party",
            "status": "Integrated (live, with labelled synthetic fallback)",
        },
        {
            "provider": "Open-Meteo Elevation API / Copernicus DEM",
            "dataset": "Copernicus DEM 2021 release GLO-90 (90m terrain elevation, DOI: 10.5270/ESA-c5d3d65)",
            "url": "https://open-meteo.com/en/docs/elevation-api",
            "license": "CC BY 4.0 (non-commercial free tier) / Copernicus open licence",
            "quality": "Measured global DEM (~90m)",
            "status": "Integrated (configurable ENU grid, default 20x20 in <=100-point batches, cached, with explicit flat fallback if unavailable)",
        },
        {
            "provider": "India Code",
            "dataset": "Central Acts full text",
            "url": "https://www.indiacode.nic.in",
            "license": "Official source",
            "quality": "Verified official",
            "status": "Curated seed import; manual import pipeline",
        },
        {
            "provider": "MoEFCC / PARIVESH",
            "dataset": "EIA Notification & clearances",
            "url": "https://environmentclearance.nic.in",
            "license": "Official source",
            "quality": "Verified official",
            "status": "Referenced in legal DB",
        },
        {
            "provider": "CGWA",
            "dataset": "Groundwater NOC guidelines",
            "url": "https://cgwa-noc.gov.in",
            "license": "Official source",
            "quality": "Verified official",
            "status": "Referenced in legal DB",
        },
        {
            "provider": "WRI Aqueduct / IMD / CWC",
            "dataset": "Water stress, flood, climate hazards",
            "url": "https://www.wri.org/aqueduct",
            "license": "Check provider terms",
            "quality": "Not yet integrated",
            "status": "Planned - shown as UNAVAILABLE, never fabricated",
        },
    ]

@app.get("/api/v1/scenario-presets")
def presets():
    return PRESETS
