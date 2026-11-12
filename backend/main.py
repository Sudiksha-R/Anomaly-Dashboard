from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware


APP_ROOT = Path(__file__).resolve().parents[1]
STORE_PATH = Path(os.environ.get("ANOMALY_STORE_PATH", APP_ROOT / "data" / "anomaly.duckdb"))
MOE_STORE_PATH = Path(os.environ.get("MOE_ANALYTICS_STORE_PATH", APP_ROOT / "data" / "moe_analytics.duckdb"))
INTERRUPTION_STORE_PATH = Path(
    os.environ.get("INTERRUPTION_STORE_PATH", APP_ROOT / "data" / "interruption_events.duckdb")
)
INTERRUPTION_5MIN_STORE_PATH = Path(
    os.environ.get("INTERRUPTION_5MIN_STORE_PATH", APP_ROOT / "data" / "interruption_events_5min.duckdb")
)
DIRECTIONS = {"Eastbound", "Westbound"}
MOE_GRAINS = {"15min", "hourly"}

app = FastAPI(title="Bee Ridge Traffic Anomaly API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def connection() -> duckdb.DuckDBPyConnection:
    if not STORE_PATH.exists():
        raise HTTPException(status_code=503, detail="Anomaly store has not been built yet.")
    return duckdb.connect(str(STORE_PATH), read_only=True)


def moe_connection() -> duckdb.DuckDBPyConnection:
    if not MOE_STORE_PATH.exists():
        raise HTTPException(status_code=503, detail="MOE analytics store has not been built yet.")
    return duckdb.connect(str(MOE_STORE_PATH), read_only=True)


def interruption_store_path(bucket_minutes: int) -> Path:
    if bucket_minutes == 5:
        return INTERRUPTION_5MIN_STORE_PATH
    if bucket_minutes == 15:
        return INTERRUPTION_STORE_PATH
    raise HTTPException(status_code=400, detail="EOI bucket size must be 5 or 15 minutes.")


def interruption_connection(bucket_minutes: int = 15) -> duckdb.DuckDBPyConnection:
    store_path = interruption_store_path(bucket_minutes)
    if not store_path.exists():
        raise HTTPException(status_code=503, detail=f"The {bucket_minutes}-minute EOI store has not been built yet.")
    return duckdb.connect(str(store_path), read_only=True)


def clean_value(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (list, tuple)):
        return [clean_value(item) for item in value]
    if hasattr(value, "tolist"):
        listed = value.tolist()
        if isinstance(listed, list):
            return [clean_value(item) for item in listed]
        value = listed
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [{key: clean_value(value) for key, value in row.items()} for row in frame.to_dict(orient="records")]


def direction_filter(direction: str | None) -> str | None:
    if direction is None or direction == "All":
        return None
    if direction not in DIRECTIONS:
        raise HTTPException(status_code=400, detail="Direction must be Eastbound or Westbound.")
    return direction


def intersection_filter(intersection_ids: str | None) -> list[str] | None:
    if intersection_ids is None:
        return None
    if intersection_ids == "__none__":
        return []
    return sorted({signal_id.strip() for signal_id in intersection_ids.split(",") if signal_id.strip()})


def date_bounds(metadata: dict[str, str], date_from: str | None, date_to: str | None) -> tuple[str, str]:
    start = date_from or metadata["date_from"]
    end = date_to or metadata["date_to"]
    if len(start) == 10:
        start = f"{start}T00:00:00"
    if len(end) == 10:
        end = f"{end}T23:59:59.999999"
    return start, end


def moe_grain_filter(grain: str) -> str:
    if grain not in MOE_GRAINS:
        raise HTTPException(status_code=400, detail="Grain must be 15min or hourly.")
    return grain


def moe_table_for_grain(grain: str) -> str:
    selected = moe_grain_filter(grain)
    return "moe_observations_15min" if selected == "15min" else "moe_observations_hourly"


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": STORE_PATH.exists(),
        "store": str(STORE_PATH),
        "moeStoreOk": MOE_STORE_PATH.exists(),
        "moeStore": str(MOE_STORE_PATH),
        "interruptionStoreOk": INTERRUPTION_STORE_PATH.exists(),
        "interruptionStore": str(INTERRUPTION_STORE_PATH),
        "interruption5minStoreOk": INTERRUPTION_5MIN_STORE_PATH.exists(),
        "interruption5minStore": str(INTERRUPTION_5MIN_STORE_PATH),
    }


