# TerraLex — Environmental Intelligence Platform for Data Centres

Answer three questions before construction: **Should a data centre be built here? What environmental consequences could follow? What Indian environmental law applies and what is the exposure?** Screening tool only — *not* environmental clearance, compliance certification, or legal advice.

![stack](https://img.shields.io/badge/stack-React%20%2B%20FastAPI%20%2B%20Three.js%20%2B%20MapLibre-emerald)

## Run (demo, 2 minutes)
```bash
# Backend
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cd backend && ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000   # docs: /api/docs
# Frontend (new terminal)
npm --prefix frontend install && npm --prefix frontend run dev   # http://localhost:5173
```
Vite proxies `/api` → `:8000`. Or `docker compose up` (includes PostGIS service for production migration; demo backend uses portable SQLite with a PostGIS-compatible site/legal schema design — see Limitations).
Tests/build:
```bash
cd backend && ../.venv/bin/python -m pytest -q               # offline suite (live tests deselected)
cd backend && ../.venv/bin/python -m pytest -q -m live       # LIVE upstream tests (needs egress)
cd backend && ../.venv/bin/python scripts/verify_upstream.py # live provenance probe, prints real values
npm --prefix frontend test && npm --prefix frontend run build
```

## Demo journey (acceptance flow)
1. **Site Intelligence** → click map / enter lat-lon (presets: Ludhiana, Mohali, Hyderabad), set radius, footprint, water, power, built-up area → *Save & run assessment*.
2. **Environmental Assessment** → findings per category with risk, evidence (real OSM features + distances), source, quality, confidence. Climate hazards shown **UNAVAILABLE** (not fabricated).
3. **Legal Intelligence** → database-driven results: law, section, original (abridged, source-linked) text, plain-language explanation, why it applies, required approval, authority, status (`APPROVAL REQUIRED` / `HIGH LEGAL EXPOSURE` / `UNKNOWN` — never "compliant" by default), consequences, verified date. Search the legal DB live.
4. **Scenario Simulation** → Baseline / High-Growth / Mitigation presets (labelled *Modelled*).
5. **Environmental Timescape** → Three.js interactive 3D (orbit/zoom/pan/reset): Present/Development/Future × scenarios, ENU latitude-corrected projection scaled to `radius_km`, real **Copernicus DEM GLO-90 (~90 m) elevation via Open-Meteo** (configurable `N×N` grid, default `20×20` split into `≤100`-point batches, reporting site AMSL elevation, range, vertical exaggeration, and attribution; degrades to a **visually & textually distinct flat `0 m` reference grid** if elevation data is unavailable).
6. **Site Comparison** → save ≥2 sites; transparent inputs table, no arbitrary weighted "best site" score.
7. **Data Sources** → provenance for every dataset.

## Architecture
```
frontend/  React+TS+Tailwind, MapLibre (OSM raster), Three.js Timescape, typed API client (src/lib/api.ts)
backend/   FastAPI + Pydantic (app/main.py routes, app/schemas.py validation)
  app/services/landscape.py    Stage 3A orchestrator: base_layer (geography) + development_impact_layer (scenario), fingerprinted + cached
  app/services/osm.py          Stage 3A per-layer Overpass client (8 layers, failure isolation, distance semantics, optional geometry)
  app/services/landcover.py    Stage 3A ESA WorldCover 2021 v200 reader (COG byte-window over HTTP; rasterio optional)
  app/services/parcels.py      Stage 3A optional parcel GeoJSON validation (Shapely optional; ENU area + containment)
  app/services/datasets.py     Stage 3A dataset provenance + availability registry (single source of truth)
  app/services/scenario_multipliers.py + app/config/scenario_multipliers.json   Configurable multipliers + NOT MODELLED registry
  app/services/cache.py        Stage 3A generic namespaced LRU + SQLite + single-flight cache
  app/services/geo.py          Stage 3A shared ENU helpers (same constants as Stage 1/2 and the frontend)
  app/services/elevation.py    Configurable ENU DEM grid (default 20x20, <=100-pt batches) via Open-Meteo + memory/SQLite cache
  app/services/environment.py  Stage 1/2 findings; OSM access delegated to services/osm.py (output contract unchanged)
  app/services/legal.py        Applicability engine: findings+project triggers → DB query (no laws in code/UI beyond trigger keys)
  app/services/scenarios.py    Preset simulation (modelled, labelled)
  app/legal_seed.json          Curated legal DB seed (the ONLY place legal content lives)
  app/import_legal.py          Manual import pipeline (provenance required, dedupe by ID)
  tests/                       Backend suites: Stage 1/2 baseline (test_api.py, untouched) + Stage 3A suites
                               (landscape, osm, landcover, parcels, multipliers, datasets, regression)
                               + live-marked upstream tests (deselected without network)
  scripts/verify_upstream.py   Live provenance probe (Open-Meteo, Overpass, ESA WorldCover)
docker-compose.yml             PostGIS + backend + frontend (production path)
```

## Database schema (SQLite demo / PostGIS-ready)
`sites(id,name,lat,lon,radius_km,footprint_ha,water_mld,power_mw,cooling,built_up_m2,created_at)` · `analyses` · `scenarios` · `elevation_cache` · **`landscape_cache`**(cache_key,payload,created_at — site/resolution-scoped Stage 3A base layers) · **`legislation`**(id,title,type,jurisdiction,authority,enacted,effective,status,source_url,source_authority,retrieved,verified,version,checksum) · **`provisions`**(legislation_id,section,subsection,original_text,explanation,applicability,authority) · **`requirements`**(trigger_key,trigger_description,approval,authority,consequence_type,consequence) · **`amendments`**. Adding a law = add JSON/import, **zero frontend changes**.

## Legal sources actually imported (8 laws, 9 provisions, 8 requirements)
Environment (Protection) Act 1986 (ss.5, 15-as-amended) · Water Act 1974 (s.25/26) · Air Act 1981 (s.21) · EIA Notification 2006 (Item 8 thresholds) · Wetlands Rules 2017 (Rule 4) · Forest (Conservation) Act 1980 (s.2) · CGWA Groundwater Guidelines 2020 (NOC) · NGT Act 2010 (ss.14–15). Sources: India Code, MoEFCC/PARIVESH, CGWA — links in-app & DB. **Honesty notes:** statutory text in DB is *abridged excerpt* pointing to official full text; penalty amounts deliberately NOT hard-coded where amendments (e.g. Jan Vishwas 2023) changed them — read current text at source. Verified 2026-10-31 by curators; re-verify before reliance.

## Real vs synthetic
| Feature | Type |
|---|---|
| Base map, water/forest/wetland/settlement features & distances | **Real** — OpenStreetMap via Overpass (live) |
| Legal provisions | **Imported official-source** curated seed (abridged excerpts) |
| Fallback features if Overpass unreachable | **Synthetic, explicitly labelled** in API + UI |
| Terrain mesh in Timescape | **Real observation-derived DEM (`data_class: observation_derived`, not a direct measurement)** — Copernicus DEM GLO-90 (~90 m) via Open-Meteo (CC BY 4.0 non-commercial, ESA DOI `10.5270/ESA-c5d3d65`). It is a **Digital Surface Model** (canopy + structures, *not* bare earth). Degrades to an **explicitly labelled flat `0 m` fallback reference grid** if unavailable |
| Land cover classes / class areas | **Mapped + estimated** — ESA WorldCover 10 m 2021 v200 (CC BY 4.0, DOI `10.5281/zenodo.7254221`, 76.7 % overall accuracy). Class *areas* are **estimated** from pixel counts |
| Roads / rivers / land use (Stage 3A layers) | **Real mapped OSM geometry** — distance basis published per feature (`geometry_min_distance` vs `mapped_centroid`) |
| Parcel geometry (optional) | **User input, validated** — Shapely validity + ENU area; *not* a surveyed boundary. An invalid ring is only accepted if `make_valid()` leaves **polygonal area** (see containment note below) |
| Scenario multipliers | **Modelled, configurable, labelled** — bounds-validated planning assumptions, `calibrated: false` |
| Groundwater drawdown, flood risk, biodiversity loss, noise, air quality, heat island, GHG | **NOT MODELLED** — listed explicitly in the API, never approximated |
| Scenarios | **Modelled, labelled** — not measurements |
| Flood/heat/water-stress (WRI Aqueduct/IMD) | **Not integrated — shown UNAVAILABLE** |

## API
`GET /api/v1/health` · `POST/GET /api/v1/sites` · `GET /api/v1/sites/{id}` · `POST /api/v1/sites/{id}/analyze` · `GET /api/v1/sites/{id}/environment` · `GET /api/v1/elevation` · `POST /api/v1/sites/{id}/scenarios` · `GET /api/v1/sites/{id}/legal` · `GET /api/v1/legal/legislation[/{id}]` · `GET /api/v1/legal/provisions/{id}` · `POST /api/v1/legal/search` · `GET /api/v1/legal/import/template` · `GET /api/v1/data-sources` · OpenAPI at `/api/docs`.

**Stage 3A additions** — `GET /api/v1/landscape` · `POST /api/v1/landscape` (optional parcel) · `GET /api/v1/sites/{id}/landscape` · `GET /api/v1/landscape/layers` · `POST /api/v1/parcel/validate` · `GET /api/v1/datasets` · `GET /api/v1/scenario-multipliers`.

## Stage 3A — landscape data foundation (backend only)

`GET /api/v1/landscape` (and `POST` with an optional parcel) returns two strictly separated layers:

| Layer | Contents | May change between Present / Development / Future |
|---|---|---|
| `base_layer` | terrain (Copernicus DSM), mapped OSM geography, ESA WorldCover land cover | **No** — identical for the same site, resolution, layer selection and dataset versions |
| `development_impact_layer` | scenario multipliers applied to that geography | Yes |

- `base_layer.content_digest` and `development_impact_layer.geographic_basis.base_fingerprint` let a client **prove** the geography did not move between temporal views (pinned by `tests/test_landscape.py`).
- Every value carries a `data_class`: `measured` (direct point/gauge observation — **no TerraLex dataset is in this class today**) · `observation_derived` (Copernicus DEM GLO-90: sensor observations processed into a DEM/DSM product) · `mapped` (OSM, WorldCover classes) · `estimated` (areas, counts, fractions) · `modelled` (scenarios) · `user_input` · `unavailable`. Definitions and the reason for each class are published by `GET /api/v1/datasets`.
- A layer that cannot be retrieved returns `status: "unavailable"` with a `reason` and is **never cached**. Caching is **per layer**: successful OSM layers, the DEM grid and the land-cover read are each cached under their own site/resolution-scoped key, so a partial result does not discard the geography that did arrive, and a later request retries only what failed. The aggregate `landscape:v2` entry is written **only when every requested layer is `ok`**; a partial result sets `cacheable: false`, a `partial_coverage_warning` and a `data_label` that reads *PARTIAL mapped geography … NOT complete coverage*, and the response sends `Cache-Control: no-store`.
- **Distance semantics:** every OSM feature publishes `distance_basis` — `geometry_min_distance` (Shapely, true minimum distance to the mapped geometry), `vertex_min_distance` (Shapely absent), `mapped_centroid` (only a centroid was returned) or `point_feature`. The centroid of a long river or road is not a proximity to that feature, so the basis always travels with the number.
- **Copernicus DEM GLO-90 is a Digital Surface Model** — stated in the payload (`base_layer.terrain.surface_model`): it includes canopy and structures and is not bare earth. Re-gridding to `N×N` does not add resolution.
- **ESA WorldCover** is read as a byte window of the public Cloud-Optimized GeoTIFF (never a whole ~100 MB tile). Class areas are *estimated* from pixel counts using a per-pixel area at the site latitude; NoData is counted and never folded into a class; AOIs beyond the pixel budget are refused rather than downsampled.
- **Scenario multipliers** live in `app/config/scenario_multipliers.json`, are bounds-validated and published with `calibrated: false`. Effects without an authoritative source are returned as `NOT MODELLED` (groundwater drawdown, flood risk, biodiversity loss, noise, air quality, heat island, GHG, LST change).
- **Optional parcel:** `POST /api/v1/parcel/validate` (Shapely) and `POST /api/v1/landscape` with `parcel_geojson`. A rejected parcel is reported and never used as a land-cover mask.
- **Parcel containment is a polygon test, not a vertex test:** `within_site_radius` is Shapely `covers()` of the projected parcel against a `PARCEL_CIRCLE_QUAD_SEGS`-based circular buffer (256-segment by default) in the local ENU plane, so a parcel is only "inside" when its whole area is. `all_vertices_within_site_radius` is published separately and, on its own, does **not** prove containment; `max_vertex_distance_km` (planar) and `max_geodesic_vertex_distance_km` are both reported because the ENU plane is an approximation. `containment_method` / `containment_note` state exactly this in the payload.
- **Shapely repair never accepts a non-polygon:** `make_valid()` output is type-checked; polygonal components of a `GeometryCollection` are extracted (unioned) and zero-area leftovers (`LineString`, `Point`) are discarded and listed in `discarded_non_polygonal_components`. If repair leaves no polygonal area the parcel is **rejected** with HTTP 422. The response publishes the `geometry` actually used downstream — land-cover masking always uses the repaired geometry, never the raw input ring.
- Environment knobs: `OVERPASS_URL`, `OVERPASS_TIMEOUT_SEC`, `OVERPASS_MAX_ELEMENTS`, `OVERPASS_MAX_GEOMETRY_POINTS`, `WORLDCOVER_ENABLED`, `WORLDCOVER_URL_PREFIX`, `WORLDCOVER_MAX_WINDOW_PIXELS`, `WORLDCOVER_MAX_SAMPLED_PIXELS`, `WORLDCOVER_TIMEOUT_SEC`, `MAX_PARCEL_VERTICES`, `SCENARIO_MULTIPLIERS_PATH`, `SCENARIO_MULTIPLIER_OVERRIDES`, `PARCEL_CIRCLE_QUAD_SEGS`, `ELEVATION_GRID_SIZE`.

The frontend is unchanged in Stage 3A — the landscape endpoints are additive.

## Known limitations & Render persistence note
- **Render ephemeral SQLite storage:** Without a mounted Render Persistent Disk, container-local `/srv/platform.db` resets on redeploy or idle spin-down (`GET /api/v1/health` reports `storage.mode = "ephemeral_default"`). To keep Open-Meteo usage controlled without relying solely on container disk: (1) backend uses a bounded in-memory LRU cache + single-flight request deduplication + best-effort SQLite `elevation_cache`, (2) `GET /api/v1/elevation` sets `Cache-Control: public, max-age=604800`, and (3) the frontend persists verified DEM grids in a bounded `localStorage` cache (`terralex:elevation_cache:v2`). For server-side persistence across restarts on Render, mount a Persistent Disk and set `DATABASE_URL_SQLITE=/var/data/platform.db`, or migrate to PostgreSQL/PostGIS (`docker-compose.yml`).
- **Open-Meteo usage terms:** Free API (`https://api.open-meteo.com/v1/elevation`) is strictly for **non-commercial use** (`<10,000` calls/day, `<5,000`/hr, `<600`/min, CC BY 4.0) with mandatory Open-Meteo + Copernicus DEM (`DOI: 10.5270/ESA-c5d3d65`) attribution; configure `OPEN_METEO_ELEVATION_URL` + `OPEN_METEO_API_KEY` for commercial plans or self-hosted Open-Meteo.
- **Optional geospatial dependencies:** `shapely` and `rasterio` are optional at runtime. Without `rasterio` the land-cover layer returns `reason: "rasterio_unavailable"`; without `shapely` parcel validation returns HTTP 503 and geometry distances fall back to a labelled `vertex_min_distance`. `rasterio` bundles GDAL and adds roughly 60-90 MB to the image.
- **WorldCover read budget:** tiles are ~100 MB COGs, so only a byte window is fetched. AOIs above `WORLDCOVER_MAX_WINDOW_PIXELS` (default 16 M pixels) are reported `aoi_exceeds_landcover_budget` instead of being downsampled, and windows above `WORLDCOVER_MAX_SAMPLED_PIXELS` (default 2 M) are systematically subsampled with the stride published in the payload.
- **Cache namespaces are versioned:** DEM grids live under `dem:v2`, assembled base layers under `landscape:v2`, and the browser cache under `terralex:elevation_cache:v2` (per-entry keys `dem:v2:`). The version was bumped when the payload gained `data_class` / `surface_model` (Copernicus GLO-90 is `observation_derived`, not `measured`), so a payload written by older code can never be served again. Both read paths additionally **fail closed**: a cached `ok` terrain payload without a current `data_class`/`surface_model` is treated as a cache miss and re-fetched, and legacy `dem:v1` / `landscape:v1` rows are purged at startup (`purge_legacy_cache_rows()`, cache tables only). Pinned by `tests/test_cache_versioning.py` and `frontend/tests/elevationCache.test.ts`.
- **Public Overpass courtesy:** the shared instance is rate-limited. Stage 3A issues one bounded query per layer (`OVERPASS_MAX_ELEMENTS`, default 250) with per-layer isolation; point `OVERPASS_URL` at a private mirror for production load.
- **Upstream verification requires egress:** the Stage 3A development sandbox blocked TLS to Open-Meteo, Overpass and the WorldCover bucket, so `scripts/verify_upstream.py` reported 0/15 PASS there and the `live` tests stay deselected. Run `pytest -m live` and the script on a machine with internet before relying on live layers.
- SQLite demo store (compose provides PostGIS; spatial queries currently Haversine in Python); OSM is crowd-sourced (verify on ground); legal DB is a small verified seed, not exhaustive; no auth; large JS bundle (code-splitting TODO); geocoding search is via map-click/presets/manual coords. Secrets via env vars only; coordinates validated server-side (Pydantic).

## Screenshots
Run locally and capture: Overview dashboard, Site Intelligence map, Legal Intelligence card, Timescape (Present→Development, Mitigation). (Live preview is provided in the Arena session.)
