from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from traffic_anomaly import anomaly, changepoint, decompose


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = Path(__file__).resolve().parents[1]
DERIVED_ROOT = PROJECT_ROOT / "derived"
REFERENCE_DB = DERIVED_ROOT / "reference_data.sqlite"
DEFAULT_OUTPUT = APP_ROOT / "data" / "anomaly.duckdb"

PHASE_DIRECTIONS = {2: "Eastbound", 6: "Westbound"}
ROLLING_WINDOW_DAYS = 42
ENTITY_THRESHOLD = 3.5
CHANGEPOINT_THRESHOLD = 1.0


def load_intersections() -> pd.DataFrame:
    query = """
        SELECT CAST(DeviceId AS VARCHAR) AS signal_id,
               name,
               latitude AS lat,
               longitude AS lon,
               route
        FROM bee_ridge_intersections
        WHERE DeviceId IS NOT NULL
          AND latitude IS NOT NULL
          AND longitude IS NOT NULL
        ORDER BY longitude
    """
    with sqlite3.connect(REFERENCE_DB) as conn:
        frame = pd.read_sql_query(query, conn)
    return frame.drop_duplicates("signal_id", keep="first").reset_index(drop=True)


def has_arrival_on_green(path: Path) -> bool:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'arrival_on_green'"
        ).fetchone()
    return row is not None


def historical_db(signal_id: str) -> Path | None:
    candidates = sorted(DERIVED_ROOT.glob(f"dask_historical_{signal_id}_all_days_*/atspm_hourly_results.sqlite"))
    valid = [path for path in candidates if has_arrival_on_green(path)]
    return max(valid, key=lambda path: path.stat().st_size) if valid else None


def load_hourly_volume(intersections: pd.DataFrame) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for signal_id in intersections["signal_id"]:
        path = historical_db(str(signal_id))
        if path is None:
            print(f"Skipping signal {signal_id}: no arrival_on_green history is available")
            continue
        query = """
            SELECT run_date,
                   hour,
                   CAST(DeviceId AS VARCHAR) AS signal_id,
                   Phase AS phase,
                   SUM(Total_Actuations) AS value
            FROM arrival_on_green
            WHERE Phase IN (2, 6)
            GROUP BY run_date, hour, DeviceId, Phase
            ORDER BY run_date, hour, Phase
        """
        with sqlite3.connect(path) as conn:
            frames.append(pd.read_sql_query(query, conn))

    observations = pd.concat(frames, ignore_index=True)
    observations["timestamp"] = pd.to_datetime(observations["run_date"]) + pd.to_timedelta(observations["hour"], unit="h")
    observations["phase"] = observations["phase"].astype(int)
    observations["value"] = pd.to_numeric(observations["value"], errors="coerce")
    observations = observations.dropna(subset=["timestamp", "value"])
    observations["direction"] = observations["phase"].map(PHASE_DIRECTIONS)
    observations["entity_id"] = observations["signal_id"] + "-p" + observations["phase"].astype(str)
    observations["metric"] = "volume"
    observations = observations.merge(intersections[["signal_id", "name"]], on="signal_id", how="left")
    return observations[
        ["timestamp", "entity_id", "signal_id", "name", "phase", "direction", "metric", "value"]
    ].sort_values(["entity_id", "timestamp"]).reset_index(drop=True)