@app.get("/api/interruptions/meta")
def interruption_meta(bucket_minutes: int = Query(15, alias="bucketMinutes")) -> dict[str, Any]:
    with interruption_connection(bucket_minutes) as conn:
        metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
        bounds = conn.execute(
            "SELECT MIN(timestamp), MAX(timestamp), COUNT(*), COUNT(DISTINCT signal_id) FROM interruption_points"
        ).fetchone()
        intersections = conn.execute(
            "SELECT signal_id, name, lat, lon, route FROM intersections ORDER BY lon"
        ).fetchdf()
        dimensions = conn.execute(
            """
            SELECT LIST(DISTINCT direction ORDER BY direction) AS directions,
                   LIST(DISTINCT phase ORDER BY phase) AS phases
            FROM interruption_points
            """
        ).fetchdf()
    return {
        "metadata": metadata,
        "dateFrom": clean_value(bounds[0]),
        "dateTo": clean_value(bounds[1]),
        "pointCount": bounds[2],
        "signalCount": bounds[3],
        "intersections": records(intersections),
        "dimensions": records(dimensions)[0],
        "bucketMinutes": bucket_minutes,
        "availableBuckets": [
            minutes
            for minutes, path in ((5, INTERRUPTION_5MIN_STORE_PATH), (15, INTERRUPTION_STORE_PATH))
            if path.exists()
        ],
    }


@app.get("/api/interruptions/events")
def interruption_events(
    date_from: str | None = Query(None, alias="dateFrom"),
    date_to: str | None = Query(None, alias="dateTo"),
    direction: str | None = None,
    phase: int | None = Query(None, ge=1, le=16),
    bucket_minutes: int = Query(15, alias="bucketMinutes"),
    min_duration: int = Query(30, alias="minDuration", ge=5, le=1440),
    min_reduction: float = Query(30, alias="minReduction", ge=0, le=100),
    intersection_ids: str | None = Query(None, alias="intersectionIds"),
    limit: int = Query(250, ge=1, le=1000),
) -> dict[str, Any]:
    selected_direction = direction_filter(direction)
    selected_intersections = intersection_filter(intersection_ids)
    with interruption_connection(bucket_minutes) as conn:
        bounds = conn.execute("SELECT MIN(start_time), MAX(end_time) FROM interruption_events").fetchone()
        start, end = date_bounds(
            {
                "date_from": bounds[0].isoformat() if bounds[0] else "1970-01-01T00:00:00",
                "date_to": bounds[1].isoformat() if bounds[1] else "1970-01-01T00:00:00",
            },
            date_from,
            date_to,
        )
        parameters = [
            start,
            end,
            selected_direction,
            selected_direction,
            phase,
            phase,
            min_duration,
            min_reduction,
            selected_intersections,
            selected_intersections,
        ]
        where_sql = """
            end_time >= CAST(? AS TIMESTAMP)
            AND start_time <= CAST(? AS TIMESTAMP)
            AND (? IS NULL OR direction = ?)
            AND (? IS NULL OR phase = ?)
            AND duration_minutes >= ?
            AND avg_reduction_pct >= ?
            AND (CAST(? AS VARCHAR[]) IS NULL OR list_contains(CAST(? AS VARCHAR[]), signal_id))
        """
        summary = conn.execute(
            f"""
            SELECT COUNT(*) AS event_count,
                   COUNT(DISTINCT signal_id) AS affected_intersections,
                   MAX(duration_minutes) AS longest_duration_minutes,
                   MAX(avg_reduction_pct) AS largest_reduction_pct
            FROM interruption_events
            WHERE {where_sql}
            """,
            parameters,
        ).fetchdf()
        frame = conn.execute(
            f"""
            SELECT event_id, signal_id, name, phase, direction, start_time, end_time,
                   duration_minutes, point_count, raw_observed_volume, observed_volume,
                   expected_volume, volume_delta, avg_reduction_pct,
                   max_reduction_pct, min_history_days, severity
            FROM interruption_events
            WHERE {where_sql}
            ORDER BY avg_reduction_pct DESC, duration_minutes DESC, start_time DESC
            LIMIT ?
            """,
            [*parameters, limit],
        ).fetchdf()
    return {
        "filters": {
            "dateFrom": start,
            "dateTo": end,
            "direction": selected_direction or "All",
            "phase": phase,
            "minDuration": min_duration,
            "minReduction": min_reduction,
            "bucketMinutes": bucket_minutes,
        },
        "summary": records(summary)[0],
        "items": records(frame),
        "count": len(frame),
    }


