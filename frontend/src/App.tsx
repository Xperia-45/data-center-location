import { useEffect, useRef, useState } from 'react';
import { api, Site, Finding } from './lib/api';
import MapView from './components/MapView';
import Timescape from './components/Timescape';
import { SITE_PRESETS, PresetKey } from './lib/timescapeMath';
import { syncElevationWithClientCache } from './lib/elevationCache';
import {
  ActiveSiteWorkspaceState,
  attachCreatedSite,
  beginCreateAndAnalyze,
  beginSiteLoad,
  completeSiteLoad,
  createInitialWorkspaceState,
  failSiteLoad,
  getValidatedFeaturesForSite,
  isTimescapeReady,
  onFormCoordinatesChanged,
} from './lib/siteState';
import {
  MapPin,
  Scale,
  Leaf,
  Box,
  GitCompare,
  Database,
  LayoutDashboard,
  FlaskConical,
  AlertTriangle,
  ExternalLink,
  Search,
} from 'lucide-react';

const NAV = [
  ['overview', 'Overview', LayoutDashboard],
  ['site', 'Site Intelligence', MapPin],
  ['env', 'Environmental Assessment', Leaf],
  ['legal', 'Legal Intelligence', Scale],
  ['scenario', 'Scenario Simulation', FlaskConical],
  ['timescape', 'Environmental Timescape', Box],
  ['compare', 'Site Comparison', GitCompare],
  ['sources', 'Data Sources', Database],
] as const;
type View = (typeof NAV)[number][0];

const riskColor = (r: string) =>
  r === 'Critical' || r === 'High'
    ? 'bg-red-500/20 text-red-300'
    : r === 'Moderate'
    ? 'bg-amber-500/20 text-amber-300'
    : r === 'UNKNOWN'
    ? 'bg-slate-500/20 text-slate-300'
    : 'bg-emerald-500/20 text-emerald-300';

const presets: any = {
  baseline: { preset: 'baseline', footprint_ha: 10, water_mld: 1, power_mw: 20, cooling: 'air', mitigation: [] },
  'high-growth': { preset: 'high-growth', footprint_ha: 25, water_mld: 3.5, power_mw: 60, cooling: 'water', mitigation: [] },
  mitigation: {
    preset: 'mitigation',
    footprint_ha: 10,
    water_mld: 0.4,
    power_mw: 22,
    cooling: 'hybrid-dry',
    mitigation: ['recycled water', 'dry cooling', 'green buffer', 'solar'],
  },
};

