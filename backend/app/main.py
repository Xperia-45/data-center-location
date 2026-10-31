import uuid, json, datetime
from typing import Annotated
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from .db import init_db, conn, get_storage_persistence_info
from .schemas import SiteIn, ScenarioIn, LegalSearch, ParcelValidateIn, LandscapeRequest
from .services.elevation import (
    DEFAULT_ELEVATION_GRID_SIZE,
    failed_elevation_retry_is_due,
    get_elevation_grid,
)
from .services.environment import analyze, sort_features_by_distance
from .services.legal import evaluate
from .services.scenarios import simulate, PRESETS
from .services.datasets import data_sources_view
from .services.datasets import registry_payload as dataset_registry_payload
from .services.landscape import get_landscape
from .services.osm import DISTANCE_BASIS_NOTES, LAYER_SPECS
from .services.parcels import (
    ParcelValidationUnavailable,
    ParcelValidationError,
    shapely_available,
    validate_parcel,
)
from .services.scenario_multipliers import MultiplierConfigError
from .services.scenario_multipliers import registry_payload as multiplier_registry_payload

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
        # A failed grid used to be re-fetched on EVERY call, because the stored payload only
        # ever became "ok". One upstream 429 then turned every page view into a fresh request
        # against a per-IP rate limit. Retry policy now lives in the elevation service:
        # transient failures are retried at most once per ELEVATION_FAILURE_RETRY_DELAY_SEC,
        # non-retryable ones not at all. An explicit grid_size still forces a fetch, because
        # that is a different resolution.
        needs_grid = not isinstance(existing_elev, dict)
        grid_mismatch = (
            isinstance(existing_elev, dict)
            and grid_size is not None
            and existing_elev.get("grid_size") != grid_size
        )
        retry_due = (
            isinstance(existing_elev, dict)
            and existing_elev.get("status") != "ok"
            and failed_elevation_retry_is_due(existing_elev)
        )
        if needs_grid or grid_mismatch or retry_due:
            refreshed = await get_elevation_grid(
                site["lat"], site["lon"], site["radius_km"], grid_size=desired_grid
            )
            payload["elevation"] = refreshed
            if grid_size is None:
                # Failures are persisted too: `observed_at` is what makes the retry window
                # measurable across requests and container restarts.
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
    """
    Legacy provenance list (Stage 1/2 response shape, keys consumed by the frontend).
    Stage 3A: derived from the single dataset registry in services/datasets.py.
    """
    return data_sources_view()


# ---------------------------------------------------------------------------
# Stage 3A - landscape (geographic base layer + development impact layer)
# ---------------------------------------------------------------------------

def _landscape_cache_headers(response: Response, payload: dict) -> None:
    """Cache the base geography; never let a degraded response be stored by clients."""
    if payload.get("all_layers_ok"):
        response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
    else:
        response.headers["Cache-Control"] = "no-store"


def _split_csv(raw: str | None) -> list[str] | None:
    if raw is None or not str(raw).strip():
        return None
    return [part.strip() for part in str(raw).split(",") if part.strip()]


@app.get("/api/v1/landscape")
async def landscape(
    response: Response,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(default=5.0, gt=0, le=50),
    grid_size: int = Query(default=DEFAULT_ELEVATION_GRID_SIZE, ge=2, le=32),
    layers: str | None = Query(default=None, description="Comma separated: dem,osm,landcover"),
    osm_layers: str | None = Query(default=None, description="Comma separated OSM layer ids"),
    include_geometry: bool = Query(default=False),
    preset: str = Query(default="baseline"),
    footprint_ha: float | None = Query(default=None, gt=0, le=1000),
    water_mld: float | None = Query(default=None, ge=0, le=100),
    power_mw: float | None = Query(default=None, gt=0, le=2000),
    refresh: bool = Query(default=False),
):
    """
    Geographic base layer + development impact layer for free coordinates.

    The base layer (terrain / mapped geography / land cover) is scenario-independent;
    only `development_impact_layer` changes with `preset`.
    """
    project = {"footprint_ha": footprint_ha, "water_mld": water_mld, "power_mw": power_mw}
    payload = await get_landscape(
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        grid_size=grid_size,
        layers=_split_csv(layers),
        osm_layers=_split_csv(osm_layers),
        include_geometry=include_geometry,
        project=project,
        preset=preset,
        use_cache=not refresh,
    )
    _landscape_cache_headers(response, payload)
    return payload


