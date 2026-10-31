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
Tests/build: `cd backend && ../.venv/bin/python -m pytest -q` · `npm --prefix frontend run build`

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
  app/services/elevation.py    Configurable ENU DEM grid (default 20x20, <=100-pt batches) via Open-Meteo + memory/SQLite cache
  app/services/environment.py  Live Overpass/OSM queries + concurrent DEM fetch → findings; labelled synthetic fallback if OSM unreachable
  app/services/legal.py        Applicability engine: findings+project triggers → DB query (no laws in code/UI beyond trigger keys)
  app/services/scenarios.py    Preset simulation (modelled, labelled)
  app/legal_seed.json          Curated legal DB seed (the ONLY place legal content lives)
  app/import_legal.py          Manual import pipeline (provenance required, dedupe by ID)
  tests/test_api.py            4 tests: health, validation, legal seed, full site→legal→scenario flow
docker-compose.yml             PostGIS + backend + frontend (production path)
```

## Database schema (SQLite demo / PostGIS-ready)
`sites(id,name,lat,lon,radius_km,footprint_ha,water_mld,power_mw,cooling,built_up_m2,created_at)` · `analyses` · `scenarios` · **`legislation`**(id,title,type,jurisdiction,authority,enacted,effective,status,source_url,source_authority,retrieved,verified,version,checksum) · **`provisions`**(legislation_id,section,subsection,original_text,explanation,applicability,authority) · **`requirements`**(trigger_key,trigger_description,approval,authority,consequence_type,consequence) · **`amendments`**. Adding a law = add JSON/import, **zero frontend changes**.

## Legal sources actually imported (8 laws, 9 provisions, 8 requirements)
Environment (Protection) Act 1986 (ss.5, 15-as-amended) · Water Act 1974 (s.25/26) · Air Act 1981 (s.21) · EIA Notification 2006 (Item 8 thresholds) · Wetlands Rules 2017 (Rule 4) · Forest (Conservation) Act 1980 (s.2) · CGWA Groundwater Guidelines 2020 (NOC) · NGT Act 2010 (ss.14–15). Sources: India Code, MoEFCC/PARIVESH, CGWA — links in-app & DB. **Honesty notes:** statutory text in DB is *abridged excerpt* pointing to official full text; penalty amounts deliberately NOT hard-coded where amendments (e.g. Jan Vishwas 2023) changed them — read current text at source. Verified 2026-10-31 by curators; re-verify before reliance.

## Real vs synthetic
| Feature | Type |
|---|---|
| Base map, water/forest/wetland/settlement features & distances | **Real** — OpenStreetMap via Overpass (live) |
| Legal provisions | **Imported official-source** curated seed (abridged excerpts) |
| Fallback features if Overpass unreachable | **Synthetic, explicitly labelled** in API + UI |
| Terrain mesh in Timescape | **Real measured DEM** — Copernicus DEM GLO-90 (~90 m) via Open-Meteo (CC BY 4.0 non-commercial, ESA DOI `10.5270/ESA-c5d3d65`); degrades to an **explicitly labelled flat `0 m` fallback reference grid** if unavailable |
| Scenarios | **Modelled, labelled** — not measurements |
| Flood/heat/water-stress (WRI Aqueduct/IMD) | **Not integrated — shown UNAVAILABLE** |

## API
`GET /api/v1/health` · `POST/GET /api/v1/sites` · `GET /api/v1/sites/{id}` · `POST /api/v1/sites/{id}/analyze` · `GET /api/v1/sites/{id}/environment` · `GET /api/v1/elevation` · `POST /api/v1/sites/{id}/scenarios` · `GET /api/v1/sites/{id}/legal` · `GET /api/v1/legal/legislation[/{id}]` · `GET /api/v1/legal/provisions/{id}` · `POST /api/v1/legal/search` · `GET /api/v1/legal/import/template` · `GET /api/v1/data-sources` · OpenAPI at `/api/docs`.

## Known limitations & Render persistence note
- **Render ephemeral SQLite storage:** Without a mounted Render Persistent Disk, container-local `/srv/platform.db` resets on redeploy or idle spin-down (`GET /api/v1/health` reports `storage.mode = "ephemeral_default"`). To keep Open-Meteo usage controlled without relying solely on container disk: (1) backend uses a bounded in-memory LRU cache + single-flight request deduplication + best-effort SQLite `elevation_cache`, (2) `GET /api/v1/elevation` sets `Cache-Control: public, max-age=604800`, and (3) the frontend persists verified DEM grids in a bounded `localStorage` cache (`terralex:elevation_cache:v1`). For server-side persistence across restarts on Render, mount a Persistent Disk and set `DATABASE_URL_SQLITE=/var/data/platform.db`, or migrate to PostgreSQL/PostGIS (`docker-compose.yml`).
- **Open-Meteo usage terms:** Free API (`https://api.open-meteo.com/v1/elevation`) is strictly for **non-commercial use** (`<10,000` calls/day, `<5,000`/hr, `<600`/min, CC BY 4.0) with mandatory Open-Meteo + Copernicus DEM (`DOI: 10.5270/ESA-c5d3d65`) attribution; configure `OPEN_METEO_ELEVATION_URL` + `OPEN_METEO_API_KEY` for commercial plans or self-hosted Open-Meteo.
- SQLite demo store (compose provides PostGIS; spatial queries currently Haversine in Python); OSM is crowd-sourced (verify on ground); legal DB is a small verified seed, not exhaustive; no auth; large JS bundle (code-splitting TODO); geocoding search is via map-click/presets/manual coords. Secrets via env vars only; coordinates validated server-side (Pydantic).

## Screenshots
Run locally and capture: Overview dashboard, Site Intelligence map, Legal Intelligence card, Timescape (Present→Development, Mitigation). (Live preview is provided in the Arena session.)
