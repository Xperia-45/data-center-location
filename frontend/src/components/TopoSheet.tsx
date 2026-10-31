/**
 * Topographic sheet for the Environmental Timescape: a Survey-of-India-inspired 2D cartographic
 * view built ONLY from what the Stage 3A backend actually returned.
 *
 * There is deliberately no remote tile server and no glyph server here. The style is a local
 * object, every source is our own GeoJSON, and place names are HTML labels positioned in screen
 * space - so the sheet renders identically offline, in a preview iframe, and on Render, and it
 * can never show a coastline or a road that our datasets did not supply.
 *
 * When the DEM is unavailable the sheet loses its contours and tinting and says so explicitly;
 * the mapped geography keeps rendering, because those layers are retrieved independently.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { Site } from '../lib/api.ts';
import {
  type CartoCollection,
  type CartographyResult,
  type ContourResult,
  type SheetSummary,
  TOPO_PALETTE,
  computeScaleBar,
  elevationAtLatLon,
} from '../lib/cartography.ts';

export interface TopoSheetProps {
  site: Site;
  cartography: CartographyResult;
  contours: ContourResult;
  tint: { collection: CartoCollection; available: boolean };
  summary: SheetSummary;
  footprint: { collection: CartoCollection; sideM: number };
  /** Raw DEM sample grid, for the coordinate/elevation readout. Null when the DEM is unavailable. */
  terrainValues?: number[][] | null;
  scenarioLabel: string;
  temporal: string;
  loading?: boolean;
  height?: number;
}

const EMPTY: CartoCollection = { type: 'FeatureCollection', features: [] };

/** Area classes in paint order (first = bottom), with their SoI-inspired fill and outline. */
const AREA_STYLES: [string, string, string, number][] = [
  ['farmland', TOPO_PALETTE.farmland, TOPO_PALETTE.farmland, 0.75],
  ['vegetation', TOPO_PALETTE.vegetation, TOPO_PALETTE.vegetationEdge, 0.8],
  ['settlement', TOPO_PALETTE.settlement, TOPO_PALETTE.settlementEdge, 0.85],
  ['protected', TOPO_PALETTE.protected, '#9c8a5f', 0.6],
  ['water_area', TOPO_PALETTE.water, TOPO_PALETTE.waterEdge, 0.9],
  ['wetland', TOPO_PALETTE.wetland, TOPO_PALETTE.waterEdge, 0.8],
];

const SOURCE_IDS = [
  'tint', ...AREA_STYLES.map(([id]) => id), 'road', 'waterway', 'contours', 'site-radius', 'footprint',
];

/** Map bounds covering the analysis circle with a little paper around the edge. */
function boundsFor(lat: number, lon: number, radiusKm: number): [[number, number], [number, number]] {
  const cosLat = Math.max(1e-6, Math.cos((lat * Math.PI) / 180));
  const halfLat = (radiusKm / 111.32) * 1.08;
  const halfLon = (radiusKm / (111.32 * cosLat)) * 1.08;
  return [[lon - halfLon, lat - halfLat], [lon + halfLon, lat + halfLat]];
}