@app.post("/api/v1/landscape")
async def landscape_with_parcel(req: LandscapeRequest, response: Response):
    """
    Landscape for free coordinates plus an OPTIONAL parcel polygon.

    When the parcel validates it becomes the land-cover AOI and the impact-layer site
    area; when it does not, the reason is returned and the base layer falls back to the
    site circle rather than masking with an unusable geometry.
    """
    payload = await get_landscape(
        lat=req.lat,
        lon=req.lon,
        radius_km=req.radius_km,
        grid_size=req.grid_size,
        layers=req.layers,
        osm_layers=req.osm_layers,
        include_geometry=req.include_geometry,
        parcel_geojson=req.parcel_geojson,
        project=req.project.model_dump() if req.project else None,
        preset=req.preset,
        use_cache=not req.refresh,
    )
    _landscape_cache_headers(response, payload)
    return payload


@app.get("/api/v1/sites/{site_id}/landscape")
async def site_landscape(
    site_id: str,
    response: Response,
    grid_size: Annotated[int | None, Query(ge=2, le=32)] = None,
    layers: str | None = Query(default=None, description="Comma separated: dem,osm,landcover"),
    osm_layers: str | None = Query(default=None),
    include_geometry: bool = Query(default=False),
    preset: str = Query(default="baseline"),
    refresh: bool = Query(default=False),
):
    """Landscape for a saved site, using the site's own project inputs."""
    site = get_site(site_id)
    payload = await get_landscape(
        lat=site["lat"],
        lon=site["lon"],
        radius_km=site["radius_km"],
        grid_size=grid_size,
        layers=_split_csv(layers),
        osm_layers=_split_csv(osm_layers),
        include_geometry=include_geometry,
        project={
            "footprint_ha": site["footprint_ha"],
            "water_mld": site["water_mld"],
            "power_mw": site["power_mw"],
        },
        preset=preset,
        use_cache=not refresh,
    )
    payload["site"]["site_id"] = site_id
    payload["site"]["name"] = site["name"]
    _landscape_cache_headers(response, payload)
    return payload


@app.get("/api/v1/landscape/layers")
def landscape_layers():
    """Catalogue of OSM base-layer queries and the distance semantics each one reports."""
    return {
        "osm_layers": {
            lid: {
                "label": spec["label"],
                "kind": spec["kind"],
                "linear_feature": spec["linear"],
                "data_class": "mapped",
                "source": "OpenStreetMap contributors via Overpass API",
                "legal_trigger_keys": spec["trigger_keys"],
            }
            for lid, spec in LAYER_SPECS.items()
        },
        "distance_basis_definitions": DISTANCE_BASIS_NOTES,
        "landscape_layers": ["dem", "osm", "landcover"],
        "note": (
            "Distances to mapped linear features depend on the reported distance_basis. "
            "A centroid distance is not a perpendicular distance to the feature."
        ),
    }


@app.post("/api/v1/parcel/validate")
def parcel_validate(inp: ParcelValidateIn):
    """
    Validates an optional user parcel polygon (GeoJSON Polygon/MultiPolygon) with Shapely.

    422 = geometry unusable. 503 = Shapely unavailable, so the parcel is NOT accepted.
    """
    site = inp.site.model_dump() if inp.site else None
    try:
        return validate_parcel(inp.geojson, site)
    except ParcelValidationError as exc:
        raise HTTPException(422, str(exc))
    except ParcelValidationUnavailable as exc:
        raise HTTPException(503, str(exc))


@app.get("/api/v1/datasets")
def datasets():
    """Dataset provenance + availability registry.

    Every dataset is published with a `data_class` (measured / observation_derived / mapped /
    estimated / modelled / user_input / unavailable) and its definition, so a client can see
    exactly how much of each number is observed and how much is derived.
    """
    return dataset_registry_payload()


@app.get("/api/v1/scenario-multipliers")
def scenario_multipliers():
    """Configurable scenario multipliers with bounds, basis and explicit NOT MODELLED list."""
    try:
        payload = multiplier_registry_payload()
    except MultiplierConfigError as exc:
        raise HTTPException(400, str(exc))
    payload["shapely_available"] = shapely_available()
    return payload


@app.get("/api/v1/scenario-presets")
def presets():
    return PRESETS