@app.get("/api/interruptions/timeline/{event_id}")
def interruption_timeline(
    event_id: str,
    bucket_minutes: int = Query(15, alias="bucketMinutes"),
) -> dict[str, Any]:
    with interruption_connection(bucket_minutes) as conn:
        event_frame = conn.execute(
            """
            SELECT event_id, signal_id, name, phase, direction, start_time, end_time,
                   duration_minutes, point_count, raw_observed_volume, observed_volume,
                   expected_volume, volume_delta, avg_reduction_pct,
                   max_reduction_pct, min_history_days, severity
            FROM interruption_events
            WHERE event_id = ?
            """,
            [event_id],
        ).fetchdf()
        if event_frame.empty:
            raise HTTPException(status_code=404, detail="Interruption event was not found.")
        event = records(event_frame)[0]
        points = conn.execute(
            """
            SELECT timestamp, signal_id, phase, direction, raw_volume,
                   observed_volume, expected_volume, volume_delta,
                   reduction_pct, history_days, is_drop,
                   timestamp >= CAST(? AS TIMESTAMP)
                       AND timestamp < CAST(? AS TIMESTAMP) AS is_event
            FROM interruption_points
            WHERE signal_id = ?
              AND phase = ?
              AND timestamp BETWEEN CAST(? AS TIMESTAMP) - INTERVAL '2 hours'
                                AND CAST(? AS TIMESTAMP) + INTERVAL '2 hours'
            ORDER BY timestamp
            """,
            [
                event["start_time"],
                event["end_time"],
                event["signal_id"],
                event["phase"],
                event["start_time"],
                event["end_time"],
            ],
        ).fetchdf()
    return {"event": event, "points": records(points)}


@app.get("/api/moe/meta")
def moe_meta() -> dict[str, Any]:
    with moe_connection() as conn:
        metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
        summary = conn.execute("SELECT * FROM store_summary ORDER BY grain").fetchdf()
        metrics = conn.execute("SELECT * FROM metric_definitions ORDER BY metric").fetchdf()
        intersections = conn.execute("SELECT signal_id, name, lat, lon, route FROM intersections ORDER BY lon").fetchdf()
        dimensions = conn.execute(
            """
            SELECT LIST(DISTINCT grain ORDER BY grain) AS grains,
                   LIST(DISTINCT direction ORDER BY direction) AS directions,
                   LIST(DISTINCT phase ORDER BY phase) FILTER (WHERE phase IS NOT NULL) AS phases
            FROM moe_observations
            """
        ).fetchdf()
    return {
        "metadata": metadata,
        "summary": records(summary),
        "metrics": records(metrics),
        "intersections": records(intersections),
        "dimensions": records(dimensions)[0],
    }


@app.get("/api/moe/observations")
def moe_observations(
    metric: str,
    grain: str = "hourly",
    date_from: str | None = Query(None, alias="dateFrom"),
    date_to: str | None = Query(None, alias="dateTo"),
    signal_id: str | None = Query(None, alias="signalId"),
    direction: str | None = None,
    phase: int | None = None,
    limit: int = Query(1000, ge=1, le=5000),
) -> dict[str, Any]:
    table = moe_table_for_grain(grain)
    with moe_connection() as conn:
        metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
        bounds = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM moe_observations").fetchone()
        start, end = date_bounds(
            {
                "date_from": bounds[0].isoformat() if bounds[0] else metadata.get("date_from", "1970-01-01T00:00:00"),
                "date_to": bounds[1].isoformat() if bounds[1] else metadata.get("date_to", "1970-01-01T00:00:00"),
            },
            date_from,
            date_to,
        )
        exists = conn.execute(
            "SELECT 1 FROM metric_definitions WHERE metric = ?",
            [metric],
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=400, detail="Unknown MOE metric.")
        frame = conn.execute(
            f"""
            SELECT timestamp, date, hour, minute, grain, signal_id, phase, direction,
                   source_table, metric, value
                   {', sample_count' if table == 'moe_observations_hourly' else ', 1 AS sample_count'}
            FROM {table}
            WHERE metric = ?
              AND timestamp BETWEEN CAST(? AS TIMESTAMP) AND CAST(? AS TIMESTAMP)
              AND (? IS NULL OR signal_id = ?)
              AND (? IS NULL OR direction = ?)
              AND (? IS NULL OR phase = ?)
            ORDER BY timestamp, TRY_CAST(signal_id AS INTEGER), phase
            LIMIT ?
            """,
            [metric, start, end, signal_id, signal_id, direction, direction, phase, phase, limit],
        ).fetchdf()
    return {
        "filters": {
            "metric": metric,
            "grain": grain,
            "dateFrom": start,
            "dateTo": end,
            "signalId": signal_id,
            "direction": direction,
            "phase": phase,
            "limit": limit,
        },
        "items": records(frame),
        "count": len(frame),
    }


