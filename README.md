# Bee Ridge Anomaly Detector

A separate, read-only dashboard for anomaly and changepoint analysis over the Bee Ridge Road ATSPM hourly history.

## Architecture

- `pipeline/`: extracts Phase 2 and Phase 6 hourly volume from the existing historical SQLite outputs, runs `traffic-anomaly`, and materializes a compact DuckDB store.
- `backend/`: FastAPI service that serves bounded reads from the DuckDB store.
- `frontend/`: focused React dashboard using Leaflet and Recharts.

The browser never scans the source SQLite files or runs the anomaly model.

## Build the analytical store

From the main repository root:

```bash
bash anomaly-dashboard/scripts/build_data.sh
```

The model uses a 42-day rolling baseline, adjustable GEH entity threshold defaulting to `3.5`, and 14-day changepoint windows. Signal `1256` currently has no `arrival_on_green` table in either historical database, so it appears as `No data` while the other 13 intersections produce 26 directional entities.

## Build the MOE analytics store

The anomaly store above is intentionally narrow. To support future tabs without slowing the dashboard down, build the separate all-MOE store:

```bash
bash anomaly-dashboard/scripts/build_moe_data.sh
```

This writes `anomaly-dashboard/data/moe_analytics.duckdb` and does not modify `anomaly-dashboard/data/anomaly.duckdb`. The MOE store keeps the original 15-minute table rows from the historical SQLite outputs and also materializes hourly rollups. It includes all mapped Bee Ridge intersections and the supported MOEs: volume, AOG %, AOR %, average wait, maximum wait, skips, green time, green occupancy %, red occupancy %, split failures, Gap-Outs, Max-Outs, Force-Offs, cycle length, actual cycle length, and actual offset.

Fast read endpoints are available at:

- `/api/moe/meta`
- `/api/moe/observations`
- `/api/moe/daily-summary`

Use bounded filters such as `metric`, `grain`, `dateFrom`, `dateTo`, `signalId`, `direction`, `phase`, and `limit` for dashboard tabs.

## Build interruption / EOI events

The Interruptions / EOI tab adapts the historical EOI idea from `yash-5595/interruptions-historic-EOI` at both 5-minute and 15-minute resolution. It compares each Phase 2/6 interval with the same time from one, two, and three weeks earlier, requires at least two valid historical values, smooths volume over 30 minutes, and combines consecutive below-baseline intervals into one event.

```bash
bash anomaly-dashboard/scripts/build_interruption_data.sh
```

This preserves the original 15-minute MOE database and creates separate read-optimized stores. The 15-minute results are written to `interruption_events.duckdb`; true 5-minute volumes are counted from raw advance-detector activation events and written to `moe_volume_5min.duckdb`, with modeled events in `interruption_events_5min.duckdb`. The frontend can switch bucket size and filter materialized events by date, direction, phase, selected intersections, minimum duration, and minimum volume reduction.

Fast read endpoints are available at:

- `/api/interruptions/meta`
- `/api/interruptions/events`
- `/api/interruptions/timeline/{event_id}`

## Run locally

Install dependencies once:

```bash
python -m pip install --target anomaly-dashboard/.python_deps -r anomaly-dashboard/backend/requirements.txt
cd anomaly-dashboard/frontend
npm install
```

Start both services:

```bash
bash anomaly-dashboard/scripts/restart.sh
```

Open `http://127.0.0.1:4180/`. The API runs at `http://127.0.0.1:8010/`.

## Verify

```bash
PYTHONPATH="anomaly-dashboard/.python_deps:.python_deps" \
  python -m unittest discover -s anomaly-dashboard/tests -v
cd anomaly-dashboard/frontend && npm run build
```

### Experimental Chronos review tab

The `Chronos · Experimental` tab reads a packaged snapshot at `frontend/public/chronos-review.json`. It does not call the inference engine or alter the existing anomaly/EOI algorithms, stores, thresholds, or endpoints. The initial snapshot covers signal 1252, phase 2, on May 2, May 11, and part of May 18, 2026. Only saved dates can be selected; intersection/phase controls remain disabled until more series are exported.

The view shows observed counts, next-bucket forecasts, validation-calibrated lower thresholds, low-volume buckets, persistent candidate intervals, and confirmation times. Unscored intervals are gray and excluded from normal/abnormal classifications. Events require two consecutive low buckets and become confirmable only after the second closes. Event totals and percentage drops are computed over event-member buckets. These are candidate events, not verified incidents. Source clock timezone is unresolved.

Refresh the snapshot after generating new reviewed results:

```bash
python3 anomaly-dashboard/scripts/export_chronos_snapshot.py
```

The exporter currently targets the reviewed signal/phase; expanding coverage requires extending the snapshot schema/exporter and selectors rather than relabeling this data. Build with `npm run build` in `anomaly-dashboard/frontend`. Start the existing dashboard using `bash anomaly-dashboard/scripts/restart.sh` from the repository root, then select the new tab. Static snapshot delivery also works from a production Vite build, with no new Python dependencies. This change does not deploy to the remote VM.
