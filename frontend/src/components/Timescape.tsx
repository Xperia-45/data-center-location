import { useEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import type { Site } from '../lib/api';
import type { CartographyResult } from '../lib/cartography.ts';
import { graticulePoints, sceneGeographyFromCartography } from '../lib/sceneGeography.ts';
import {
  ANALYSIS_RADIUS_UNITS,
  type ElevationGridPayload,
  MAX_SCENE_FEATURES,
  TERRAIN_SPAN,
  computeCampusLayout,
  computeTerrainVerticalParams,
  elevationColorRgb,
  isValidElevationGrid,
  resolveTimescapeElevation,
  resolveTimescapeFeatures,
  sampleElevationMeters,
  sanitizeRadiusKm,
  selectSceneFeatures,
  terrainHeightAt,
} from '../lib/timescapeMath';

export interface TimescapeProps {
  site: Site;
  features: any[];
  elevation?: ElevationGridPayload | null;
  analysisSiteId?: string;
  dataLabel?: string;
  loading?: boolean;
  temporal: string;
  scenario: string;
  showLegal: boolean;
  /** Mapped geography from /api/v1/landscape, draped on the terrain. Null until it loads. */
  cartography?: CartographyResult | null;
  /** True while the landscape request is in flight. */
  geographyLoading?: boolean;
  /** Reason the landscape layer could not be retrieved, for the on-scene notice. */
  geographyUnavailableReason?: string | null;
}

function disposeObjectTree(root: THREE.Object3D) {
  root.traverse((obj) => {
    if (obj === root) return;
    const renderable = obj as THREE.Object3D & {
      geometry?: THREE.BufferGeometry;
      material?: THREE.Material | THREE.Material[];
    };
    if (renderable.geometry) renderable.geometry.dispose();
    if (Array.isArray(renderable.material)) {
      renderable.material.forEach((m) => m.dispose());
    } else if (renderable.material) {
      renderable.material.dispose();
    }
  });
  while (root.children.length > 0) {
    root.remove(root.children[0]);
  }
}

export default function Timescape({
  site,
  features,
  elevation = null,
  analysisSiteId,
  dataLabel,
  loading = false,
  temporal,
  scenario,
  showLegal,
  cartography = null,
}: TimescapeProps) {
  const ref = useRef<HTMLDivElement>(null);
  const resetRef = useRef<() => void>(() => {});
  const sceneRef = useRef<THREE.Scene | null>(null);
  const terrainGroupRef = useRef<THREE.Group | null>(null);
  const geographyGroupRef = useRef<THREE.Group | null>(null);
  const dynamicGroupRef = useRef<THREE.Group | null>(null);
  const lastSiteKeyRef = useRef<string>('');

  const radiusKm = sanitizeRadiusKm(site.radius_km);

  // Never apply an elevation grid if loading, if analysisSiteId mismatches site.id, or if coords/radius mismatch
  const activeElevation = useMemo(
    () => resolveTimescapeElevation({ site, elevation, analysisSiteId, loading }),
    [site, elevation, analysisSiteId, loading]
  );

  const terrainParams = useMemo(
    () => computeTerrainVerticalParams(activeElevation, radiusKm),
    [activeElevation, radiusKm]
  );

  const safeFeatures = useMemo(
    () => resolveTimescapeFeatures({ site, features, analysisSiteId, loading }),
    [site, features, analysisSiteId, loading]
  );

  const selectedFeatures = useMemo(
    () => (site ? selectSceneFeatures(safeFeatures, site, MAX_SCENE_FEATURES, activeElevation) : []),
    [safeFeatures, site, activeElevation]
  );

  // Mount WebGL scene, renderer, lights, and OrbitControls once
  useEffect(() => {
    const container = ref.current;
    if (!container) return;
    const h = 460;
    const w = Math.max(320, container.clientWidth);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b1f17);
    scene.fog = new THREE.Fog(0x0b1f17, 85, 190);
    sceneRef.current = scene;

    const cam = new THREE.PerspectiveCamera(50, w / h, 0.1, 500);
    cam.position.set(28, 24, 32);

    const ren = new THREE.WebGLRenderer({ antialias: true });
    ren.setSize(w, h);
    container.innerHTML = '';
    container.appendChild(ren.domElement);

    const controls = new OrbitControls(cam, ren.domElement);
    controls.enableDamping = true;
    resetRef.current = () => {
      cam.position.set(28, 24, 32);
      controls.target.set(0, 0, 0);
      controls.update();
    };

    scene.add(new THREE.AmbientLight(0xffffff, 0.72));
    const sun = new THREE.DirectionalLight(0xffffff, 1.45);
    sun.position.set(22, 34, 14);
    scene.add(sun);

    const terrainGroup = new THREE.Group();
    scene.add(terrainGroup);
    terrainGroupRef.current = terrainGroup;

    const geographyGroup = new THREE.Group();
    scene.add(geographyGroup);
    geographyGroupRef.current = geographyGroup;

    const dynamicGroup = new THREE.Group();
    scene.add(dynamicGroup);
    dynamicGroupRef.current = dynamicGroup;

    const onResize = () => {
      if (!ref.current) return;
      const nw = Math.max(320, ref.current.clientWidth);
      cam.aspect = nw / h;
      cam.updateProjectionMatrix();
      ren.setSize(nw, h);
    };
    window.addEventListener('resize', onResize);
    const resizeObs =
      typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => onResize()) : null;
    resizeObs?.observe(container);

    let frameId = 0;
    const loop = () => {
      frameId = requestAnimationFrame(loop);
      controls.update();
      ren.render(scene, cam);
    };
    loop();

    return () => {
      cancelAnimationFrame(frameId);
      window.removeEventListener('resize', onResize);
      resizeObs?.disconnect();
      disposeObjectTree(terrainGroup);
      disposeObjectTree(geographyGroup);
      disposeObjectTree(dynamicGroup);
      terrainGroupRef.current = null;
      geographyGroupRef.current = null;
      dynamicGroupRef.current = null;
      sceneRef.current = null;
      controls.dispose();
      ren.dispose();
    };
  }, []);

  // Rebuild terrain mesh whenever site radius or DEM elevation changes
  useEffect(() => {
    const terrainGroup = terrainGroupRef.current;
    const scene = sceneRef.current;
    if (!terrainGroup || !scene) return;

    disposeObjectTree(terrainGroup);

    const measured = isValidElevationGrid(activeElevation) && terrainParams.isMeasured;
    const bgHex = measured ? 0x0b1f17 : 0x0f172a;
    scene.background = new THREE.Color(bgHex);
    scene.fog = new THREE.Fog(bgHex, 85, 190);

    if (measured && terrainParams.minM !== null && terrainParams.maxM !== null) {
      // Real (observed-derived) Copernicus DEM terrain mesh with hypsometric vertex shading
      const segs = 56;
      const geo = new THREE.PlaneGeometry(TERRAIN_SPAN, TERRAIN_SPAN, segs, segs);
      const pos = geo.attributes.position;
      const colors = new Float32Array(pos.count * 3);

      for (let i = 0; i < pos.count; i++) {
        const vx = pos.getX(i);
        const vy = pos.getY(i);
        // After rotation.x = -Math.PI/2, local (vx, vy, vz) -> world (vx, +vz, -vy)
        const worldX = vx;
        const worldZ = -vy;
        const heightY = terrainHeightAt(worldX, worldZ, activeElevation, radiusKm);
        pos.setZ(i, heightY);

        const elevM = sampleElevationMeters(worldX, worldZ, activeElevation) ?? terrainParams.centerM ?? 0;
        const [r, g, b] = elevationColorRgb(elevM, terrainParams.minM, terrainParams.maxM);
        colors[i * 3] = r;
        colors[i * 3 + 1] = g;
        colors[i * 3 + 2] = b;
      }
      geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
      geo.computeVertexNormals();

      const mat = new THREE.MeshStandardMaterial({
        vertexColors: true,
        roughness: 0.92,
        metalness: 0.04,
      });
      const mesh = new THREE.Mesh(geo, mat);
      mesh.rotation.x = -Math.PI / 2;
      terrainGroup.add(mesh);
    } else {
      // Explicit visually distinct fallback: flat zero-elevation slate reference plane + grid
      const geo = new THREE.PlaneGeometry(TERRAIN_SPAN, TERRAIN_SPAN, 4, 4);
      const mat = new THREE.MeshStandardMaterial({
        color: 0x1e293b,
        roughness: 0.96,
        metalness: 0.05,
      });
      const flatPlane = new THREE.Mesh(geo, mat);
      flatPlane.rotation.x = -Math.PI / 2;
      terrainGroup.add(flatPlane);

      // A 1 km reference graticule, not a game grid: it states the scale of the flat plane and
      // nothing about relief. Mapped geography (rivers, roads, water) is still draped on top by
      // the geography pass, so a missing DEM does not leave an empty plane.
      const graticuleMat = new THREE.LineBasicMaterial({
        color: 0x8ea6b8, transparent: true, opacity: 0.42,
      });
      for (const line of graticulePoints(radiusKm, 1)) {
        const geo = new THREE.BufferGeometry().setFromPoints(
          line.map((p) => new THREE.Vector3(p.x, p.y, p.z))
        );
        terrainGroup.add(new THREE.Line(geo, graticuleMat));
      }
    }
  }, [activeElevation, terrainParams, radiusKm]);

  // Drape the mapped geography on the terrain. This is the SAME projection and the SAME retrieved
  // features the 2D topographic sheet draws, so the two views can never disagree - and when the
  // DEM is unavailable the features still draw, flattened onto the labelled reference plane.
  useEffect(() => {
    const group = geographyGroupRef.current;
    if (!group || !site) return;
    disposeObjectTree(group);
    if (!cartography) return;

    const draped = sceneGeographyFromCartography({
      layers: cartography.layers as any,
      site,
      elevation: activeElevation,
    });

    const AREA_COLORS: Record<string, number> = {
      water_area: 0x3f7fbf,
      wetland: 0x6f9fb5,
      vegetation: 0x4f7a3a,
      farmland: 0x8a7f45,
      settlement: 0x9a7f5f,
      protected: 0x7d6b3f,
    };
    const AREA_OPACITY: Record<string, number> = {
      water_area: 0.82, wetland: 0.66, vegetation: 0.6, farmland: 0.45, settlement: 0.5, protected: 0.4,
    };

    for (const area of draped.areas) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.Float32BufferAttribute(area.triangles, 3));
      geo.computeVertexNormals();
      const mesh = new THREE.Mesh(
        geo,
        new THREE.MeshStandardMaterial({
          color: AREA_COLORS[area.class] ?? 0x8899aa,
          transparent: true,
          opacity: AREA_OPACITY[area.class] ?? 0.5,
          roughness: 0.95,
          metalness: 0.02,
          side: THREE.DoubleSide,
        })
      );
      mesh.name = `geo-area:${area.class}:${area.id}`;
      group.add(mesh);
    }

    for (const line of draped.lines) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.Float32BufferAttribute(line.vertices, 3));
      geo.computeVertexNormals();
      const isWater = line.class === 'waterway';
      const mesh = new THREE.Mesh(
        geo,
        new THREE.MeshStandardMaterial({
          // Seasonal drainage (a choe, a monsoon nullah) is paler, matching the dashed 2D sheet.
          color: isWater ? (line.intermittent ? 0x8fb8d8 : 0x2f6fa8) : 0xb08d62,
          transparent: true,
          opacity: isWater ? (line.intermittent ? 0.6 : 0.92) : 0.85,
          roughness: 0.9,
          metalness: 0.02,
          side: THREE.DoubleSide,
        })
      );
      mesh.name = `geo-line:${line.class}:${line.id}`;
      group.add(mesh);
    }
  }, [cartography, activeElevation, radiusKm, site]);

  // Update dynamic campus, radius boundary, and ENU-projected features
  useEffect(() => {
    const group = dynamicGroupRef.current;
    if (!group || !site) return;

    disposeObjectTree(group);

    const siteKey = `${site.id ?? ''}:${site.lat}:${site.lon}:${radiusKm}`;
    if (lastSiteKeyRef.current && lastSiteKeyRef.current !== siteKey) {
      resetRef.current();
    }
    lastSiteKeyRef.current = siteKey;

    // Analysis radius ring (ANALYSIS_RADIUS_UNITS = site.radius_km) conforming to terrain
    const ringPts: THREE.Vector3[] = [];
    const segs = 96;
    for (let i = 0; i < segs; i++) {
      const a = (i / segs) * Math.PI * 2;
      const rx = Math.cos(a) * ANALYSIS_RADIUS_UNITS;
      const rz = Math.sin(a) * ANALYSIS_RADIUS_UNITS;
      ringPts.push(new THREE.Vector3(rx, terrainHeightAt(rx, rz, activeElevation, radiusKm) + 0.14, rz));
    }
    const radiusRingGeo = new THREE.BufferGeometry().setFromPoints(ringPts);
    const radiusRing = new THREE.LineLoop(
      radiusRingGeo,
      new THREE.LineBasicMaterial({ color: 0x10b981, transparent: true, opacity: 0.9 })
    );
    group.add(radiusRing);

    // Candidate campus geometry scaled proportionally to footprint_ha vs radius_km
    const campus = computeCampusLayout(site, scenario, temporal, activeElevation);
    if (campus.visible) {
      const pad = new THREE.Mesh(
        new THREE.BoxGeometry(campus.padWidth, campus.padThickness, campus.padDepth),
        new THREE.MeshStandardMaterial({ color: 0x475569, transparent: true, opacity: 0.85 })
      );
      pad.position.set(0, campus.padY, 0);
      group.add(pad);

      for (const h of campus.halls) {
        const hall = new THREE.Mesh(
          new THREE.BoxGeometry(h.width, h.height, h.depth),
          new THREE.MeshStandardMaterial({
            color: scenario === 'mitigation' ? 0x94a3b8 : 0xcbd5e1,
            roughness: 0.4,
            metalness: 0.3,
          })
        );
        hall.position.set(h.x, h.y, h.z);
        group.add(hall);
      }

      for (const tr of campus.mitigationTrees) {
        const tree = new THREE.Mesh(
          new THREE.ConeGeometry(tr.radius, tr.height, 7),
          new THREE.MeshStandardMaterial({ color: 0x22c55e })
        );
        tree.position.set(tr.x, tr.y, tr.z);
        group.add(tree);
      }

      if (campus.solarCanopy) {
        const sc = campus.solarCanopy;
        const solar = new THREE.Mesh(
          new THREE.PlaneGeometry(sc.width, sc.depth),
          new THREE.MeshStandardMaterial({ color: 0x1d4ed8, side: THREE.DoubleSide })
        );
        solar.rotation.x = -Math.PI / 2 + sc.tiltX;
        solar.position.set(sc.x, sc.y, sc.z);
        group.add(solar);
      }
    }

    // Nearest environmental features projected via latitude-corrected ENU onto terrain
    if (!loading) {
      for (const item of selectedFeatures) {
        const f = item.feature;
        const { x, y, z } = item;
        if (f.kind === 'water' || f.kind === 'wetland') {
          const r = f.kind === 'wetland' ? 1.25 : 0.95;
          const m = new THREE.Mesh(
            new THREE.CylinderGeometry(r, r, 0.2, 24),
            new THREE.MeshStandardMaterial({
              color: f.kind === 'wetland' ? 0x8b5cf6 : 0x38bdf8,
              transparent: true,
              opacity: 0.88,
            })
          );
          m.position.set(x, y + 0.14, z);
          group.add(m);
        } else if (f.kind === 'forest' || f.kind === 'protected') {
          const m = new THREE.Mesh(
            new THREE.ConeGeometry(0.65, 2.0, 7),
            new THREE.MeshStandardMaterial({ color: f.kind === 'protected' ? 0xef4444 : 0x16a34a })
          );
          m.position.set(x, y + 1.0, z);
          group.add(m);
        } else {
          const m = new THREE.Mesh(
            new THREE.BoxGeometry(0.85, 0.85, 0.85),
            new THREE.MeshStandardMaterial({ color: 0xf59e0b })
          );
          m.position.set(x, y + 0.425, z);
          group.add(m);
        }

        if (showLegal && (f.kind === 'wetland' || f.kind === 'protected' || f.kind === 'forest')) {
          const ring = new THREE.Mesh(
            new THREE.RingGeometry(1.4, 1.7, 32),
            new THREE.MeshBasicMaterial({ color: 0xef4444, side: THREE.DoubleSide })
          );
          ring.rotation.x = -Math.PI / 2;
          ring.position.set(x, y + 0.26, z);
          group.add(ring);
        }
      }
    }
  }, [site, radiusKm, activeElevation, selectedFeatures, loading, temporal, scenario, showLegal]);

  const spanKm = (radiusKm * (TERRAIN_SPAN / 2)) / ANALYSIS_RADIUS_UNITS * 2;

  return (
    <div className="space-y-2">
      {terrainParams.isMeasured ? (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs bg-emerald-500/15 border border-emerald-400/30 text-emerald-100 rounded-lg px-3 py-2">
          <span className="font-bold text-emerald-300">Observed-derived DEM Terrain (DSM)</span>
          <span>
            Site elevation: <b>{terrainParams.centerM?.toFixed(1)} m AMSL</b>
          </span>
          <span>
            Range: <b>{terrainParams.minM?.toFixed(1)}–{terrainParams.maxM?.toFixed(1)} m AMSL</b> (Δ{' '}
            {terrainParams.reliefM?.toFixed(1)} m across {spanKm.toFixed(1)}×{spanKm.toFixed(1)} km)
          </span>
          <span>
            Grid: <b>{terrainParams.gridSize}×{terrainParams.gridSize}</b> (~90 m DEM)
          </span>
          <span>
            Class: <b>{activeElevation?.data_class || 'observation_derived'}</b> — Copernicus GLO-90 is a{' '}
            <b>digital surface model</b> (includes canopy and structures; not bare-earth terrain)
          </span>
          <span>
            Vertical exaggeration: <b>{terrainParams.verticalExaggeration}×</b>
          </span>
        </div>
      ) : (
        <div className="text-xs bg-amber-500/15 border border-amber-400/40 text-amber-100 rounded-lg px-3 py-2">
          <b className="text-amber-300">FALLBACK REFERENCE PLANE (Elevation UNAVAILABLE):</b>{' '}
          Real DEM elevation could not be loaded
          {activeElevation?.reason ? ` (${activeElevation.reason})` : ''}. Displaying a flat 0 m unmeasured reference grid —{' '}
          <b>not measured terrain</b>. Site elevation: <b>UNAVAILABLE</b> • Range: <b>UNAVAILABLE m AMSL</b> • Vertical exaggeration: <b>n/a</b>.
        </div>
      )}

      <div ref={ref} style={{ height: 460, borderRadius: 16, overflow: 'hidden' }} />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <button className="btn" onClick={() => resetRef.current()}>
          Reset camera
        </button>
        <span className="text-xs opacity-80">
          Site: {site.name} ({site.lat}, {site.lon}) • Green ring = {radiusKm} km analysis radius • Showing{' '}
          {selectedFeatures.length} of {safeFeatures.length} nearest feature(s)
        </span>
      </div>

      <div className="text-xs opacity-75 space-y-1">
        <p>
          Features: {dataLabel || 'same site analysis features'} (ENU latitude-corrected projection). Drag rotate • scroll zoom • right-drag pan.
        </p>
        {terrainParams.isMeasured ? (
          <p>
            Terrain attribution:{' '}
            <a
              className="underline text-emerald-300"
              href={activeElevation?.source_url || 'https://open-meteo.com/en/docs/elevation-api'}
              target="_blank"
              rel="noreferrer"
            >
              Elevation data by Open-Meteo.com
            </a>{' '}
            (CC BY 4.0, non-commercial free tier) •{' '}
            <a
              className="underline text-emerald-300"
              href={activeElevation?.copernicus_doi_url || 'https://doi.org/10.5270/ESA-c5d3d65'}
              target="_blank"
              rel="noreferrer"
            >
              Copernicus DEM 2021 release GLO-90
            </a>{' '}
            (90 m, © DLR e.V. / Airbus Defence and Space GmbH, funded by the European Union; DOI: 10.5270/ESA-c5d3d65)
            {activeElevation?.cache_hit === 'browser_local_storage' ? ' • Restored from browser cache' : ''}.
          </p>
        ) : (
          <p>
            Terrain provenance: Synthetic flat reference grid (0 m relative). Real Copernicus DEM GLO-90 / Open-Meteo data unavailable for this request.
          </p>
        )}
      </div>
    </div>
  );
}