@app.get("/api/moe/daily-summary")
def moe_daily_summary(
    metric: str,
    grain: str = "hourly",
    date_from: str | None = Query(None, alias="dateFrom"),
    date_to: str | None = Query(None, alias="dateTo"),
    signal_id: str | None = Query(None, alias="signalId"),
    direction: str | None = None,
    phase: int | None = None,
    limit: int = Query(1000, ge=1, le=5000),
) -> dict[str, Any]:
    selected_grain = moe_grain_filter(grain)
    with moe_connection() as conn:
        bounds = conn.execute("SELECT MIN(date), MAX(date) FROM moe_daily_summary").fetchone()
        start = date_from or (bounds[0].isoformat() if bounds[0] else "1970-01-01")
        end = date_to or (bounds[1].isoformat() if bounds[1] else "1970-01-01")
        exists = conn.execute(
            "SELECT 1 FROM metric_definitions WHERE metric = ?",
            [metric],
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=400, detail="Unknown MOE metric.")
        frame = conn.execute(
            """
            SELECT date, grain, signal_id, phase, direction, metric,
                   avg_value, min_value, max_value, point_count
            FROM moe_daily_summary
            WHERE metric = ?
              AND grain = ?
              AND date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
              AND (? IS NULL OR signal_id = ?)
              AND (? IS NULL OR direction = ?)
              AND (? IS NULL OR phase = ?)
            ORDER BY date, TRY_CAST(signal_id AS INTEGER), phase
            LIMIT ?
            """,
            [metric, selected_grain, start, end, signal_id, signal_id, direction, direction, phase, phase, limit],
        ).fetchdf()
    return {
        "filters": {
            "metric": metric,
            "grain": selected_grain,
            "dateFrom": start,
            "dateTo": end,
            "signalId": signal_id,
            "direction": direction,
            "phase": phase,
            "limit": limit,
        },
        "items": records(frame),
        "count": len(frame),
    }


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    with connection() as conn:
        metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
        entities = conn.execute(
            """
            SELECT DISTINCT entity_id, signal_id, name, phase, direction, metric
            FROM anomalies
            ORDER BY direction, TRY_CAST(signal_id AS INTEGER)
            """
        ).fetchdf()
        intersections = conn.execute(
            "SELECT signal_id, name FROM intersections ORDER BY lon"
        ).fetchdf()
    return {
        "metadata": metadata,
        "entities": records(entities),
        "intersections": records(intersections),
        "directions": sorted(DIRECTIONS),
    }