export default function TopoSheet({
  site, cartography, contours, tint, summary, footprint, terrainValues, scenarioLabel, temporal, loading, height = 560,
}: TopoSheetProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const labelsRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState({ meters: 0, pixels: 0, text: '—' });
  const [readout, setReadout] = useState<{ lat: number; lon: number; elevM: number | null } | null>(null);

  // The DEM grid the readout interpolates. Kept in a ref so the mousemove handler is cheap and
  // so the map is created exactly once.
  const terrainRef = useRef<number[][] | null>(terrainValues ?? null);
  useEffect(() => { terrainRef.current = terrainValues ?? null; }, [terrainValues]);

  const bounds = useMemo(() => boundsFor(site.lat, site.lon, site.radius_km), [site.lat, site.lon, site.radius_km]);

  const siteCircle = useMemo<CartoCollection>(() => {
    const cosLat = Math.max(1e-6, Math.cos((site.lat * Math.PI) / 180));
    const coords: [number, number][] = [];
    for (let i = 0; i <= 96; i++) {
      const a = (i / 96) * Math.PI * 2;
      coords.push([
        site.lon + (site.radius_km / (111.32 * cosLat)) * Math.cos(a),
        site.lat + (site.radius_km / 111.32) * Math.sin(a),
      ]);
    }
    return { type: 'FeatureCollection', features: [{ type: 'Feature', id: 'site-radius', properties: {}, geometry: { type: 'LineString', coordinates: coords } }] };
  }, [site.lat, site.lon, site.radius_km]);

  // ------------------------------------------------------------------ map lifecycle
  useEffect(() => {
    if (!containerRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: {
        version: 8,
        sources: {},
        layers: [{ id: 'paper', type: 'background', paint: { 'background-color': TOPO_PALETTE.paper } }],
      },
      bounds,
      fitBoundsOptions: { padding: 24, duration: 0 },
      attributionControl: false,
      maxPitch: 0,
      dragRotate: false,
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    map.addControl(
      new maplibregl.AttributionControl({ compact: true, customAttribution: 'Sheet drawn from retrieved datasets only' }),
      'bottom-right'
    );
    map.on('move', () => setScale(computeScaleBar(map.getZoom(), map.getCenter().lat)));
    map.on('mousemove', (e) => {
      setReadout({
        lat: e.lngLat.lat,
        lon: e.lngLat.lng,
        elevM: elevationAtLatLon({
          lat: e.lngLat.lat,
          lon: e.lngLat.lng,
          values_m: terrainRef.current,
          span_km: summary.spanKm,
          siteLat: site.lat,
          siteLon: site.lon,
        }),
      });
    });
    map.on('mouseout', () => setReadout(null));
    mapRef.current = map;
    setScale(computeScaleBar(map.getZoom(), site.lat));
    return () => { map.remove(); mapRef.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Recenter when the site changes.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.fitBounds(bounds, { padding: 24, duration: 500 });
  }, [bounds]);

  // ------------------------------------------------------------------ data -> sources
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const upsert = (id: string, data: CartoCollection) => {
      const existing = map.getSource(id) as maplibregl.GeoJSONSource | undefined;
      if (existing) existing.setData(data as any);
      else map.addSource(id, { type: 'geojson', data: data as any });
    };

    // Sources and styles are created ONCE; the data is refreshed on EVERY change. The previous
    // single guarded `paint()` early-returned as soon as the layers existed, so a second site's
    // geography never reached the map and the sheet kept drawing the first site's features.
    const ensureLayers = () => {
      if (map.getLayer('tint')) return;
      for (const id of SOURCE_IDS) {
        if (!map.getSource(id)) map.addSource(id, { type: 'geojson', data: EMPTY as any });
      }
      map.addLayer({
        id: 'tint', type: 'fill', source: 'tint',
        paint: {
          'fill-color': [
            'interpolate', ['linear'], ['get', 'band'],
            0, '#dfe8c8', 1, '#e7e3bb', 2, '#e9dcb4', 3, '#dfc9a0', 4, '#d2b189', 5, '#c39a76',
          ],
          'fill-opacity': 0.55,
        },
      });
      for (const [id, color, outline, opacity] of AREA_STYLES) {
        map.addLayer({ id, type: 'fill', source: id, paint: { 'fill-color': color, 'fill-opacity': opacity } });
        map.addLayer({
          id: `${id}-outline`, type: 'line', source: id,
          paint: { 'line-color': outline, 'line-width': 0.6, 'line-opacity': 0.7 },
        });
      }
      map.addLayer({
        id: 'road', type: 'line', source: 'road',
        paint: {
          'line-color': ['case', ['>=', ['get', 'road_rank'], 4], TOPO_PALETTE.roadMajor, TOPO_PALETTE.road],
          'line-width': ['interpolate', ['linear'], ['get', 'road_rank'], 1, 0.6, 6, 2.1],
          'line-opacity': 0.85,
        },
      });
      map.addLayer({
        id: 'waterway', type: 'line', source: 'waterway',
        paint: {
          'line-color': TOPO_PALETTE.waterEdge,
          'line-width': 1.5,
          // Seasonal drainage (a choe, a monsoon nullah) is dashed, as on a survey sheet.
          'line-dasharray': ['case', ['get', 'intermittent'], ['literal', [3, 2]], ['literal', [1]]],
        },
      });
      map.addLayer({
        id: 'contours', type: 'line', source: 'contours',
        paint: {
          'line-color': ['case', ['get', 'index'], TOPO_PALETTE.contourIndex, TOPO_PALETTE.contour],
          'line-width': ['case', ['get', 'index'], 1.1, 0.55],
          'line-opacity': 0.85,
        },
      });
      map.addLayer({
        id: 'site-radius', type: 'line', source: 'site-radius',
        paint: { 'line-color': '#2f6f4f', 'line-width': 1.4, 'line-dasharray': [4, 3] },
      });
      map.addLayer({
        id: 'footprint-fill', type: 'fill', source: 'footprint',
        paint: { 'fill-color': '#7c3aed', 'fill-opacity': 0.22 },
      });
      map.addLayer({
        id: 'footprint-line', type: 'line', source: 'footprint',
        paint: { 'line-color': '#5b21b6', 'line-width': 1.6, 'line-dasharray': [2, 2] },
      });
    };

    const refreshData = () => {
      upsert('tint', tint.available ? tint.collection : EMPTY);
      for (const [id] of AREA_STYLES) {
        upsert(id, cartography.layers[id as keyof typeof cartography.layers].collection);
      }
      upsert('road', cartography.layers.road.collection);
      upsert('waterway', cartography.layers.waterway.collection);
      upsert('contours', contours.collection);
      upsert('site-radius', siteCircle);
      upsert('footprint', footprint.collection);
    };

    const run = () => { ensureLayers(); refreshData(); };
    if (map.loaded()) run();
    else map.once('load', run);
  }, [cartography, contours, tint, siteCircle, footprint]);

  // ------------------------------------------------------------------ labels
  useEffect(() => {
    const map = mapRef.current;
    const host = labelsRef.current;
    if (!map || !host) return;
    const draw = () => {
      host.innerHTML = '';
      const named = cartography.layers.label.collection.features
        .filter((f) => f.properties.name)
        .sort((a, b) => Number(b.properties.place_rank ?? 0) - Number(a.properties.place_rank ?? 0))
        .slice(0, 14);
      const taken: { x: number; y: number; w: number }[] = [];
      for (const feature of named) {
        const [lon, lat] = (feature.geometry as any).coordinates as [number, number];
        const p = map.project([lon, lat]);
        const width = String(feature.properties.name).length * 6.4 + 10;
        if (taken.some((t) => Math.abs(t.x - p.x) < (t.w + width) / 2 && Math.abs(t.y - p.y) < 15)) continue;
        taken.push({ x: p.x, y: p.y, w: width });
        const el = document.createElement('div');
        el.className = 'topo-label';
        el.textContent = String(feature.properties.name);
        el.style.left = `${p.x}px`;
        el.style.top = `${p.y}px`;
        host.appendChild(el);
      }
      // Named hydrology and roads, so a sheet without place nodes is still readable.
      for (const cls of ['waterway', 'water_area', 'road'] as const) {
        const list = cartography.layers[cls].collection.features
          .filter((f) => f.properties.name && String(f.properties.name).toLowerCase().indexOf('unnamed') !== 0)
          .slice(0, 6);
        for (const feature of list) {
          const coords = (feature.geometry as any).coordinates;
          const flat: [number, number][] = Array.isArray(coords[0][0]) ? coords[0] : coords;
          const mid = flat[Math.floor(flat.length / 2)];
          if (!mid) continue;
          const p = map.project(mid);
          const width = String(feature.properties.name).length * 6.0 + 10;
          if (taken.some((t) => Math.abs(t.x - p.x) < (t.w + width) / 2 && Math.abs(t.y - p.y) < 14)) continue;
          taken.push({ x: p.x, y: p.y, w: width });
          const el = document.createElement('div');
          el.className = `topo-label topo-label-${cls}`;
          el.textContent = String(feature.properties.name);
          el.style.left = `${p.x}px`;
          el.style.top = `${p.y}px`;
          host.appendChild(el);
        }
      }
    };
    draw();
    map.on('move', draw);
    return () => { map.off('move', draw); };
  }, [cartography]);

  const present = useMemo(
    () => summary.layerStatuses.filter((l) => l.available),
    [summary.layerStatuses]
  );
  const missing = useMemo(
    () => summary.layerStatuses.filter((l) => !l.available),
    [summary.layerStatuses]
  );

  return (
    <div className="rounded-xl border border-stone-300 bg-[#f6f1e3] overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-stone-300 bg-[#efe7d2] px-3 py-2 text-[11px] text-stone-700">
        <div className="font-semibold tracking-wide uppercase text-stone-800">
          Topographic sheet · {site.name || `${site.lat.toFixed(4)}, ${site.lon.toFixed(4)}`}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded bg-stone-200 px-1.5 py-0.5">{temporal}</span>
          <span className="rounded bg-stone-200 px-1.5 py-0.5">{scenarioLabel}</span>
          {summary.terrainAvailable ? (
            <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-900">
              {summary.dataClass} · {summary.resolutionM} m DSM
            </span>
          ) : (
            <span className="rounded bg-amber-200 px-1.5 py-0.5 text-amber-900">
              Elevation unavailable ({summary.terrainReason})
            </span>
          )}
          {loading ? <span className="rounded bg-stone-200 px-1.5 py-0.5">loading…</span> : null}
        </div>
      </div>

      <div className="relative" style={{ height }}>
        <div ref={containerRef} style={{ height, width: '100%' }} aria-label="Topographic sheet of the selected site" />
        <div ref={labelsRef} className="pointer-events-none absolute inset-0 overflow-hidden" />

        {/* North arrow */}
        <div className="absolute left-3 top-3 flex flex-col items-center rounded bg-[#f6f1e3]/90 px-2 py-1 text-stone-800 shadow">
          <svg width="18" height="24" viewBox="0 0 18 24" aria-hidden="true">
            <path d="M9 1 L14 20 L9 15 L4 20 Z" fill="#4a3f30" />
          </svg>
          <span className="text-[10px] font-bold leading-none">N</span>
        </div>

        {/* Scale bar */}
        <div className="absolute bottom-3 left-3 rounded bg-[#f6f1e3]/90 px-2 py-1 text-[10px] text-stone-800 shadow">
          <div className="mb-1 font-semibold">{scale.text}</div>
          <div
            className="h-2 border-x-2 border-b-2 border-stone-800"
            style={{ width: `${Math.max(20, scale.pixels)}px` }}
          />
        </div>

        {/* Coordinate / elevation readout */}
        <div className="absolute bottom-3 right-3 rounded bg-[#f6f1e3]/90 px-2 py-1 text-[10px] text-stone-800 shadow">
          {readout ? (
            <>
              <div>{readout.lat.toFixed(5)}° N, {readout.lon.toFixed(5)}° E</div>
              {readout.elevM === null ? (
                <div className="text-amber-800">Elevation not measured (DEM unavailable)</div>
              ) : (
                <div>{readout.elevM.toFixed(1)} m AMSL (interpolated)</div>
              )}
            </>
          ) : (
            <div>Hover the sheet for coordinates</div>
          )}
        </div>
      </div>

      {/* Legend + integrity */}
      <div className="grid gap-3 border-t border-stone-300 bg-[#efe7d2] px-3 py-2 text-[11px] text-stone-700 md:grid-cols-3">
        <div>
          <div className="mb-1 font-semibold uppercase text-stone-800">Legend</div>
          <ul className="space-y-0.5">
            <li><Swatch color={TOPO_PALETTE.water} /> Lakes and standing water</li>
            <li><Swatch color={TOPO_PALETTE.waterEdge} line /> Rivers, streams, canals (dashed = seasonal)</li>
            <li><Swatch color={TOPO_PALETTE.wetland} /> Wetland</li>
            <li><Swatch color={TOPO_PALETTE.vegetation} /> Forest, woodland, scrub</li>
            <li><Swatch color={TOPO_PALETTE.farmland} /> Agricultural land</li>
            <li><Swatch color={TOPO_PALETTE.settlement} /> Settlement / built-up</li>
            <li><Swatch color={TOPO_PALETTE.roadMajor} line /> Roads (major)</li>
            <li><Swatch color={TOPO_PALETTE.contour} line /> Contours</li>
            <li><Swatch color="#7c3aed" /> Modelled footprint envelope</li>
          </ul>
        </div>
        <div>
          <div className="mb-1 font-semibold uppercase text-stone-800">Terrain</div>
          {summary.terrainAvailable ? (
            <ul className="space-y-0.5">
              <li>Contour interval: <b>{summary.contourIntervalM} m</b> (index every {summary.contourIntervalM * 5} m)</li>
              <li>Contours drawn: {summary.contourCount} levels</li>
              <li>Range: {summary.minM}–{summary.maxM} m AMSL (relief {summary.reliefM} m)</li>
              <li>Sample spacing: {summary.sampleSpacingKm} km · grid {summary.gridSize}×{summary.gridSize}</li>
              <li>Native resolution: ~{summary.resolutionM} m GLO-90</li>
              <li className="text-stone-600">{summary.surfaceModel}</li>
            </ul>
          ) : (
            <div className="rounded bg-amber-100 p-2 text-amber-900">
              <b>No contours and no elevation tint are drawn.</b> The DEM is unavailable
              ({summary.terrainReason}). The terrain in 3D is an explicitly labelled flat reference
              plane; no elevation has been estimated and none has been set to zero. Mapped
              geography below is retrieved independently and is still shown.
            </div>
          )}
        </div>
        <div>
          <div className="mb-1 font-semibold uppercase text-stone-800">Layers</div>
          <ul className="space-y-0.5">
            {present.map((l) => (
              <li key={l.id} className="text-emerald-900">
                ✓ {l.label} <span className="text-stone-600">({l.data_class})</span>
              </li>
            ))}
            {missing.map((l) => (
              <li key={l.id} className="text-amber-900">
                ✗ {l.label} — unavailable{l.reason ? `: ${l.reason}` : ''}
              </li>
            ))}
          </ul>
          <div className="mt-1 text-stone-600">
            {summary.mappedFeatures} mapped features drawn · contours are interpolated between
            samples, which does not add native resolution.
          </div>
          {summary.osmAttribution ? <div className="text-stone-600">{summary.osmAttribution}</div> : null}
          {summary.landCoverEpoch ? <div className="text-stone-600">ESA WorldCover {summary.landCoverEpoch}</div> : null}
          {footprint.sideM > 0 ? (
            <div className="text-stone-600">
              Footprint envelope {footprint.sideM} m square from the declared area — modelled, not surveyed.
            </div>
          ) : null}
        </div>
      </div>

      <style>{`
        .topo-label{position:absolute;transform:translate(-50%,-140%);font:italic 600 11px/1.2 Georgia,'Times New Roman',serif;color:#4a3f30;text-shadow:0 0 3px #f6f1e3,0 0 3px #f6f1e3;white-space:nowrap}
        .topo-label-waterway,.topo-label-water_area{color:#2f5f8f;font-style:italic}
        .topo-label-road{color:#6b4a3a;font-style:normal;font-weight:500;font-size:10px}
      `}</style>
    </div>
  );
}

function Swatch({ color, line }: { color: string; line?: boolean }) {
  return (
    <span
      className="mr-1 inline-block align-middle"
      style={line
        ? { width: 16, height: 0, borderTop: `2px solid ${color}` }
        : { width: 12, height: 12, background: color, border: '1px solid rgba(0,0,0,0.25)' }}
    />
  );
}
