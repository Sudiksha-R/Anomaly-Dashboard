# Chronos neural-network anomaly detector

The Chronos tab now reads a detector database through the API, replacing the static
`chronos-review.json` viewer. The worker runs the fine-tuned Chronos-2 neural network;
it does not import the old report's predictions. Existing anomaly and EOI methods
are unchanged.

## What runs now

A chronological, accelerated historical replay for a selected signal, phase, and
period. Each five-minute replay step first scores forecasts whose outcome buckets
have closed, then generates and commits the next three forecasts. The UI polls
results every 15 seconds. This is actual inference and stateful anomaly detection,
not a live-feed deployment. Reading the tab does not start a GPU job.

- Source: `data/moe_volume_5min.duckdb`, volume rows in `moe_observations_5min` (read only).
- Model: `../experiments/bee_ridge_forecasting/results/calendar_epochs/weekly_calendar/finetuned-ckpt`.
- Inputs: preceding 12 five-minute volumes, matching slots from the preceding three
  weeks, 24 hour and 7 weekday indicators. Current future volume is masked with NaN.
- Source-history SQL explicitly requires `timestamp < replay_clock`. Weekly forecast
  covariates are historical values, not upcoming observations.
- Three forecast horizons are persisted before their outcomes are fetched/scored.
  Alerts use only the next-bucket horizon to avoid counting overlapping forecasts twice.
- No training occurs. The model and validation residual thresholds remain frozen.
- Counts are detector activations, not guaranteed unique vehicles. Source timezone
  and direction mapping remain unverified.

## Alert lifecycle

A value strictly below `max(0, prediction + validation_residual_q05)` is low.
Two adjacent low buckets trigger an event only after the second bucket closes.
The initial interval is retrospectively shaded, but `confirmed_at` is the actual
replay trigger time. The UI calls this **Alert triggered**.

The event remains active until two consecutive scored buckets meet both the current
lower threshold and the larger lower threshold from the two onset buckets. This
onset reference prevents a falling prediction alone from declaring recovery.
Missing data resets pending triggers and recovery counters; an already active event
becomes `data_gap` and remains unresolved. Active events at replay end are not forced
closed. Recovery is an experimental heuristic: normal diurnal demand changes can
also affect its duration, and event boundaries need review against external evidence.

Anomaly score = positive prediction deficit / prediction-to-threshold margin.
Above 1 means below the low threshold; it is not an incident probability. Event
volume totals include scored buckets through recovery, including the two recovery
buckets. Peak score is the maximum among the event's scored buckets. A gap is never
silently converted to a zero count.

## Run a replay

From the project root, **inside a GPU compute allocation**:

```bash
cd "/blue/ranka/rajavaram.s/ATSPM Dashboard"
experiments/traffic_forecast/.venv/bin/python \
  anomaly-dashboard/scripts/run_chronos_detector.py \
  --signal 1252 --phase 2 \
  --start '2026-05-02 00:00:00' \
  --end '2026-05-03 00:00:00' \
  --run-id my-new-replay
```

Use a unique run ID. Existing runs are never overwritten. `--end` is exclusive for
primary detection buckets. Source coverage must extend ten minutes beyond that end
so all three forecast horizons can close. The worker uses CUDA when available and
otherwise CPU; use a compute allocation rather than a login node for inference.
Replay is restricted to dates after the validation period (after April 2, 2026).
An interrupted run has no automatic resume; a handled error marks it failed, while
a hard process termination may leave a stale `running` status. Run again with a new
ID after diagnosing the cause. It is not an unattended production service yet.

Start/restart the usual dashboard to load the new API routes:

```bash
bash anomaly-dashboard/scripts/restart.sh
```

Open the existing anomaly dashboard at port 4180, choose **Chronos · Experimental**,
and select a detector run. GPU inference is separate from the web process.

## Files and API

- `backend/chronos_detector.py`: SQLite schema, scoring/event lifecycle, snapshots.
- `scripts/run_chronos_detector.py`: read-only source access, real inference, replay clock.
- `data/chronos_detector.sqlite`: separate persisted runs, forecasts, points, events.
- `GET /api/chronos/runs`: available runs and processing state.
- `GET /api/chronos/runs/{run_id}`: chart points, scores, and events for one run.
- `frontend/src/ChronosView.tsx`: polling, run selector, plots, event states and scores.
- `tests/test_chronos_detector.py`: trigger, recovery, missing-data and boundary tests.

## Limits and next deployment step

Thresholds use validation data that also informed model selection; May examples
have already been inspected. Results validate software behavior, not independent
incident-detection accuracy. No labeled incidents establish precision or recall.