@app.get("/api/overview")
def overview(
    date_from: str | None = Query(None, alias="dateFrom"),
    date_to: str | None = Query(None, alias="dateTo"),
    direction: str | None = None,
    geh_threshold: float = Query(3.5, alias="gehThreshold", ge=0.1, le=50),
    intersection_ids: str | None = Query(None, alias="intersectionIds"),
) -> dict[str, Any]:
    selected_direction = direction_filter(direction)
    selected_intersections = intersection_filter(intersection_ids)
    with connection() as conn:
        metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
        start, end = date_bounds(metadata, date_from, date_to)
        rows = conn.execute(
            """
            WITH settings AS (
                SELECT CAST(? AS DOUBLE) AS threshold
            ), filtered AS (
                SELECT a.*, s.threshold
                FROM anomalies a CROSS JOIN settings s
                WHERE timestamp BETWEEN CAST(? AS TIMESTAMP) AND CAST(? AS TIMESTAMP)
                  AND (? IS NULL OR direction = ?)
                  AND (CAST(? AS VARCHAR[]) IS NULL OR list_contains(CAST(? AS VARCHAR[]), signal_id))
            ), status AS (
                SELECT signal_id,
                       COUNT(*) AS modeled_points,
                       SUM(CASE WHEN abs_score > threshold THEN 1 ELSE 0 END) AS anomaly_count,
                       MAX(abs_score) AS max_score,
                       MAX(CASE WHEN abs_score > threshold THEN timestamp END) AS latest_anomaly,
                       COALESCE(
                           to_json(list_slice(
                               LIST(struct_pack(
                                   event_time := strftime(timestamp, '%Y-%m-%dT%H:%M:%S'),
                                   event_direction := direction,
                                   geh := abs_score
                               ) ORDER BY timestamp DESC) FILTER (WHERE abs_score > threshold),
                               1,
                               10
                           )),
                           '[]'
                       ) AS recent_events_json,
                       CASE
                           WHEN MAX(abs_score) <= MAX(threshold) THEN 'Normal'
                           WHEN MAX(abs_score) <= MAX(threshold) + 1.5 THEN 'Watch'
                           WHEN MAX(abs_score) <= MAX(threshold) + 3.5 THEN 'High'
                           ELSE 'Critical'
                       END AS severity
                FROM filtered
                GROUP BY signal_id, threshold
            )
            SELECT i.signal_id, i.name, i.lat, i.lon, i.route,
                   COALESCE(s.modeled_points, 0) AS modeled_points,
                   COALESCE(s.anomaly_count, 0) AS anomaly_count,
                   s.max_score, s.latest_anomaly, COALESCE(s.recent_events_json, '[]') AS recent_events_json,
                   CASE WHEN s.modeled_points IS NULL THEN 'No data' ELSE COALESCE(s.severity, 'Normal') END AS severity
            FROM intersections i
            LEFT JOIN status s USING (signal_id)
            WHERE (CAST(? AS VARCHAR[]) IS NULL OR list_contains(CAST(? AS VARCHAR[]), i.signal_id))
            ORDER BY i.lon
            """,
            [
                geh_threshold, start, end,
                selected_direction, selected_direction,
                selected_intersections, selected_intersections,
                selected_intersections, selected_intersections,
            ],
        ).fetchdf()
        summary = conn.execute(
            """
            WITH settings AS (
                SELECT CAST(? AS DOUBLE) AS threshold
            )
            SELECT COUNT(*) AS modeled_points,
                   COUNT(DISTINCT signal_id) AS modeled_intersections,
                   COALESCE(SUM(CASE WHEN abs_score > threshold THEN 1 ELSE 0 END), 0) AS anomaly_count,
                   MAX(abs_score) AS max_score
            FROM anomalies CROSS JOIN settings
            WHERE timestamp BETWEEN CAST(? AS TIMESTAMP) AND CAST(? AS TIMESTAMP)
              AND (? IS NULL OR direction = ?)
              AND (CAST(? AS VARCHAR[]) IS NULL OR list_contains(CAST(? AS VARCHAR[]), signal_id))
            """,
            [
                geh_threshold, start, end,
                selected_direction, selected_direction,
                selected_intersections, selected_intersections,
            ],
        ).fetchdf()
    intersection_rows = records(rows)
    for row in intersection_rows:
        row["recent_events"] = json.loads(row.pop("recent_events_json"))
    return {
        "filters": {
            "dateFrom": start,
            "dateTo": end,
            "direction": selected_direction or "All",
            "gehThreshold": geh_threshold,
        },
        "summary": records(summary)[0],
        "intersections": intersection_rows,
    }