export default function App() {
  const [view, setView] = useState<View>('overview');
  const [sites, setSites] = useState<Site[]>([]);
  const [form, setForm] = useState<Site>({
    name: 'Ludhiana, Punjab candidate',
    lat: SITE_PRESETS.ludhiana.lat,
    lon: SITE_PRESETS.ludhiana.lon,
    radius_km: 5,
    footprint_ha: 10,
    water_mld: 1,
    power_mw: 20,
    cooling: 'air',
    built_up_m2: 30000,
  });
  const [workspace, setWorkspace] = useState<ActiveSiteWorkspaceState>(createInitialWorkspaceState);
  const requestIdRef = useRef<number>(0);

  const [scenarioName, setScenarioName] = useState('baseline');
  const [temporal, setTemporal] = useState('present');
  const [showLegal, setShowLegal] = useState(true);
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [laws, setLaws] = useState<any[]>([]);
  const [sources, setSources] = useState<any[]>([]);
  const [legalQuery, setLegalQuery] = useState('');
  const [searchResults, setSearchResults] = useState<any[]>([]);

  const refresh = () => api.listSites().then(setSites).catch(() => {});
  useEffect(() => {
    refresh();
    api.legislation().then(setLaws).catch(() => {});
    api.sources().then(setSources).catch(() => {});
  }, []);

  function updateForm(patch: Partial<Site>) {
    const nextForm = { ...form, ...patch };
    setForm(nextForm);
    if ('lat' in patch || 'lon' in patch || 'radius_km' in patch) {
      setWorkspace((prev) => {
        const next = onFormCoordinatesChanged(prev, nextForm);
        requestIdRef.current = next.requestId;
        if (next !== prev) setBusy('');
        return next;
      });
    }
  }

  function applyPreset(key: PresetKey) {
    const p = SITE_PRESETS[key];
    updateForm({ name: p.name, lat: p.lat, lon: p.lon });
  }

  async function createAndAnalyze(s: Site = form, navigateToEnv: boolean = true) {
    let reqId = 0;
    setWorkspace((prev) => {
      const next = beginCreateAndAnalyze(prev);
      reqId = next.requestId;
      requestIdRef.current = reqId;
      return next;
    });
    setBusy('Analysing… (OSM live query may take ~15s)');
    setErr('');
    try {
      const created = await api.createSite(s);
      if (requestIdRef.current !== reqId) return;
      setWorkspace((prev) => attachCreatedSite(prev, reqId, created));
      const a = await api.analyze(created.id!);
      if (requestIdRef.current !== reqId) return;
      const hydratedAnalysis = { ...a, elevation: syncElevationWithClientCache(created, a.elevation) };
      const [l, sc] = await Promise.all([
        api.legal(created.id!),
        api.scenario(created.id!, presets[scenarioName]),
      ]);
      if (requestIdRef.current !== reqId) return;
      setWorkspace((prev) => completeSiteLoad(prev, reqId, created, hydratedAnalysis, l.results, sc));
      refresh();
      if (navigateToEnv) setView('env');
    } catch (e: any) {
      if (requestIdRef.current !== reqId) return;
      setWorkspace((prev) => failSiteLoad(prev, reqId));
      setErr('Backend unavailable or analysis failed: ' + e.message);
    } finally {
      if (requestIdRef.current === reqId) setBusy('');
    }
  }

  async function loadSite(s: Site) {
    let reqId = 0;
    setForm(s);
    setWorkspace((prev) => {
      const next = beginSiteLoad(prev, s);
      reqId = next.requestId;
      requestIdRef.current = reqId;
      return next;
    });
    setBusy(`Loading analysis for ${s.name}…`);
    setErr('');
    try {
      const [a, l, sc] = await Promise.all([
        api.environment(s.id!),
        api.legal(s.id!),
        api.scenario(s.id!, presets[scenarioName]),
      ]);
      if (requestIdRef.current !== reqId) return;
      const hydratedAnalysis = { ...a, elevation: syncElevationWithClientCache(s, a.elevation) };
      setWorkspace((prev) => completeSiteLoad(prev, reqId, s, hydratedAnalysis, l.results, sc));
    } catch (e: any) {
      if (requestIdRef.current !== reqId) return;
      setWorkspace((prev) => failSiteLoad(prev, reqId));
      setErr(e.message);
    } finally {
      if (requestIdRef.current === reqId) setBusy('');
    }
  }

  async function runScenario(name: string) {
    setScenarioName(name);
    const activeSite = workspace.site;
    if (!activeSite?.id || workspace.loadingAnalysis) return;
    const reqId = requestIdRef.current;
    try {
      const sc = await api.scenario(activeSite.id, presets[name]);
      if (requestIdRef.current !== reqId) return;
      setWorkspace((prev) => (prev.site?.id === activeSite.id ? { ...prev, scenario: sc } : prev));
    } catch (e: any) {
      if (requestIdRef.current === reqId) setErr(e.message);
    }
  }

  const { site, analysis, legal, scenario, loadingAnalysis } = workspace;
  const timescapeReady = isTimescapeReady(workspace);
  const timescapeFeatures = getValidatedFeaturesForSite(workspace);
  const mapFeatures = getValidatedFeaturesForSite(workspace, form);
  const highLegal = legal.filter(
    (l) => l.compliance_status === 'HIGH LEGAL EXPOSURE' || l.compliance_status === 'APPROVAL REQUIRED'
  );

  return (
    <div className="min-h-screen lg:flex">
      <aside className="lg:w-64 p-4 lg:min-h-screen bg-black/30 border-r border-white/10">
        <h1 className="text-xl font-black">🌿 TerraLex</h1>
        <p className="text-xs opacity-70 mt-1">Environmental Intelligence for Data Centres — India</p>
        <p className="text-[11px] mt-2 bg-amber-500/15 text-amber-200 rounded p-2">
          Screening tool only. Not environmental clearance or legal advice.
        </p>
        <nav className="mt-4 space-y-1">
          {NAV.map(([id, label, Icon]) => (
            <button
              key={id}
              onClick={() => setView(id)}
              className={`w-full flex gap-2 items-center px-3 py-2 rounded-lg text-sm ${
                view === id ? 'bg-emerald-500 text-emerald-950 font-bold' : 'hover:bg-white/10'
              }`}
            >
              <Icon size={16} />
              {label}
            </button>
          ))}
        </nav>
        <div className="mt-6 text-xs opacity-70">
          Saved sites: {sites.length}
          <br />
          Laws in DB: {laws.length}
        </div>
      </aside>
      <main className="flex-1 p-4 lg:p-8 max-w-[1400px] mx-auto w-full">
        {busy && <div className="card mb-4 animate-pulse">{busy}</div>}
        {err && (
          <div className="card mb-4 border-red-400/40 text-red-200 flex gap-2">
            <AlertTriangle />
            {err}
          </div>
        )}

        {view === 'overview' && (
          <section className="space-y-4">
            <h2 className="text-3xl font-black">Should you build here — and what could it cost legally?</h2>
            <p className="opacity-80 max-w-3xl">
              Select a location, run a live OpenStreetMap-backed environmental screen, retrieve applicable Indian environmental law from a structured database (never hardcoded in UI), model scenarios, and explore the same site in 3D.
            </p>
            <div className="grid md:grid-cols-4 gap-4">
              {[
                ['Sites analysed', sites.length],
                ['Findings (current)', analysis?.findings?.length || 0],
                ['Approvals / high exposure', highLegal.length],
                ['Legislation in DB', laws.length],
              ].map(([k, v]) => (
                <div className="card" key={k}>
                  <div className="text-3xl font-black">{v}</div>
                  <div className="text-xs opacity-70">{k}</div>
                </div>
              ))}
            </div>
            <div className="card">
              <h3 className="font-bold mb-2">Start / recent analyses</h3>
              {sites.length === 0 ? (
                <p className="text-sm opacity-70">No sites yet — empty state. Create one in Site Intelligence.</p>
              ) : (
                sites.slice(0, 5).map((s) => (
                  <button key={s.id} className="btn2 mr-2 mb-2" onClick={() => loadSite(s)}>
                    {s.name} ({s.lat},{s.lon})
                  </button>
                ))
              )}
              <button className="btn ml-2" onClick={() => setView('site')}>
                + New site
              </button>
            </div>
            {timescapeReady && analysis && (
              <div className="card">
                <h3 className="font-bold">Risk overview — {site?.name}</h3>
                <p className="text-xs mb-2">{analysis.data_label}</p>
                <div className="flex flex-wrap gap-2">
                  {analysis.findings.map((f: Finding, i: number) => (
                    <span key={i} className={`badge ${riskColor(f.risk)}`}>
                      {f.category}: {f.risk}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </section>
        )}

        {view === 'site' && (
          <section className="grid lg:grid-cols-[380px_1fr] gap-4">
            <div className="card space-y-3">
              <h2 className="text-xl font-bold">Site Intelligence</h2>
              <label className="text-xs">
                Name
                <input className="input" value={form.name} onChange={(e) => updateForm({ name: e.target.value })} />
              </label>
              <div className="grid grid-cols-2 gap-2">
                <label className="text-xs">
                  Latitude
                  <input
                    type="number"
                    step="any"
                    className="input"
                    value={form.lat}
                    onChange={(e) => updateForm({ lat: +e.target.value })}
                  />
                </label>
                <label className="text-xs">
                  Longitude
                  <input
                    type="number"
                    step="any"
                    className="input"
                    value={form.lon}
                    onChange={(e) => updateForm({ lon: +e.target.value })}
                  />
                </label>
              </div>
              <p className="text-xs opacity-70">
                Or click the map. Presets:{' '}
                <button className="underline" onClick={() => applyPreset('ludhiana')}>
                  Ludhiana
                </button>{' '}
                ·{' '}
                <button className="underline" onClick={() => applyPreset('mohali')}>
                  Mohali
                </button>{' '}
                ·{' '}
                <button className="underline" onClick={() => applyPreset('hyderabad')}>
                  Hyderabad
                </button>
              </p>
              {[
                ['radius_km', 'Analysis radius (km)'],
                ['footprint_ha', 'Footprint (ha)'],
                ['water_mld', 'Water demand (MLD)'],
                ['power_mw', 'Power (MW)'],
                ['built_up_m2', 'Built-up area (m²) — EIA trigger ≥20,000'],
              ].map(([k, l]) => (
                <label key={k} className="text-xs block">
                  {l}
                  <input
                    type="number"
                    step="any"
                    className="input"
                    value={(form as any)[k]}
                    onChange={(e) => updateForm({ [k]: +e.target.value } as Partial<Site>)}
                  />
                </label>
              ))}
              <label className="text-xs block">
                Cooling
                <select className="input" value={form.cooling} onChange={(e) => updateForm({ cooling: e.target.value })}>
                  <option>air</option>
                  <option>water</option>
                  <option>hybrid-dry</option>
                </select>
              </label>
              <button className="btn w-full" disabled={loadingAnalysis} onClick={() => createAndAnalyze(form, true)}>
                Save site & run assessment →
              </button>
              <div className="pt-2">
                <h4 className="font-bold text-sm">Saved sites</h4>
                {sites.map((s) => (
                  <button
                    key={s.id}
                    className={`btn2 block w-full text-left mt-1 ${site?.id === s.id ? 'border border-emerald-400' : ''}`}
                    onClick={() => loadSite(s)}
                  >
                    {s.name} ({s.lat}, {s.lon})
                  </button>
                ))}
              </div>
            </div>
            <div className="card">
              <MapView
                lat={form.lat}
                lon={form.lon}
                radius={form.radius_km}
                features={mapFeatures}
                onPick={(lat, lon) => updateForm({ lat, lon })}
              />
              <p className="text-xs opacity-70 mt-2">
                Real OpenStreetMap base map. Green circle = analysis radius. Coloured pins after analysis = same OSM features used by risk & Timescape. Click map to move site.
              </p>
            </div>
          </section>
        )}

        {view === 'env' && (
          <section className="space-y-4">
            <h2 className="text-2xl font-bold">Environmental Assessment {site && `— ${site.name}`}</h2>
            {loadingAnalysis ? (
              <p className="card">Loading environmental assessment…</p>
            ) : !timescapeReady || !analysis ? (
              <p className="card">
                No current analysis for ({form.lat}, {form.lon}).{' '}
                <button className="btn ml-2" onClick={() => createAndAnalyze(form, false)}>
                  Save site & run assessment →
                </button>
              </p>
            ) : (
              <>
                <p className="text-sm bg-white/5 rounded p-3">
                  {analysis.data_label} • {analysis.disclaimer}
                </p>
                {analysis.findings.map((f: Finding, i: number) => (
                  <article className="card" key={i}>
                    <div className="flex justify-between flex-wrap gap-2">
                      <h3 className="font-bold">{f.category}</h3>
                      <span className={`badge ${riskColor(f.risk)}`}>{f.risk}</span>
                    </div>
                    <p className="mt-1">{f.finding}</p>
                    <p className="text-sm opacity-80 mt-1">{f.explanation}</p>
                    <div className="grid md:grid-cols-4 gap-2 text-xs mt-3 opacity-80">
                      <span>Source: {f.source}</span>
                      <span>Quality: {f.data_quality}</span>
                      <span>Updated: {f.last_updated}</span>
                      <span>Confidence: {f.confidence}</span>
                    </div>
                    {f.evidence?.length > 0 && (
                      <ul className="text-xs mt-2 list-disc ml-5">
                        {f.evidence.slice(0, 5).map((e: any, j: number) => (
                          <li key={j}>
                            {e.name} — {e.kind}, {e.distance_km} km ({e.source})
                          </li>
                        ))}
                      </ul>
                    )}
                  </article>
                ))}
              </>
            )}
          </section>
        )}

        {view === 'legal' && (
          <section className="space-y-4">
            <h2 className="text-2xl font-bold">
              Legal Intelligence{' '}
              <span className="text-xs badge bg-emerald-500/20 text-emerald-200">
                database-driven — no laws hardcoded in UI
              </span>
            </h2>
            <div className="card flex gap-2">
              <Search />
              <input
                className="input"
                placeholder="Search legal DB e.g. consent, wetland, groundwater"
                value={legalQuery}
                onChange={(e) => setLegalQuery(e.target.value)}
              />
              <button className="btn" onClick={async () => setSearchResults(await api.searchLegal(legalQuery))}>
                Search DB
              </button>
            </div>
            {searchResults.map((r, i) => (
              <div className="card text-sm" key={i}>
                <b>
                  {r.title} — {r.section}
                </b>
                <p className="opacity-80">{r.explanation}</p>
                <a className="text-emerald-300 underline" href={r.source_url} target="_blank">
                  Official source <ExternalLink size={12} className="inline" />
                </a>
              </div>
            ))}
            {loadingAnalysis ? (
              <p className="card">Loading legal applicability for selected site…</p>
            ) : !legal.length ? (
              <p className="card">Run an assessment first to evaluate applicability for the selected site.</p>
            ) : (
              legal.map((l, i) => (
                <article className="card" key={i}>
                  <div className="flex justify-between flex-wrap gap-2">
                    <h3 className="font-bold">{l.law}</h3>
                    <span
                      className={`badge ${riskColor(
                        l.compliance_status === 'HIGH LEGAL EXPOSURE'
                          ? 'Critical'
                          : l.compliance_status === 'APPROVAL REQUIRED'
                          ? 'Moderate'
                          : 'UNKNOWN'
                      )}`}
                    >
                      {l.compliance_status}
                    </span>
                  </div>
                  <p className="text-sm mt-1">
                    <b>{l.section}</b> • Authority: {l.authority}
                  </p>
                  <blockquote className="border-l-4 border-emerald-400 pl-3 text-sm mt-2 bg-black/20 p-2 rounded">
                    {l.original_text}
                  </blockquote>
                  <p className="text-sm mt-2">
                    <b>Plain language (AI/curator explanation, distinct from statute):</b> {l.plain_explanation}
                  </p>
                  <p className="text-sm">
                    <b>Why it may apply:</b> {l.why_applies}
                  </p>
                  <p className="text-sm">
                    <b>Required action:</b> {l.required_action}
                  </p>
                  <p className="text-sm">
                    <b>Potential consequences ({l.consequence_type}):</b> {l.potential_consequences}
                  </p>
                  <p className="text-xs opacity-70 mt-2">
                    Effective: {l.effective_date} • Status: {l.amendment_status} • Last verified: {l.last_verified} •{' '}
                    <a className="underline text-emerald-300" href={l.source_url} target="_blank">
                      Official source
                    </a>
                  </p>
                </article>
              ))
            )}
            <div className="card">
              <h3 className="font-bold">Legislation in database ({laws.length})</h3>
              {laws.map((l) => (
                <p key={l.id} className="text-sm mt-1">
                  • {l.title} — {l.status} (
                  <a className="underline text-emerald-300" href={l.source_url} target="_blank">
                    source
                  </a>
                  , verified {l.verified})
                </p>
              ))}
            </div>
          </section>
        )}

        {view === 'scenario' && (
          <section className="space-y-4">
            <h2 className="text-2xl font-bold">Scenario Simulation</h2>
            <div className="flex gap-2 flex-wrap">
              {Object.keys(presets).map((n) => (
                <button key={n} className={scenarioName === n ? 'btn' : 'btn2'} onClick={() => runScenario(n)}>
                  {n}
                </button>
              ))}
            </div>
            {loadingAnalysis ? (
              <p className="card">Loading scenario simulation…</p>
            ) : scenario && timescapeReady ? (
              <div className="card">
                <p className="badge bg-purple-500/20 text-purple-200">{scenario.label}</p>
                <pre className="text-sm whitespace-pre-wrap mt-3">{JSON.stringify(scenario.outputs, null, 2)}</pre>
                <p className="text-xs opacity-70">
                  Changing scenario also changes Timescape buildings. Re-run Legal after material threshold changes.
                </p>
              </div>
            ) : (
              <p className="card">Analyse the selected site first.</p>
            )}
          </section>
        )}

        {view === 'timescape' && (
          <section className="space-y-4">
            <h2 className="text-2xl font-bold">Environmental Timescape</h2>
            {loadingAnalysis ? (
              <div className="card">
                <p className="font-semibold">
                  Loading environmental analysis for {site?.name || form.name}…
                </p>
                <p className="text-xs opacity-70 mt-1">
                  Previous site features are cleared while the new analysis loads so stale coordinates never appear in 3D.
                </p>
              </div>
            ) : timescapeReady && site && analysis ? (
              <>
                <div className="flex gap-2 flex-wrap items-center">
                  {['present', 'development', 'future'].map((t) => (
                    <button key={t} className={temporal === t ? 'btn' : 'btn2'} onClick={() => setTemporal(t)}>
                      {t}
                    </button>
                  ))}
                  {Object.keys(presets).map((n) => (
                    <button key={n} className={scenarioName === n ? 'btn' : 'btn2'} onClick={() => runScenario(n)}>
                      {n}
                    </button>
                  ))}
                  <label className="text-sm">
                    <input
                      type="checkbox"
                      checked={showLegal}
                      onChange={(e) => setShowLegal(e.target.checked)}
                    />{' '}
                    Legal overlays (red rings = wetland/protected/forest)
                  </label>
                </div>
                <div className="card">
                  <Timescape
                    site={site}
                    features={timescapeFeatures}
                    elevation={analysis.elevation}
                    analysisSiteId={analysis.site_id}
                    dataLabel={analysis.data_label}
                    loading={loadingAnalysis}
                    temporal={temporal}
                    scenario={scenarioName}
                    showLegal={showLegal}
                  />
                </div>
              </>
            ) : (
              <div className="card space-y-2">
                <p>
                  No completed analysis for current candidate <b>{form.name}</b> ({form.lat}, {form.lon}, r={form.radius_km} km).
                </p>
                <div className="flex gap-2 flex-wrap">
                  <button className="btn" onClick={() => createAndAnalyze(form, false)}>
                    Save site & run assessment →
                  </button>
                  <button className="btn2" onClick={() => setView('site')}>
                    Edit in Site Intelligence
                  </button>
                </div>
              </div>
            )}
          </section>
        )}

        {view === 'compare' && (
          <section className="space-y-4">
            <h2 className="text-2xl font-bold">Site Comparison</h2>
            <p className="text-sm opacity-80">
              Transparent side-by-side on recorded inputs — no arbitrary “best site” score. Run saved sites to populate live findings; preference depends on your stated priorities (water, ecology, legal exposure).
            </p>
            <div className="card overflow-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left">
                    <th>Site</th>
                    <th>Coordinates</th>
                    <th>Water MLD</th>
                    <th>Footprint ha</th>
                    <th>Power MW</th>
                    <th>Built-up m²</th>
                    <th>EIA screen</th>
                  </tr>
                </thead>
                <tbody>
                  {sites.map((s) => (
                    <tr key={s.id} className="border-t border-white/10">
                      <td>{s.name}</td>
                      <td>
                        {s.lat}, {s.lon}
                      </td>
                      <td>{s.water_mld}</td>
                      <td>{s.footprint_ha}</td>
                      <td>{s.power_mw}</td>
                      <td>{s.built_up_m2}</td>
                      <td>{s.built_up_m2 >= 20000 ? 'APPROVAL REQUIRED' : 'Below 20k screen*'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {sites.length < 2 && (
                <p className="text-xs opacity-70 mt-2">
                  Save at least 2 sites in Site Intelligence to compare. *Verify current EIA Schedule.
                </p>
              )}
            </div>
          </section>
        )}

        {view === 'sources' && (
          <section className="space-y-4">
            <h2 className="text-2xl font-bold">Data Sources & Provenance</h2>
            {sources.map((s, i) => (
              <div className="card" key={i}>
                <h3 className="font-bold">
                  {s.provider} — {s.dataset}
                </h3>
                <p className="text-sm opacity-80">
                  License: {s.license} • Quality: {s.quality} • Status: {s.status}
                </p>
                <a className="text-emerald-300 underline text-sm" href={s.url} target="_blank">
                  {s.url}
                </a>
              </div>
            ))}
            <div className="card text-sm">
              <h3 className="font-bold">Labelling policy</h3>
              <p>
                Verified real data / Imported official data / Third-party data / Synthetic demonstration / Modelled scenario are labelled in-line. Missing data is shown as UNAVAILABLE — never fabricated.
              </p>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