def run_anomaly_model(observations: pd.DataFrame) -> pd.DataFrame:
    decomposed = decompose(
        data=observations,
        datetime_column="timestamp",
        value_column="value",
        entity_grouping_columns=["entity_id"],
        freq_minutes=60,
        rolling_window_days=ROLLING_WINDOW_DAYS,
        drop_days=ROLLING_WINDOW_DAYS,
        min_rolling_window_samples=168,
        min_time_of_day_samples=5,
        drop_extras=False,
    )

    entity_result = anomaly(
        decomposed_data=decomposed,
        datetime_column="timestamp",
        value_column="value",
        entity_grouping_columns=["entity_id"],
        entity_threshold=ENTITY_THRESHOLD,
        GEH=True,
    ).rename(columns={"anomaly": "entity_anomaly", "resid": "geh_score"})

    result = entity_result
    result["entity_anomaly"] = result["entity_anomaly"].fillna(False).astype(bool)
    result["abs_score"] = result["geh_score"].abs()
    result["deviation"] = result["value"] - result["prediction"]
    result["deviation_pct"] = np.where(
        result["prediction"].abs() > 1e-8,
        result["deviation"] / result["prediction"] * 100,
        np.nan,
    )
    result["severity"] = pd.cut(
        result["abs_score"],
        bins=[-np.inf, ENTITY_THRESHOLD, 5.0, 7.0, np.inf],
        labels=["Normal", "Watch", "High", "Critical"],
    ).astype(str)
    return result.sort_values(["timestamp", "entity_id"]).reset_index(drop=True)


def run_changepoint_model(observations: pd.DataFrame) -> pd.DataFrame:
    result = changepoint(
        data=observations,
        datetime_column="timestamp",
        value_column="value",
        entity_grouping_column="entity_id",
        rolling_window_days=14,
        robust=False,
        score_threshold=CHANGEPOINT_THRESHOLD,
        min_separation_days=3,
        min_samples=30,
    )
    metadata = observations[["entity_id", "signal_id", "name", "phase", "direction", "metric"]].drop_duplicates()
    return result.merge(metadata, on="entity_id", how="left").sort_values("timestamp", ascending=False).reset_index(drop=True)


def write_store(
    output: Path,
    intersections: pd.DataFrame,
    observations: pd.DataFrame,
    anomalies: pd.DataFrame,
    changepoints: pd.DataFrame,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    metadata = pd.DataFrame(
        [
            {"key": "metric", "value": "volume"},
            {"key": "rolling_window_days", "value": str(ROLLING_WINDOW_DAYS)},
            {"key": "entity_threshold", "value": str(ENTITY_THRESHOLD)},
            {"key": "changepoint_threshold", "value": str(CHANGEPOINT_THRESHOLD)},
            {"key": "generated_at", "value": pd.Timestamp.now(tz="UTC").isoformat()},
            {"key": "date_from", "value": observations["timestamp"].min().isoformat()},
            {"key": "date_to", "value": observations["timestamp"].max().isoformat()},
        ]
    )
    with duckdb.connect(str(output)) as conn:
        conn.register("intersections_frame", intersections)
        conn.register("observations_frame", observations)
        conn.register("anomalies_frame", anomalies)
        conn.register("changepoints_frame", changepoints)
        conn.register("metadata_frame", metadata)
        conn.execute("CREATE TABLE intersections AS SELECT * FROM intersections_frame")
        conn.execute("CREATE TABLE observations AS SELECT * FROM observations_frame")
        conn.execute("CREATE TABLE anomalies AS SELECT * FROM anomalies_frame")
        conn.execute("CREATE TABLE changepoints AS SELECT * FROM changepoints_frame")
        conn.execute("CREATE TABLE metadata AS SELECT * FROM metadata_frame")
        conn.execute("CREATE INDEX anomaly_entity_time ON anomalies(entity_id, timestamp)")
        conn.execute("CREATE INDEX anomaly_time ON anomalies(timestamp)")
        conn.execute("CREATE INDEX changepoint_entity_time ON changepoints(entity_id, timestamp)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Bee Ridge traffic anomaly analytical store.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    intersections = load_intersections()
    observations = load_hourly_volume(intersections)
    anomalies = run_anomaly_model(observations)
    changepoints = run_changepoint_model(observations)
    write_store(args.output, intersections, observations, anomalies, changepoints)

    summary = {
        "output": str(args.output),
        "intersections": int(len(intersections)),
        "entities": int(observations["entity_id"].nunique()),
        "observations": int(len(observations)),
        "modeled_observations": int(len(anomalies)),
        "entity_anomalies": int(anomalies["entity_anomaly"].sum()),
        "changepoints": int(len(changepoints)),
        "date_from": observations["timestamp"].min().isoformat(),
        "date_to": observations["timestamp"].max().isoformat(),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