@app.get("/api/anomalies")
def anomalies(
    date_from: str | None = Query(None, alias="dateFrom"),
    date_to: str | None = Query(None, alias="dateTo"),
    direction: str | None = None,
    signal_id: str | None = Query(None, alias="signalId"),
    limit: int = Query(100, ge=1, le=500),
    geh_threshold: float = Query(3.5, alias="gehThreshold", ge=0.1, le=50),
    intersection_ids: str | None = Query(None, alias="intersectionIds"),
) -> dict[str, Any]:
    selected_direction = direction_filter(direction)
    selected_intersections = intersection_filter(intersection_ids)
    with connection() as conn:
        metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
        start, end = date_bounds(metadata, date_from, date_to)
        frame = conn.execute(
            """
            WITH settings AS (
                SELECT CAST(? AS DOUBLE) AS threshold
            )
            SELECT entity_id, signal_id, name, phase, direction, metric, timestamp,
                   value, prediction, deviation, deviation_pct, geh_score, abs_score,
                   abs_score > threshold AS entity_anomaly,
                   CASE
                       WHEN abs_score <= threshold THEN 'Normal'
                       WHEN abs_score <= threshold + 1.5 THEN 'Watch'
                       WHEN abs_score <= threshold + 3.5 THEN 'High'
                       ELSE 'Critical'
                   END AS severity
            FROM anomalies CROSS JOIN settings
            WHERE timestamp BETWEEN CAST(? AS TIMESTAMP) AND CAST(? AS TIMESTAMP)
              AND abs_score > threshold
              AND (? IS NULL OR direction = ?)
              AND (? IS NULL OR signal_id = ?)
              AND (CAST(? AS VARCHAR[]) IS NULL OR list_contains(CAST(? AS VARCHAR[]), signal_id))
            ORDER BY abs_score DESC, timestamp DESC
            LIMIT ?
            """,
            [
                geh_threshold, start, end,
                selected_direction, selected_direction, signal_id, signal_id,
                selected_intersections, selected_intersections, limit,
            ],
        ).fetchdf()
    return {"items": records(frame), "count": len(frame)}


@app.get("/api/timeline/{entity_id}")
def timeline(
    entity_id: str,
    date_from: str | None = Query(None, alias="dateFrom"),
    date_to: str | None = Query(None, alias="dateTo"),
    geh_threshold: float = Query(3.5, alias="gehThreshold", ge=0.1, le=50),
) -> dict[str, Any]:
    with connection() as conn:
        metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
        start, end = date_bounds(metadata, date_from, date_to)
        frame = conn.execute(
            """
            WITH settings AS (
                SELECT CAST(? AS DOUBLE) AS threshold
            )
            SELECT entity_id, signal_id, name, phase, direction, metric, timestamp,
                   value, prediction, deviation, deviation_pct, geh_score, abs_score,
                   abs_score > threshold AS entity_anomaly,
                   CASE
                       WHEN abs_score <= threshold THEN 'Normal'
                       WHEN abs_score <= threshold + 1.5 THEN 'Watch'
                       WHEN abs_score <= threshold + 3.5 THEN 'High'
                       ELSE 'Critical'
                   END AS severity
            FROM anomalies CROSS JOIN settings
            WHERE entity_id = ?
              AND timestamp BETWEEN CAST(? AS TIMESTAMP) AND CAST(? AS TIMESTAMP)
            ORDER BY timestamp
            LIMIT 3000
            """,
            [geh_threshold, entity_id, start, end],
        ).fetchdf()
    if frame.empty:
        raise HTTPException(status_code=404, detail="No modeled timeline was found for that entity and date range.")
    return {"entity": records(frame.head(1))[0], "points": records(frame)}


@app.get("/api/changepoints")
def changepoints_endpoint(
    direction: str | None = None,
    signal_id: str | None = Query(None, alias="signalId"),
    intersection_ids: str | None = Query(None, alias="intersectionIds"),
) -> dict[str, Any]:
    selected_direction = direction_filter(direction)
    selected_intersections = intersection_filter(intersection_ids)
    with connection() as conn:
        frame = conn.execute(
            """
            SELECT entity_id, signal_id, name, phase, direction, metric, timestamp,
                   score, avg_before, avg_after, avg_diff, pct_change
            FROM changepoints
            WHERE (? IS NULL OR direction = ?)
              AND (? IS NULL OR signal_id = ?)
              AND (CAST(? AS VARCHAR[]) IS NULL OR list_contains(CAST(? AS VARCHAR[]), signal_id))
            ORDER BY ABS(pct_change) DESC, timestamp DESC
            LIMIT 250
            """,
            [
                selected_direction, selected_direction, signal_id, signal_id,
                selected_intersections, selected_intersections,
            ],
        ).fetchdf()
    return {"items": records(frame), "count": len(frame)}
