import { useEffect, useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import "./chronos.css";
type Point = { day: string; origin: string; actual: number | null; prediction: number | null; lower_threshold: number | null; scored: boolean; low: boolean; input_available: boolean; event_id: number | null; anomaly_score: number | null };
type Event = { event_id: number; day: string; start: string; end: string; confirmed_at: string; duration_minutes: number; peak_score: number | null; observed: number; expected: number; drop_percent: number | null; status: string };
type Snapshot = { run_id: string; status: string; replay_clock: string; schema_version: number; model: string; signal_id: string; phase: number; dates: string[]; points: Point[]; events: Event[] };
// Numeric chart coordinates preserve the source clock without browser timezone conversion.
const coordinate = (value: string) => Date.parse(value.replace(" ", "T") + "Z");
const clock = (value: number) => new Date(value).toISOString().slice(11, 16);
const fmt = (n: number | null, digits = 1) => n == null ? "—" : n.toFixed(digits);
export default function ChronosReplayView() {
  const [data, setData] = useState<Snapshot | null>(null);
  const [day, setDay] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [runs, setRuns] = useState<{id:string;signal:string;phase:number;status:string;start:string}[]>([]);
  const [run, setRun] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    const refresh = async () => {
      try {
        const base = (import.meta.env.VITE_ANOMALY_API_BASE_URL ?? "").replace(/\/$/, "");
        const listResponse = await fetch(`${base}/api/chronos/runs`, {signal:controller.signal});
        if (!listResponse.ok) throw new Error("Could not load detector runs.");
        const list = await listResponse.json(); setRuns(list.runs);
        const id = run || list.runs[0]?.id;
        if (!id) { setError("No detector runs yet. Start a chronological replay with the detector worker."); return; }
        const response = await fetch(`${base}/api/chronos/runs/${encodeURIComponent(id)}`, {signal:controller.signal});
        if (!response.ok) throw new Error("Could not load detector results.");
        const snapshot: Snapshot = await response.json();
        if (snapshot.schema_version !== 1 || !Array.isArray(snapshot.points)) throw new Error("Unsupported detector response.");
        setData(snapshot); setDay(old => snapshot.dates.includes(old) ? old : snapshot.dates[0]); setError("");
      } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Detector unavailable."); }
    };
    void refresh(); const timer = window.setInterval(refresh, 15000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [attempt, run]);
  if (error) return <section className="chronos-view"><p role="alert">{error}</p><button onClick={() => setAttempt(attempt + 1)}>Retry</button></section>;
  if (!data) return <section className="chronos-view" role="status">Loading Chronos detector…</section>;
  const chooseRun = (signal: string, phase: number, date: string) => {
    const candidates = runs.filter(r => r.signal === signal);
    const next = candidates.find(r => r.phase === phase && r.start.slice(0,10) === date)
      ?? candidates.find(r => r.phase === phase) ?? candidates[0];
    if (next) { setRun(next.id); setData(null); setSelected(null); }
  };
  const signals = [...new Set(runs.map(r => r.signal))].sort();
  const phases = [...new Set(runs.filter(r => r.signal === data.signal_id).map(r => r.phase))].sort();
  const dates = [...new Set(runs.filter(r => r.signal === data.signal_id && r.phase === data.phase).flatMap(r => [r.start.slice(0,10)]).concat(data.dates))].sort();
  const points = data.points.filter(p => p.day === day).map(p => ({ ...p, time: coordinate(p.origin), candidate: p.low ? p.actual : null }));
  const events = data.events.filter(e => coordinate(e.start) < coordinate(day + " 00:00:00") + 86400000 && coordinate(e.end) > coordinate(day + " 00:00:00"));
  const scored = points.filter(p => p.scored).length;
  const missingHistory = points.filter(p => !p.input_available).length;
  const missingObservation = points.filter(p => p.input_available && !p.scored).length;
  const start = points.length ? points[0].time : coordinate(day + " 00:00:00");
  const end = points.length ? points[points.length - 1].time + 300000 : start + 86400000;
  return <section className="chronos-view">
    <header className="chronos-heading"><div><h2>Chronos anomaly detector <span className="chronos-badge">Experimental</span></h2><p>{data.model} · Run: {data.status} · Replay clock: {data.replay_clock}</p></div></header>
    <p className="chronos-notice">Neural-network detection from a chronological historical replay. Forecasts are stored before outcomes are scored. This is not a live traffic feed; alerts are investigation candidates, not confirmed incidents.</p>
    <div className="chronos-controls">
      <label>Detector run<select value={run || data.run_id} onChange={e => {setRun(e.target.value); setData(null); setSelected(null);}}>{runs.map(r => <option key={r.id} value={r.id}>{r.id} · {r.status}</option>)}</select></label>
      <label>Intersection<select value={data.signal_id} onChange={e => chooseRun(e.target.value, data.phase, day)}>{signals.map(signal => <option key={signal} value={signal}>Signal {signal}</option>)}</select></label>
      <label>Phase<select value={data.phase} onChange={e => chooseRun(data.signal_id, Number(e.target.value), day)}>{phases.map(phase => <option key={phase} value={phase}>Phase {phase}</option>)}</select></label>
      <label>Review date<select value={day} onChange={e => { if (data.dates.includes(e.target.value)) {setDay(e.target.value); setSelected(null);} else chooseRun(data.signal_id, data.phase, e.target.value); }}>{dates.map(d => <option key={d}>{d}</option>)}</select></label>
      <span>Results refresh every 15 seconds while the worker runs. No direction mapping is assumed.</span>
    </div>
    <div className="chronos-stats" aria-live="polite"><div><strong>{scored} / {points.length}</strong><span>Scored buckets</span></div><div><strong>{points.filter(p => p.low).length}</strong><span>Low-volume buckets</span></div><div><strong>{events.length}</strong><span>Detected events</span></div><div><strong>{points.length - scored}</strong><span>Unscored buckets</span></div></div>
    <p>Coverage: {day} {clock(start)}–{end - start === 86400000 ? "24:00" : clock(end)}{end - start === 86400000 ? " (full day)" : " (partial day)"}. {missingHistory} buckets lack complete input history; {missingObservation} lack an observed outcome. Unscored periods are not classified as normal.</p>
    <div className="chronos-chart" role="img" aria-label={`Observed volume, predictions, lower threshold and candidate intervals for ${day}. ${events.length} persistent candidates.`}>
      <ResponsiveContainer width="100%" height="100%"><LineChart data={points} margin={{ top: 18, right: 20, bottom: 20, left: 12 }}>
        <CartesianGrid strokeDasharray="3 3" /><XAxis dataKey="time" type="number" domain={[start, end]} tickFormatter={clock} scale="time" /><YAxis width={48} /><Tooltip labelFormatter={v => `${day} ${clock(Number(v))} · source clock`} formatter={(v, name) => [typeof v === "number" ? fmt(v) : "Missing", name]} />
        {points.filter(p => !p.scored).map(p => <ReferenceArea key={p.time} x1={p.time} x2={p.time + 300000} fill="#64748b" fillOpacity={0.18} />)}
        {events.map(e => <ReferenceArea key={e.event_id} x1={Math.max(start, coordinate(e.start))} x2={Math.min(end, coordinate(e.end))} fill="#dc2626" fillOpacity={selected === e.event_id ? 0.35 : 0.13} stroke={selected === e.event_id ? "#dc2626" : undefined} />)}
        {events.map(e => <ReferenceLine key={e.event_id} x={coordinate(e.confirmed_at)} stroke="#7c3aed" strokeDasharray="3 3" />)}
        <Line dataKey="actual" name="Observed" stroke="#475569" dot={false} connectNulls={false} isAnimationActive={false} />
        <Line dataKey="prediction" name="Predicted" stroke="#00796b" dot={false} connectNulls={false} isAnimationActive={false} />
        <Line dataKey="lower_threshold" name="Lower threshold" stroke="#d97706" strokeDasharray="5 4" dot={false} connectNulls={false} isAnimationActive={false} />
        <Line dataKey="candidate" name="Low-volume bucket" stroke="none" dot={{ r: 3, fill: "#dc2626", stroke: "#dc2626" }} connectNulls={false} isAnimationActive={false} />
      </LineChart></ResponsiveContainer>
    </div>
    <section className="chronos-guide" aria-labelledby="chronos-guide-title">
      <h3 id="chronos-guide-title">How to read this plot</h3>
      <p>Each point represents a five-minute interval. The horizontal axis shows time; the vertical axis shows detector activations during that interval, which are not necessarily unique vehicles.</p>
      <dl className="chronos-legend-definitions">
        <div><dt><span className="chronos-key observed" aria-hidden="true" />Gray line · Observed volume</dt><dd>The count actually recorded by the detectors.</dd></div>
        <div><dt><span className="chronos-key predicted" aria-hidden="true" />Green line · Predicted volume</dt><dd>What Chronos expected for that interval, using information available before it began.</dd></div>
        <div><dt><span className="chronos-key threshold" aria-hidden="true" />Orange dashed line · Lower threshold</dt><dd>The cutoff for an unusually low count. It sits below the prediction to allow for forecast error. An observation below it is flagged; simply being below the green line is not enough.</dd></div>
        <div><dt><span className="chronos-key low" aria-hidden="true" />Red dot · Low-volume bucket</dt><dd>One five-minute observation below the lower threshold. A single red dot does not trigger a persistent-event alert.</dd></div>
        <div><dt><span className="chronos-key interval" aria-hidden="true" />Red shading · Candidate event</dt><dd>An interval flagged after two consecutive low buckets. Shading starts at the first low bucket and extends through recovery, or to the last assessed time if unresolved. This is not a confirmed incident.</dd></div>
        <div><dt><span className="chronos-key trigger" aria-hidden="true" />Purple dotted line · Alert triggered</dt><dd>The earliest time the rule could raise the alert: when the second consecutive low bucket finished, ten minutes after the first began.</dd></div>
        <div><dt><span className="chronos-key missing" aria-hidden="true" />Gray shading · Unscored period</dt><dd>History or observations were missing, so the detector could not assess the interval. Unscored does not mean normal traffic.</dd></div>
      </dl>
      <p className="chronos-caption">Example: if the prediction is 40 and the lower threshold is 32, an observed count of 35 is not flagged; 25 is a red dot. Two consecutive flagged intervals trigger an alert. These numbers are illustrative.</p>
      <details><summary>What do the counts and event-table terms mean?</summary>
        <dl className="chronos-term-definitions">
          <dt>Scored buckets</dt><dd>Intervals with both a usable forecast and an observed count, out of all scheduled intervals.</dd>
          <dt>Low-volume buckets versus detected events</dt><dd>Low-volume buckets count individual red dots. Detected events group persistent deviations; isolated dots may never become an event.</dd>
          <dt>Active / awaiting data / recovered</dt><dd>Active means an alert has not met the recovery rule. Awaiting data means missing values prevent assessment. Recovered means two consecutive observations meet both the current threshold and the fixed threshold recorded at event onset. These are detector states, not verified traffic conditions.</dd>
          <dt>End and duration</dt><dd>For recovered events, the end is when recovery was established. For unresolved events, it is the last assessed boundary, not a confirmed end to the disruption. Duration includes any intervening unscored periods.</dd>
          <dt>Observed total / predicted total</dt><dd>Counts summed across scored intervals belonging to the event, including the two recovery intervals. Unscored intervals contribute to neither total.</dd>
          <dt>Drop vs prediction</dt><dd>100 × (predicted total − observed total) / predicted total. It compares those totals, not the drop in a single red-dot interval. A negative value means the observed total exceeded the prediction; a dash means the percentage cannot be calculated.</dd>
          <dt>Peak anomaly score</dt><dd>The largest normalized shortfall within the event: (prediction − observation) / (prediction − lower threshold), with negative shortfalls set to zero. A score above 1 crosses the low-volume cutoff. It is not an incident probability or a percentage.</dd>
          <dt>How the threshold is set</dt><dd>Prediction plus the fifth percentile of validation errors (observed minus predicted), floored at zero. It is an exploratory cutoff, not a guarantee of a 5% false-alarm rate. Validation also informed model selection.</dd>
        </dl>
      </details>
      <p className="chronos-caption">Times use the source clock; timezone is unverified. Use these alerts to prioritize investigation, alongside detector health and other traffic evidence.</p>
    </section>
    <h3>Detected events</h3><p>{events.filter(e => e.status === "active").length} active · {events.filter(e => e.status === "data_gap").length} awaiting data · {events.filter(e => e.status === "recovered").length} recovered as of the replay clock.</p>
    <p>An alert triggers after two consecutive low buckets. Recovery requires two buckets at or above both the current and onset thresholds; missing data pauses assessment. Active events at replay end remain unresolved. Select a row to highlight its interval.</p>
    <div className="chronos-table-wrap"><table><thead><tr><th>Review</th><th>Start</th><th>End</th><th>Alert triggered</th><th>Status</th><th>Duration</th><th>Observed total</th><th>Predicted total</th><th>Drop vs prediction</th><th>Peak anomaly score</th></tr></thead><tbody>
      {events.map(e => <tr key={e.event_id} className={selected === e.event_id ? "selected" : ""}><td><button aria-pressed={selected === e.event_id} onClick={() => setSelected(selected === e.event_id ? null : e.event_id)}>Event {e.event_id}</button></td><td>{e.start.slice(11,16)}</td><td>{e.end.slice(11,16)}</td><td>{e.confirmed_at.slice(11,16)}</td><td>{e.status.replace("_", " ")}</td><td>{e.duration_minutes} min</td><td>{fmt(e.observed,0)}</td><td>{fmt(e.expected)}</td><td>{fmt(e.drop_percent)}%</td><td>{fmt(e.peak_score,2)}</td></tr>)}
      {!events.length && <tr><td colSpan={10}>No persistent candidates in this saved period. This does not rule out incidents or issues in unscored data.</td></tr>}
    </tbody></table></div>
    <details><summary>Model and alert rules</summary><p>Input: the preceding hour, matching slots from the previous three weeks, and one-hot hour/weekday indicators. The model predicts three buckets; this view uses the next bucket to avoid counting overlapping forecasts twice. Thresholds use the fifth percentile of five-minute validation residuals. Validation also informed model selection, so these thresholds are exploratory. Missing data resets pending triggers and recovery counters; active events remain unresolved. Missing counts are not filled with zero. Anomaly score is the positive prediction deficit divided by the prediction-to-threshold margin; a score above 1 exceeds the low-volume threshold. It is not an incident probability. Totals cover scored buckets through recovery, including the two recovery buckets. The onset recovery reference is an experimental heuristic, not a verified incident boundary. May examples have already been inspected and do not establish independent detection accuracy.</p></details>
  </section>;
}
