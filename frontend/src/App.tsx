import ChronosView from "./ChronosView";
import { useEffect, useMemo, useState } from "react";
import { Activity, AlertTriangle, ArrowDownUp, BarChart3, Clock3, Info, Map, RefreshCw } from "lucide-react";
import { CircleMarker, MapContainer, Polyline, Popup, TileLayer } from "react-leaflet";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

const API_BASE = (import.meta.env.VITE_ANOMALY_API_BASE_URL ?? "").replace(/\/$/, "");

type View = "chronos" | "overview" | "timeline" | "intersections" | "interruptions" | "moe";
type Direction = "All" | "Eastbound" | "Westbound";
type MoeGrain = "15min" | "hourly";

type Entity = {
  entity_id: string;
  signal_id: string;
  name: string;
  phase: number;
  direction: Exclude<Direction, "All">;
  metric: string;
};

type IntersectionOption = {
  signal_id: string;
  name: string;
};

type MetaResponse = {
  metadata: Record<string, string>;
  entities: Entity[];
  intersections: IntersectionOption[];
  directions: string[];
};

type IntersectionStatus = {
  signal_id: string;
  name: string;
  lat: number;
  lon: number;
  route: string;
  modeled_points: number;
  anomaly_count: number;
  max_score: number | null;
  latest_anomaly: string | null;
  recent_events: Array<{
    event_time: string;
    event_direction: string;
    geh: number;
  }>;
  severity: string;
};

type OverviewResponse = {
  filters: { dateFrom: string; dateTo: string; direction: Direction };
  summary: {
    modeled_points: number;
    modeled_intersections: number;
    anomaly_count: number;
    max_score: number | null;
  };
  intersections: IntersectionStatus[];
};

type Anomaly = {
  entity_id: string;
  signal_id: string;
  name: string;
  phase: number;
  direction: string;
  timestamp: string;
  value: number;
  prediction: number;
  deviation: number;
  deviation_pct: number | null;
  geh_score: number;
  abs_score: number;
  entity_anomaly: boolean;
  severity: string;
};

type TimelineResponse = { entity: Anomaly; points: Anomaly[] };

type Changepoint = {
  entity_id: string;
  signal_id: string;
  name: string;
  phase: number;
  direction: string;
  timestamp: string;
  score: number;
  avg_before: number;
  avg_after: number;
  avg_diff: number;
  pct_change: number;
};

type MoeMetric = {
  metric: string;
  label: string;
  unit: string;
  hourly_rollup: string;
};

type MoeMetaResponse = {
  metadata: Record<string, string>;
  summary: Array<{
    grain: MoeGrain;
    row_count: number;
    signal_count: number;
    metric_count: number;
    date_from: string;
    date_to: string;
  }>;
  metrics: MoeMetric[];
  intersections: Array<IntersectionOption & { lat: number; lon: number; route: string }>;
  dimensions: {
    grains: MoeGrain[];
    directions: string[];
    phases: number[];
  };
};

type MoeObservation = {
  timestamp: string;
  date: string;
  hour: number;
  minute: number;
  grain: MoeGrain;
  signal_id: string;
  phase: number | null;
  direction: string;
  source_table: string;
  metric: string;
  value: number;
  sample_count: number;
};

type InterruptionMetaResponse = {
  metadata: Record<string, string>;
  dateFrom: string;
  dateTo: string;
  pointCount: number;
  signalCount: number;
  intersections: Array<IntersectionOption & { lat: number; lon: number; route: string }>;
  dimensions: { directions: string[]; phases: number[] };
  bucketMinutes: number;
  availableBuckets: number[];
};

type InterruptionEvent = {
  event_id: string;
  signal_id: string;
  name: string;
  phase: number;
  direction: string;
  start_time: string;
  end_time: string;
  duration_minutes: number;
  point_count: number;
  raw_observed_volume: number;
  observed_volume: number;
  expected_volume: number;
  volume_delta: number;
  avg_reduction_pct: number;
  max_reduction_pct: number;
  min_history_days: number;
  severity: string;
};

type InterruptionPoint = {
  timestamp: string;
  raw_volume: number;
  observed_volume: number;
  expected_volume: number;
  reduction_pct: number;
  history_days: number;
  is_drop: boolean;
  is_event: boolean;
};

type InterruptionEventsResponse = {
  summary: {
    event_count: number;
    affected_intersections: number;
    longest_duration_minutes: number | null;
    largest_reduction_pct: number | null;
  };
  items: InterruptionEvent[];
  count: number;
};

