import { useEffect, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import "./chronos.css";
type Point = { day: string; origin: string; actual: number | null; prediction: number | null; lower_threshold: number | null; scored: boolean; low: boolean; input_available: boolean; event_id: number | null };
type Event = { event_id: number; day: string; start: string; end: string; confirmed_at: string; duration_minutes: number; observed: number; expected: number; drop_percent: number | null };
type Snapshot = { schema_version: number; model: string; signal_id: string; phase: number; dates: string[]; points: Point[]; events: Event[] };
// Numeric chart coordinates preserve the source clock without browser timezone conversion.
const coordinate = (value: string) => Date.parse(value.replace(" ", "T") + "Z");
const clock = (value: number) => new Date(value).toISOString().slice(11, 16);
const fmt = (n: number | null, digits = 1) => n == null ? "—" : n.toFixed(digits);
export default function ChronosView() {
  const [data, setData] = useState<Snapshot | null>(null);
  const [day, setDay] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setError(""); setData(null);
    fetch(`${import.meta.env.BASE_URL}chronos-review.json`, { signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error("Saved Chronos results are unavailable."); return response.json(); })
      .then((snapshot: Snapshot) => { if (snapshot.schema_version !== 1 || !Array.isArray(snapshot.points) || !snapshot.dates.length) throw new Error("Unsupported or empty Chronos snapshot."); setData(snapshot); setDay(snapshot.dates[0]); setSelected(null); })
      .catch(reason => { if (!controller.signal.aborted) setError(reason.message || "Could not load saved results."); });
    return () => controller.abort();
  }, [attempt]);
  if (error) return <section className="chronos-view"><p role="alert">{error}</p><button onClick={() => setAttempt(attempt + 1)}>Retry</button></section>;
  if (!data) return <section className="chronos-view" role="status">Loading saved Chronos predictions…</section>;
  const points = data.points.filter(p => p.day === day).map(p => ({ ...p, time: coordinate(p.origin), candidate: p.low ? p.actual : null }));
  const events = data.events.filter(e => e.day === day);
  const scored = points.filter(p => p.scored).length;
  const missingHistory = points.filter(p => !p.input_available).length;
  const missingObservation = points.filter(p => p.input_available && !p.scored).length;
  const start = coordinate(day + " 00:00:00");
  const end = points.length ? points[points.length - 1].time + 300000 : start + 86400000;
  return <section className="chronos-view">
    <header className="chronos-heading"><div><h2>Chronos forecast review <span className="chronos-badge">Experimental</span></h2><p>Saved five-minute predictions · {data.model}</p></div></header>
    <p className="chronos-notice">Candidate low-volume events, not confirmed incidents. This view is an offline replay; it does not run live inference. The existing anomaly methods are unchanged.</p>
    <div className="chronos-controls">
      <label>Intersection<select value={data.signal_id} disabled><option value={data.signal_id}>Signal {data.signal_id}</option></select></label>
      <label>Phase<select value={data.phase} disabled><option value={data.phase}>Phase {data.phase}</option></select></label>
      <label>Review date<select value={day} onChange={e => { setDay(e.target.value); setSelected(null); }}>{data.dates.map(d => <option key={d}>{d}</option>)}</select></label>
      <span>Only saved coverage is selectable. No direction mapping is assumed.</span>
    </div>
    <div className="chronos-stats" aria-live="polite"><div><strong>{scored} / {points.length}</strong><span>Scored buckets</span></div><div><strong>{points.filter(p => p.low).length}</strong><span>Low-volume buckets</span></div><div><strong>{events.length}</strong><span>Persistent candidates</span></div><div><strong>{points.length - scored}</strong><span>Unscored buckets</span></div></div>
    <p>Coverage: {day} {clock(start)}–{end - start === 86400000 ? "24:00" : clock(end)}{end - start === 86400000 ? " (full day)" : " (partial day)"}. {missingHistory} buckets lack complete input history; {missingObservation} lack an observed outcome. Unscored periods are not classified as normal.</p>
    <div className="chronos-chart" role="img" aria-label={`Observed volume, predictions, lower threshold and candidate intervals for ${day}. ${events.length} persistent candidates.`}>
      <ResponsiveContainer width="100%" height="100%"><LineChart data={points} margin={{ top: 18, right: 20, bottom: 20, left: 12 }}>
        <CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="time" type="number" domain={[start, end]} tickFormatter={clock} scale="time" /><YAxis width={48} /><Tooltip labelFormatter={v => `${day} ${clock(Number(v))} · source clock`} formatter={(v, name) => [typeof v === "number" ? fmt(v) : "Missing", name]} /><Legend />
        {points.filter(p => !p.scored).map(p => <ReferenceArea key={p.time} x1={p.time} x2={p.time + 300000} fill="#64748b" fillOpacity={0.18} />)}
        {events.map(e => <ReferenceArea key={e.event_id} x1={coordinate(e.start)} x2={coordinate(e.end)} fill="#dc2626" fillOpacity={selected === e.event_id ? 0.35 : 0.13} stroke={selected === e.event_id ? "#dc2626" : undefined} />)}
        {events.map(e => <ReferenceLine key={e.event_id} x={coordinate(e.confirmed_at)} stroke="#7c3aed" strokeDasharray="3 3" />)}
        <Line dataKey="actual" name="Observed" stroke="#475569" dot={false} connectNulls={false} isAnimationActive={false} />
        <Line dataKey="prediction" name="Predicted" stroke="#00796b" dot={false} connectNulls={false} isAnimationActive={false} />
        <Line dataKey="lower_threshold" name="Lower threshold" stroke="#d97706" strokeDasharray="5 4" dot={false} connectNulls={false} isAnimationActive={false} />
        <Line dataKey="candidate" name="Low-volume bucket" stroke="none" dot={{ r: 3, fill: "#dc2626", stroke: "#dc2626" }} connectNulls={false} isAnimationActive={false} />
      </LineChart></ResponsiveContainer>
    </div>
    <p className="chronos-caption">Volume units: detector activations per five-minute bucket. Red shading: candidate interval. Purple dotted line: earliest confirmation. Gray: unscored. Times use the source clock; timezone is unverified.</p>
    <h3>Candidate events</h3>
    <p>An event requires two consecutive low buckets (10 minutes); it is confirmed only when the second bucket closes. Select a row to highlight the interval.</p>
    <div className="chronos-table-wrap"><table><thead><tr><th>Review</th><th>Start</th><th>End</th><th>Confirmed</th><th>Duration</th><th>Observed total</th><th>Predicted total</th><th>Drop vs prediction</th></tr></thead><tbody>
      {events.map(e => <tr key={e.event_id} className={selected === e.event_id ? "selected" : ""}><td><button aria-pressed={selected === e.event_id} onClick={() => setSelected(selected === e.event_id ? null : e.event_id)}>Event {e.event_id}</button></td><td>{e.start.slice(11,16)}</td><td>{e.end.slice(11,16)}</td><td>{e.confirmed_at.slice(11,16)}</td><td>{e.duration_minutes} min</td><td>{fmt(e.observed,0)}</td><td>{fmt(e.expected)}</td><td>{fmt(e.drop_percent)}%</td></tr>)}
      {!events.length && <tr><td colSpan={8}>No persistent candidates in this saved period. This does not rule out incidents or issues in unscored data.</td></tr>}
    </tbody></table></div>
    <details><summary>Model and alert rules</summary><p>Input: the preceding hour, matching slots from the previous three weeks, and one-hot hour/weekday indicators. The model predicts three buckets; this view uses the next bucket to avoid counting overlapping forecasts twice. Thresholds use the fifth percentile of five-minute validation residuals. Validation also informed model selection, so these thresholds are exploratory. Missing data breaks an event; missing counts are not filled with zero. May examples have already been inspected and do not establish independent detection accuracy.</p></details>
  </section>;
}