To run on a live feed, provide a reliable five-minute ingestion watermark (including
late-arrival policy), a durable worker scheduler/restart policy, and reviewed
alert/recovery thresholds. Historical timestamp availability does not establish
when those data would actually have arrived operationally. Keep this distinction
visible until live ingestion and operational validation are completed.

### Source status confirmed November 13, 2026

The user confirmed that **no current feed exists**; only the historical dataset is
available. The intended current mode is therefore historical anomaly detection.
There is no pending live-feed deployment. The source contains 438,339 volume rows
from February 2 through May 18, 2026. The read-only source audit and before/after
hashes found no changes to the seven checked stores. Evidence is in
`../artifacts/reports/chronos_live_readiness/`.

To repeat the source check without rebuilding anything:

```bash
experiments/bee_ridge_forecasting/.venv/bin/python \
  anomaly-dashboard/scripts/check_chronos_source.py
```

Run this command from the project root. It outputs JSON to stdout and never writes
to the source. Its validity checks do not imply full time coverage or live readiness.

### Corridor expansion

The Chronos tab now has active intersection, phase, and review-date selectors.
Validated corridor-v1 runs cover all 13 available signals (1256 absent), phases 2
and 6, on May 2, May 11, and May 18. The first two days are full days; May 18
primary buckets end at 11:40, with the remaining forecast horizons scored through
11:50. This is selected-date coverage, not every day in the historical dataset.

`run_chronos_corridor.py --store <separate-staging.sqlite>` batches independent
series at each replay clock with `cross_learning=False`. Inputs are explicitly
limited to permitted historical timestamps; the future current-volume channel is
masked. The worker writes to staging, not the active dashboard store.
`validate_chronos_expansion.py` verifies the staged runs; its `--publish` option
backs up the existing Chronos database and appends results in a single transaction.
Existing run IDs are never overwritten. A publication cannot simply be rerun with
the same IDs; preserve the backup and inspect any failure first.

The 78 new runs contain 18,616 primary buckets, 15,911 scored buckets, 2,705 unscored
buckets, and 67 candidate events, including four unresolved at replay end. The
original two runs remain available. Missing data and thresholds are unchanged.
See `../artifacts/reports/chronos_corridor_expansion/validation.json`, `coverage.csv`,
and `events.csv` for details. These are candidate alerts, not confirmed incidents.

## One-hour prediction (November 18, 2026)

The default Chronos mode now accepts an intersection, phase, date and one complete
clock-hour. Click **Run prediction** for exactly three future five-minute buckets.
Incomplete input hours are disabled; missing weekly values remain NaN. Historical
training/validation dates are selectable but explicitly labelled as not held-out.
Missing future observations do not prevent prediction, but cannot be scored.

A separate GPU worker keeps inference out of the dashboard API process:

```bash
cd "/blue/ranka/rajavaram.s/ATSPM Dashboard"
bash anomaly-dashboard/scripts/start_chronos_worker.sh YOUR_GPU_JOB_ID
```

Keep this command running inside the lifetime of an active GPU allocation. It needs
4 CPUs, 12 GB RAM and one GPU. The job ID changes with allocations. A second worker
is prevented by an exclusive lock. The ordinary dashboard restart script does not
start or stop this worker. If the worker is offline, the UI explains this; cached
completed requests and saved replay review remain accessible. A restarted worker
requeues interrupted running requests.

The API writes only to `data/chronos_requests/`; all source DuckDB connections are
read-only. Cache keys include input data, source version, model file versions and
calibration checksum. Identical submissions reuse the result. The GPU worker writes
`predictions.json` atomically before calling the function that reads future actuals.
The input payload has no future actual-volume field. API endpoints:

- GET `/api/chronos/options` (series, dates, complete-hour availability)
- POST `/api/chronos/predict` with `signal`, `phase`, `day`, `hour`
- GET `/api/chronos/predict/{id}` (queued/running/complete/failed and result)

One request uses a single forecast origin, so each horizon uses its corresponding
validation residual cutoff. This differs from the saved replay mode, which repeatedly
predicts at five-minute origins and uses next-bucket alerts. Two consecutive low
forecast buckets trigger a candidate. Recovery cannot be established with only three
buckets after a two-bucket trigger; such events are unresolved within this window.
No detection claims are made for the input hour itself. The full 80 saved replay runs
remain under **Saved replay review**. Other tabs and detection methods are unchanged.

A browser request is not an unattended production monitoring service. Source timezones
are still unverified, and historical candidates are not confirmed traffic incidents.