type InterruptionTimelineResponse = {
  event: InterruptionEvent;
  points: InterruptionPoint[];
};

const severityColors: Record<string, string> = {
  Normal: "#27735a",
  Watch: "#d3a11f",
  High: "#d56b2d",
  Critical: "#b5343f",
  "No data": "#8b9590",
};

const shortName = (name: string) => name.replace("SR 758 (Bee Ridge Rd) & ", "");
const formatDateTime = (value: string | null) => {
  if (!value) return "None";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? "None"
    : new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "numeric" }).format(parsed);
};
const formatNumber = (value: number | null | undefined, digits = 0) =>
  typeof value === "number" ? value.toLocaleString(undefined, { maximumFractionDigits: digits }) : "-";
const formatDuration = (minutes: number | null | undefined) => {
  if (typeof minutes !== "number") return "-";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
};

async function getJson<T>(path: string, params?: URLSearchParams): Promise<T> {
  const response = await fetch(`${API_BASE}${path}${params ? `?${params}` : ""}`);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail ?? `Request failed with ${response.status}`);
  }
  return response.json();
}

function LoadingBand() {
  return <div className="loading-band"><RefreshCw size={17} className="spin" /> Loading model results</div>;
}

export function App() {
  const [view, setView] = useState<View>("overview");
  const [meta, setMeta] = useState<MetaResponse | null>(null);
  const [moeMeta, setMoeMeta] = useState<MoeMetaResponse | null>(null);
  const [interruptionMeta, setInterruptionMeta] = useState<InterruptionMetaResponse | null>(null);
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [changepoints, setChangepoints] = useState<Changepoint[]>([]);
  const [moeRows, setMoeRows] = useState<MoeObservation[]>([]);
  const [interruptionEvents, setInterruptionEvents] = useState<InterruptionEvent[]>([]);
  const [interruptionSummary, setInterruptionSummary] = useState<InterruptionEventsResponse["summary"] | null>(null);
  const [interruptionTimeline, setInterruptionTimeline] = useState<InterruptionTimelineResponse | null>(null);
  const [direction, setDirection] = useState<Direction>("All");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [entityId, setEntityId] = useState("");
  const [gehThreshold, setGehThreshold] = useState("3.5");
  const [selectedSignalIds, setSelectedSignalIds] = useState<string[]>([]);
  const [moeMetric, setMoeMetric] = useState("volume");
  const [moeGrain, setMoeGrain] = useState<MoeGrain>("15min");
  const [moeSignalId, setMoeSignalId] = useState("");
  const [moePhase, setMoePhase] = useState("2");
  const [interruptionPhase, setInterruptionPhase] = useState("all");
  const [interruptionBucketMinutes, setInterruptionBucketMinutes] = useState("15");
  const [minInterruptionDuration, setMinInterruptionDuration] = useState("30");
  const [minInterruptionReduction, setMinInterruptionReduction] = useState("30");
  const [selectedInterruptionId, setSelectedInterruptionId] = useState("");
  const [loading, setLoading] = useState(true);
  const [moeLoading, setMoeLoading] = useState(false);
  const [interruptionLoading, setInterruptionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getJson<MetaResponse>("/api/meta")
      .then((payload) => {
        setMeta(payload);
        const end = payload.metadata.date_to.slice(0, 10);
        const startDate = new Date(payload.metadata.date_to);
        startDate.setDate(startDate.getDate() - 14);
        setDateFrom(startDate.toISOString().slice(0, 10));
        setDateTo(end);
        setGehThreshold(payload.metadata.entity_threshold ?? "3.5");
        setSelectedSignalIds(payload.intersections.map((intersection) => intersection.signal_id));
        setEntityId(payload.entities[0]?.entity_id ?? "");
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    getJson<MoeMetaResponse>("/api/moe/meta")
      .then((payload) => {
        setMoeMeta(payload);
        setMoeMetric(payload.metrics.find((metric) => metric.metric === "volume")?.metric ?? payload.metrics[0]?.metric ?? "volume");
        setMoeSignalId(payload.intersections[0]?.signal_id ?? "");
        setMoePhase(String(payload.dimensions.phases?.includes(2) ? 2 : payload.dimensions.phases?.[0] ?? ""));
      })
      .catch((reason) => setError(reason.message));
  }, []);

  useEffect(() => {
    getJson<InterruptionMetaResponse>("/api/interruptions/meta", new URLSearchParams({ bucketMinutes: interruptionBucketMinutes }))
      .then(setInterruptionMeta)
      .catch((reason) => setError(reason.message));
  }, [interruptionBucketMinutes]);

  useEffect(() => {
    if (!dateFrom || !dateTo || !meta) return;
    const threshold = Math.max(0.1, Number(gehThreshold) || 3.5);
    const intersectionIds = selectedSignalIds.length ? selectedSignalIds.join(",") : "__none__";
    const params = new URLSearchParams({ dateFrom, dateTo, direction, gehThreshold: String(threshold), intersectionIds });
    setLoading(true);
    setError(null);
    Promise.all([
      getJson<OverviewResponse>("/api/overview", params),
      getJson<{ items: Anomaly[] }>("/api/anomalies", new URLSearchParams({ ...Object.fromEntries(params), limit: "120" })),
      getJson<{ items: Changepoint[] }>("/api/changepoints", new URLSearchParams({ direction, intersectionIds })),
    ])
      .then(([overviewPayload, anomalyPayload, changepointPayload]) => {
        setOverview(overviewPayload);
        setAnomalies(anomalyPayload.items);
        setChangepoints(changepointPayload.items);
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setLoading(false));
  }, [dateFrom, dateTo, direction, gehThreshold, meta, selectedSignalIds]);

  useEffect(() => {
    if (!entityId || !dateFrom || !dateTo) {
      setTimeline(null);
      return;
    }
    const threshold = Math.max(0.1, Number(gehThreshold) || 3.5);
    getJson<TimelineResponse>(`/api/timeline/${entityId}`, new URLSearchParams({ dateFrom, dateTo, gehThreshold: String(threshold) }))
      .then(setTimeline)
      .catch((reason) => setError(reason.message));
  }, [entityId, dateFrom, dateTo, gehThreshold]);

  useEffect(() => {
    if (!moeMeta || !moeMetric || !moeSignalId || !dateFrom || !dateTo) return;
    const params = new URLSearchParams({
      metric: moeMetric,
      grain: moeGrain,
      dateFrom,
      dateTo,
      signalId: moeSignalId,
      limit: "2500",
    });
    if (direction !== "All") params.set("direction", direction);
    if (moePhase !== "all") params.set("phase", moePhase);
    setMoeLoading(true);
    getJson<{ items: MoeObservation[]; count: number }>("/api/moe/observations", params)
      .then((payload) => setMoeRows(payload.items))
      .catch((reason) => setError(reason.message))
      .finally(() => setMoeLoading(false));
  }, [moeMeta, moeMetric, moeGrain, moeSignalId, moePhase, dateFrom, dateTo, direction]);

  useEffect(() => {
    if (!interruptionMeta || !dateFrom || !dateTo) return;
    const params = new URLSearchParams({
      dateFrom,
      dateTo,
      direction,
      bucketMinutes: interruptionBucketMinutes,
      minDuration: String(Math.max(5, Number(minInterruptionDuration) || 30)),
      minReduction: String(Math.max(0, Number(minInterruptionReduction) || 30)),
      intersectionIds: selectedSignalIds.length ? selectedSignalIds.join(",") : "__none__",
      limit: "300",
    });
    if (interruptionPhase !== "all") params.set("phase", interruptionPhase);
    setInterruptionLoading(true);
    getJson<InterruptionEventsResponse>("/api/interruptions/events", params)
      .then((payload) => {
        setInterruptionEvents(payload.items);
        setInterruptionSummary(payload.summary);
        setSelectedInterruptionId((current) =>
          payload.items.some((item) => item.event_id === current) ? current : payload.items[0]?.event_id ?? ""
        );
      })
      .catch((reason) => setError(reason.message))
      .finally(() => setInterruptionLoading(false));
  }, [interruptionMeta, interruptionBucketMinutes, dateFrom, dateTo, direction, interruptionPhase, minInterruptionDuration, minInterruptionReduction, selectedSignalIds]);

  useEffect(() => {
    if (!selectedInterruptionId) {
      setInterruptionTimeline(null);
      return;
    }
    getJson<InterruptionTimelineResponse>(`/api/interruptions/timeline/${selectedInterruptionId}`, new URLSearchParams({ bucketMinutes: interruptionBucketMinutes }))
      .then(setInterruptionTimeline)
      .catch((reason) => setError(reason.message));
  }, [selectedInterruptionId, interruptionBucketMinutes]);

  const entities = useMemo(
    () => (meta?.entities ?? []).filter(
      (entity) => selectedSignalIds.includes(entity.signal_id) && (direction === "All" || entity.direction === direction)
    ),
    [meta, direction, selectedSignalIds]
  );

  useEffect(() => {
    if (entities.length && !entities.some((entity) => entity.entity_id === entityId)) {
      setEntityId(entities[0].entity_id);
    } else if (!entities.length && entityId) {
      setEntityId("");
    }
  }, [entities, entityId]);

  const corridorPath = useMemo(
    () => (overview?.intersections ?? []).map((intersection) => [intersection.lat, intersection.lon] as [number, number]),
    [overview]
  );
  const selectedEntity = meta?.entities.find((entity) => entity.entity_id === entityId);
  const selectedMoeMetric = moeMeta?.metrics.find((metric) => metric.metric === moeMetric);
  const moeSummary = moeMeta?.summary.find((item) => item.grain === moeGrain);
  const timelineChangepoints = useMemo(() => {
    const start = new Date(`${dateFrom}T00:00:00`).getTime();
    const end = new Date(`${dateTo}T23:59:59`).getTime();
    return changepoints.filter((item) => {
      const timestamp = new Date(item.timestamp).getTime();
      return item.entity_id === entityId && timestamp >= start && timestamp <= end;
    });
  }, [changepoints, dateFrom, dateTo, entityId]);
  const changepointRows = useMemo(
    () => [...changepoints].sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()),
    [changepoints]
  );

  const focusChangepoint = (item: Changepoint) => {
    const center = new Date(item.timestamp);
    const start = new Date(center);
    const end = new Date(center);
    start.setDate(start.getDate() - 7);
    end.setDate(end.getDate() + 7);
    setEntityId(item.entity_id);
    setDateFrom(start.toISOString().slice(0, 10));
    setDateTo(end.toISOString().slice(0, 10));
    setView("timeline");
  };
  const tabs: Array<{ id: View; label: string; icon: typeof Activity }> = [
    { id: "overview", label: "Corridor", icon: Map },
    { id: "timeline", label: "Anomaly and Changepoints", icon: Activity },
    { id: "intersections", label: "Intersections Ranking", icon: ArrowDownUp },
    { id: "interruptions", label: "Interruptions / EOI", icon: Clock3 },
    { id: "moe", label: "MOE Explorer", icon: BarChart3 },
    { id: "chronos", label: "Chronos · Experimental", icon: Activity },
  ];

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-block">
          <span className="brand-mark"><Activity size={21} /></span>
          <div><strong>Bee Ridge Anomaly Detector</strong><span>Hourly anomalies · 15-minute operational analytics</span></div>
        </div>
      </header>

      <nav className="view-tabs" aria-label="Anomaly views">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          return <button key={tab.id} className={view === tab.id ? "active" : ""} onClick={() => setView(tab.id)}><Icon size={17} />{tab.label}</button>;
        })}
      </nav>

      {view !== "chronos" && <section className="filter-bar">
        <label><span>Date from</span><input type="date" value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} /></label>
        <label><span>Date to</span><input type="date" value={dateTo} onChange={(event) => setDateTo(event.target.value)} /></label>
        <label><span>Direction</span><select value={direction} onChange={(event) => setDirection(event.target.value as Direction)}><option>All</option><option>Eastbound</option><option>Westbound</option></select></label>
        <label><span>GEH threshold</span><input className="threshold-input" type="number" min="0.1" max="50" step="0.1" value={gehThreshold} onChange={(event) => setGehThreshold(event.target.value)} /></label>
        <details className="intersection-filter">
          <summary><span>Intersections</span><strong>{selectedSignalIds.length} of {meta?.intersections.length ?? 14}</strong></summary>
          <div className="intersection-menu">
            <div className="intersection-actions"><button type="button" onClick={() => setSelectedSignalIds(meta?.intersections.map((item) => item.signal_id) ?? [])}>Select all</button><button type="button" onClick={() => setSelectedSignalIds([])}>Clear</button></div>
            <div className="intersection-options">{meta?.intersections.map((intersection) => <label key={intersection.signal_id}><input type="checkbox" checked={selectedSignalIds.includes(intersection.signal_id)} onChange={(event) => setSelectedSignalIds((current) => event.target.checked ? [...current, intersection.signal_id] : current.filter((signalId) => signalId !== intersection.signal_id))} /><span><strong>{intersection.signal_id}</strong>{shortName(intersection.name)}</span></label>)}</div>
          </div>
        </details>
        <div className="filter-meta"><span>Baseline</span><strong>{meta?.metadata.rolling_window_days ?? 42} days</strong></div>
      </section>}

      {view !== "chronos" && error ? <div className="error-band"><AlertTriangle size={18} />{error}</div> : null}
      {view !== "chronos" && loading ? <LoadingBand /> : null}

      <main>
        {view === "chronos" && <ChronosView />}
        {view === "overview" ? (
          <>
            <section className="kpi-strip">
              <div><span>Modeled intersections</span><strong>{overview?.summary.modeled_intersections ?? 0}<small> / {selectedSignalIds.length}</small></strong></div>
              <div><span>Anomaly events</span><strong>{formatNumber(overview?.summary.anomaly_count)}</strong></div>
              <div><span className="metric-label">Highest GEH <span className="info-tooltip" tabIndex={0} aria-label="About GEH"><Info size={13} /><span role="tooltip">GEH compares observed traffic volume with expected volume. Higher values indicate a larger, more unusual difference.</span></span></span><strong>{formatNumber(overview?.summary.max_score, 1)}</strong></div>
            </section>
            <section className="overview-grid">
              <div className="map-region">
                <div className="section-title"><div><span>Corridor status</span><h1>Bee Ridge Road</h1></div><div className="legend"><i className="normal" />Normal<i className="watch" />Watch<i className="high" />High<i className="critical" />Critical</div></div>
                {corridorPath.length ? <MapContainer center={[27.2989, -82.48]} zoom={13} scrollWheelZoom={false} className="corridor-map">
                  <TileLayer attribution='&copy; OpenStreetMap contributors' url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />
                  <Polyline positions={corridorPath} pathOptions={{ color: "#244f44", weight: 4, opacity: 0.78 }} />
                  {overview?.intersections.map((intersection) => <CircleMarker key={intersection.signal_id} center={[intersection.lat, intersection.lon]} radius={intersection.severity === "No data" ? 7 : 9} pathOptions={{ color: "#fff", weight: 3, fillColor: severityColors[intersection.severity], fillOpacity: 1 }} eventHandlers={{ click: () => { const entity = meta?.entities.find((item) => item.signal_id === intersection.signal_id && (direction === "All" || item.direction === direction)); if (entity) { setEntityId(entity.entity_id); setView("timeline"); } } }}><Popup><strong>{shortName(intersection.name)}</strong><br />Signal {intersection.signal_id}<br />{intersection.anomaly_count} anomalies</Popup></CircleMarker>)}
                </MapContainer> : null}
              </div>
              <div className="event-region">
                <div className="section-title compact"><div><span>Highest departures</span><h2>Top anomaly events</h2></div></div>
                <EventTable items={anomalies.slice(0, 12)} onOpen={(item) => { setEntityId(item.entity_id); setView("timeline"); }} />
              </div>
            </section>
          </>
        ) : null}

        {view === "timeline" ? (
          <section className="workspace-view">
            <div className="section-title"><div><span>Observed vs expected</span><h1>{selectedEntity ? shortName(selectedEntity.name) : "Intersection timeline"}</h1></div><label className="entity-select"><span>Intersection</span><select value={entityId} onChange={(event) => setEntityId(event.target.value)}>{entities.map((entity) => <option key={entity.entity_id} value={entity.entity_id}>{entity.signal_id} · {shortName(entity.name)} · {entity.direction}</option>)}</select></label></div>
            <div className="timeline-summary"><span>Signal {selectedEntity?.signal_id}</span><span>Phase {selectedEntity?.phase}</span><span>{selectedEntity?.direction}</span><span>GEH threshold {Math.max(0.1, Number(gehThreshold) || 3.5)}</span><span className="changepoint-key"><i />{timelineChangepoints.length} in chart · {changepointRows.length} detected</span></div>
            <div className="chart-wrap"><ResponsiveContainer width="100%" height="100%"><LineChart data={timeline?.points ?? []} margin={{ top: 18, right: 24, left: 0, bottom: 8 }}><CartesianGrid stroke="#dfe5e2" vertical={false} /><XAxis dataKey="timestamp" tickFormatter={(value) => new Date(value).toLocaleDateString("en-US", { month: "short", day: "numeric" })} minTickGap={42} /><YAxis /><Tooltip labelFormatter={(value) => formatDateTime(String(value))} formatter={(value) => formatNumber(Number(value), 1)} /><Legend /><Line type="monotone" dataKey="value" name="Observed volume" stroke="#1d5f4b" strokeWidth={2} dot={false} /><Line type="monotone" dataKey="prediction" name="Expected volume" stroke="#547ab7" strokeWidth={2} strokeDasharray="6 4" dot={false} />{timelineChangepoints.map((item) => <ReferenceLine key={`${item.entity_id}-${item.timestamp}`} x={item.timestamp} stroke="#bd2f3b" strokeWidth={2} strokeDasharray="7 5" label={{ value: "Change", position: "insideTopRight", fill: "#9f2631", fontSize: 10 }} />)}{(timeline?.points ?? []).filter((point) => point.entity_anomaly).map((point) => <ReferenceDot key={point.timestamp} x={point.timestamp} y={point.value} r={4} fill={severityColors[point.severity]} stroke="#fff" />)}</LineChart></ResponsiveContainer></div>
            <div className="section-title compact"><div><span>Selected entity</span><h2>Anomaly events</h2></div></div>
            <EventTable items={(timeline?.points ?? []).filter((point) => point.entity_anomaly).sort((a, b) => b.abs_score - a.abs_score).slice(0, 40)} />
            <div className="section-title compact merged-section-title"><div><span>Persistent pattern shifts across selected intersections</span><h2>Changepoints</h2></div><div className="count-badge">{changepointRows.length} shifts</div></div>
            <div className="data-table changepoint-table"><div className="table-head"><span>Intersection</span><span>Direction</span><span>Detected</span><span>Before</span><span>After</span><span>Difference</span><span>Change</span></div>{changepointRows.length ? changepointRows.map((item) => <button className="table-row" key={`${item.entity_id}-${item.timestamp}`} onClick={() => focusChangepoint(item)}><strong>{shortName(item.name)}</strong><span>{item.direction}</span><span>{formatDateTime(item.timestamp)}</span><span>{formatNumber(item.avg_before, 1)}</span><span>{formatNumber(item.avg_after, 1)}</span><span className={item.avg_diff >= 0 ? "up" : "down"}>{item.avg_diff >= 0 ? "+" : ""}{formatNumber(item.avg_diff, 1)}</span><span className={item.pct_change >= 0 ? "up" : "down"}>{item.pct_change >= 0 ? "+" : ""}{formatNumber(item.pct_change * 100, 1)}%</span></button>) : <div className="empty-state">No changepoints were detected for the selected intersections and direction.</div>}</div>
          </section>
        ) : null}

        {view === "intersections" ? (
          <section className="workspace-view">
            <div className="section-title"><div><span>Corridor comparison</span><h1>Intersection anomaly ranking</h1></div></div>
            <div className="data-table intersection-table"><div className="table-head"><span>Intersection</span><span>Signal</span><span>Anomalies</span><span>Last 10 events</span></div>{[...(overview?.intersections ?? [])].sort((a, b) => b.anomaly_count - a.anomaly_count).map((item) => <button className="table-row" key={item.signal_id} onClick={() => { const entity = meta?.entities.find((candidate) => candidate.signal_id === item.signal_id && (direction === "All" || candidate.direction === direction)); if (entity) { setEntityId(entity.entity_id); setView("timeline"); } }}><strong>{shortName(item.name)}</strong><span>{item.signal_id}</span><span>{item.anomaly_count}</span><span className="recent-events">{item.recent_events.length ? item.recent_events.map((event) => <span className="recent-event" key={`${event.event_time}-${event.event_direction}`}><time>{formatDateTime(event.event_time)}</time><small>{event.event_direction === "Eastbound" ? "EB" : "WB"}</small><strong>GEH {formatNumber(event.geh, 1)}</strong></span>) : <span className="no-events">No events</span>}</span></button>)}</div>
          </section>
        ) : null}

        {view === "interruptions" ? (
          <section className="workspace-view interruption-view">
            <div className="section-title">
              <div><span>Sustained low-volume periods</span><h1>Interruptions / Events of Interest</h1></div>
              <div className="count-badge">{interruptionBucketMinutes}-minute volume · {formatNumber(interruptionMeta?.pointCount)} scored points</div>
            </div>
            <div className="interruption-control-grid">
              <label><span>Bucket size</span><select value={interruptionBucketMinutes} onChange={(event) => { setSelectedInterruptionId(""); setInterruptionTimeline(null); setInterruptionBucketMinutes(event.target.value); }}>{(interruptionMeta?.availableBuckets ?? [15]).map((minutes) => <option key={minutes} value={minutes}>{minutes} minutes</option>)}</select></label>
              <label><span>Phase</span><select value={interruptionPhase} onChange={(event) => setInterruptionPhase(event.target.value)}><option value="all">All phases</option>{(interruptionMeta?.dimensions.phases ?? []).map((phase) => <option key={phase} value={phase}>Phase {phase}</option>)}</select></label>
              <label><span>Minimum duration</span><select value={minInterruptionDuration} onChange={(event) => setMinInterruptionDuration(event.target.value)}><option value="5">5 minutes</option><option value="10">10 minutes</option><option value="15">15 minutes</option><option value="30">30 minutes</option><option value="45">45 minutes</option><option value="60">1 hour</option><option value="120">2 hours</option></select></label>
              <label><span>Minimum reduction</span><div className="number-with-unit"><input type="number" min="0" max="100" step="5" value={minInterruptionReduction} onChange={(event) => setMinInterruptionReduction(event.target.value)} /><span>%</span></div></label>
              <div className="filter-meta interruption-method"><span>Historical comparison</span><strong>Same time, prior 1–3 weeks</strong></div>
            </div>
            {interruptionLoading ? <LoadingBand /> : null}
            <div className="interruption-kpis">
              <div><span>Events</span><strong>{formatNumber(interruptionSummary?.event_count)}</strong></div>
              <div><span>Affected intersections</span><strong>{formatNumber(interruptionSummary?.affected_intersections)}</strong></div>
              <div><span>Longest event</span><strong>{formatDuration(interruptionSummary?.longest_duration_minutes)}</strong></div>
              <div><span>Largest average reduction</span><strong>{formatNumber(interruptionSummary?.largest_reduction_pct, 1)}%</strong></div>
            </div>

            <div className="section-title compact interruption-chart-title">
              <div><span>Selected event</span><h2>{interruptionTimeline ? `${interruptionTimeline.event.signal_id} · ${shortName(interruptionTimeline.event.name)}` : "Event detail"}</h2></div>
              {interruptionTimeline ? <div className="event-detail-meta"><span>{interruptionTimeline.event.direction}</span><span>{formatDuration(interruptionTimeline.event.duration_minutes)}</span><strong>{formatNumber(interruptionTimeline.event.avg_reduction_pct, 1)}% reduction</strong></div> : null}
            </div>
            <div className="chart-wrap interruption-chart">
              {interruptionTimeline?.points.length ? <ResponsiveContainer width="100%" height="100%">
                <LineChart data={interruptionTimeline.points} margin={{ top: 18, right: 24, left: 0, bottom: 8 }}>
                  <CartesianGrid stroke="#dfe5e2" vertical={false} />
                  <XAxis dataKey="timestamp" tickFormatter={(value) => new Date(value).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })} minTickGap={28} />
                  <YAxis />
                  <Tooltip labelFormatter={(value) => formatDateTime(String(value))} formatter={(value) => formatNumber(Number(value), 1)} />
                  <Legend />
                  <ReferenceArea x1={interruptionTimeline.event.start_time} x2={interruptionTimeline.event.end_time} fill="#d56b2d" fillOpacity={0.14} />
                  <Line type="monotone" dataKey="observed_volume" name="Observed volume" stroke="#1d5f4b" strokeWidth={2.4} dot={false} />
                  <Line type="monotone" dataKey="expected_volume" name="Historical expected" stroke="#547ab7" strokeWidth={2} strokeDasharray="6 4" dot={false} />
                </LineChart>
              </ResponsiveContainer> : <div className="empty-state">Select an interruption event to inspect its observed and expected volume.</div>}
            </div>

            <div className="section-title compact"><div><span>Ranked by average reduction</span><h2>Interruption events</h2></div><div className="count-badge">{interruptionEvents.length} shown</div></div>
            <div className="data-table interruption-table">
              <div className="table-head"><span>Intersection</span><span>Direction</span><span>Start</span><span>Duration</span><span>Observed</span><span>Expected</span><span>Reduction</span></div>
              {interruptionEvents.length ? interruptionEvents.map((item) => <button className={`table-row ${selectedInterruptionId === item.event_id ? "selected" : ""}`} key={item.event_id} onClick={() => setSelectedInterruptionId(item.event_id)}><strong>{item.signal_id} · {shortName(item.name)}</strong><span>{item.direction}</span><span>{formatDateTime(item.start_time)}</span><span>{formatDuration(item.duration_minutes)}</span><span>{formatNumber(item.observed_volume, 1)}</span><span>{formatNumber(item.expected_volume, 1)}</span><span className="interruption-reduction">-{formatNumber(item.avg_reduction_pct, 1)}%</span></button>) : <div className="empty-state">No interruption events match the selected date, intersection, duration, and reduction filters.</div>}
            </div>
          </section>
        ) : null}

        {view === "moe" ? (
          <section className="workspace-view">
            <div className="section-title">
              <div><span>New analytics store</span><h1>MOE Explorer</h1></div>
              <div className="count-badge">{formatNumber(moeSummary?.row_count)} {moeGrain === "15min" ? "15-minute" : "hourly"} rows</div>
            </div>
            <div className="moe-control-grid">
              <label><span>Grain</span><select value={moeGrain} onChange={(event) => setMoeGrain(event.target.value as MoeGrain)}><option value="15min">15-minute</option><option value="hourly">Hourly</option></select></label>
              <label><span>MOE</span><select value={moeMetric} onChange={(event) => setMoeMetric(event.target.value)}>{moeMeta?.metrics.map((metric) => <option key={metric.metric} value={metric.metric}>{metric.label}</option>)}</select></label>
              <label><span>Intersection</span><select value={moeSignalId} onChange={(event) => setMoeSignalId(event.target.value)}>{moeMeta?.intersections.map((intersection) => <option key={intersection.signal_id} value={intersection.signal_id}>{intersection.signal_id} · {shortName(intersection.name)}</option>)}</select></label>
              <label><span>Phase</span><select value={moePhase} onChange={(event) => setMoePhase(event.target.value)}><option value="all">All phases</option>{(moeMeta?.dimensions.phases ?? []).map((phase) => <option key={phase} value={phase}>Phase {phase}</option>)}</select></label>
              <div className="filter-meta moe-meta-card"><span>Rollup</span><strong>{selectedMoeMetric?.hourly_rollup ?? "-"}</strong></div>
              <div className="filter-meta moe-meta-card"><span>Unit</span><strong>{selectedMoeMetric?.unit ?? "-"}</strong></div>
            </div>
            {moeLoading ? <LoadingBand /> : null}
            <div className="timeline-summary">
              <span>{moeRows.length} returned rows</span>
              <span>{selectedMoeMetric?.label ?? moeMetric}</span>
              <span>{moeGrain === "15min" ? "15-minute source grain" : "Hourly rollup"}</span>
              <span>{direction === "All" ? "All directions" : direction}</span>
            </div>
            <div className="chart-wrap">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={moeRows} margin={{ top: 18, right: 24, left: 0, bottom: 8 }}>
                  <CartesianGrid stroke="#dfe5e2" vertical={false} />
                  <XAxis dataKey="timestamp" tickFormatter={(value) => new Date(value).toLocaleDateString("en-US", { month: "short", day: "numeric" })} minTickGap={42} />
                  <YAxis />
                  <Tooltip labelFormatter={(value) => formatDateTime(String(value))} formatter={(value) => formatNumber(Number(value), 2)} />
                  <Legend />
                  <Line type="monotone" dataKey="value" name={selectedMoeMetric?.label ?? moeMetric} stroke="#1d5f4b" strokeWidth={2} dot={moeRows.length < 220} />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="data-table moe-table">
              <div className="table-head"><span>Timestamp</span><span>Signal</span><span>Phase</span><span>Direction</span><span>MOE</span><span>Value</span><span>Samples</span></div>
              {moeRows.length ? moeRows.slice(0, 80).map((row) => <div className="table-row" key={`${row.timestamp}-${row.signal_id}-${row.phase}-${row.metric}-${row.value}`}><strong>{formatDateTime(row.timestamp)}</strong><span>{row.signal_id}</span><span>{row.phase ?? "-"}</span><span>{row.direction}</span><span>{selectedMoeMetric?.label ?? row.metric}</span><span>{formatNumber(row.value, 2)}</span><span>{row.sample_count}</span></div>) : <div className="empty-state">No MOE rows match this filter window.</div>}
            </div>
          </section>
        ) : null}

      </main>
    </div>
  );
}

function EventTable({ items, onOpen }: { items: Anomaly[]; onOpen?: (item: Anomaly) => void }) {
  if (!items.length) return <div className="empty-state">No anomaly events in this filter window.</div>;
  return <div className="event-list">{items.map((item) => <button key={`${item.entity_id}-${item.timestamp}`} onClick={() => onOpen?.(item)}><span className="event-tone" style={{ background: severityColors[item.severity] }} /><span className="event-main"><strong>{shortName(item.name)}</strong><small>{formatDateTime(item.timestamp)} · {item.direction}</small></span><span className="event-delta"><strong>{item.deviation >= 0 ? "+" : ""}{formatNumber(item.deviation, 0)}</strong><small>vehicles</small></span><span className="event-score"><strong>{formatNumber(item.abs_score, 1)}</strong><small>GEH</small></span></button>)}</div>;
}
