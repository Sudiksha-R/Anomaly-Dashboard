from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = Path(__file__).resolve().parents[1]
DERIVED_ROOT = PROJECT_ROOT / "derived"
REFERENCE_DB = DERIVED_ROOT / "reference_data.sqlite"
DEFAULT_OUTPUT = APP_ROOT / "data" / "moe_analytics.duckdb"

PHASE_DIRECTIONS = {2: "Eastbound", 6: "Westbound"}

METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "volume": {"label": "Volume", "unit": "vehicles", "rollup": "sum"},
    "aogPct": {"label": "AOG %", "unit": "percent", "rollup": "mean"},
    "aorPct": {"label": "AOR %", "unit": "percent", "rollup": "mean"},
    "avgWait": {"label": "Average Wait", "unit": "seconds", "rollup": "mean"},
    "maxWait": {"label": "Maximum Wait", "unit": "seconds", "rollup": "max"},
    "skips": {"label": "Skips", "unit": "count", "rollup": "sum"},
    "greenTime": {"label": "Green Time", "unit": "seconds", "rollup": "mean"},
    "greenOccupancyPct": {"label": "Green Occupancy %", "unit": "percent", "rollup": "mean"},
    "redOccupancyPct": {"label": "Red Occupancy %", "unit": "percent", "rollup": "mean"},
    "splitFailures": {"label": "Split Failures", "unit": "count", "rollup": "sum"},
    "gapOuts": {"label": "Gap-Outs", "unit": "count", "rollup": "sum"},
    "maxOuts": {"label": "Max-Outs", "unit": "count", "rollup": "sum"},
    "forceOffs": {"label": "Force-Offs", "unit": "count", "rollup": "sum"},
    "cycleLength": {"label": "Cycle Length", "unit": "seconds", "rollup": "mean"},
    "actualCycleLength": {"label": "Actual Cycle Length", "unit": "seconds", "rollup": "mean"},
    "actualOffset": {"label": "Actual Offset", "unit": "seconds", "rollup": "mean"},
}

ROLLUP_SQL = {
    "mean": "AVG(value)",
    "sum": "SUM(value)",
    "max": "MAX(value)",
}


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


def sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def historical_db(signal_id: str) -> Path | None:
    preferred = DERIVED_ROOT / f"dask_historical_{signal_id}_all_days_feb_to_may" / "atspm_hourly_results.sqlite"
    if preferred.exists():
        return preferred
    candidates = sorted(DERIVED_ROOT.glob(f"dask_historical_{signal_id}_all_days_*/atspm_hourly_results.sqlite"))
    return max(candidates, key=lambda path: path.stat().st_size) if candidates else None


def normalize_base(frame: pd.DataFrame, source_table: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    out = frame.copy()
    out["timestamp"] = pd.to_datetime(out["TimeStamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"])
    out["date"] = out["timestamp"].dt.date.astype(str)
    out["hour"] = out["timestamp"].dt.hour.astype("int16")
    out["minute"] = out["timestamp"].dt.minute.astype("int16")
    out["grain"] = "15min"
    out["source_table"] = source_table
    out["signal_id"] = out["DeviceId"].astype(str)
    if "Phase" in out.columns:
        out["phase"] = pd.to_numeric(out["Phase"], errors="coerce").astype("Int64")
        out["direction"] = out["phase"].map(PHASE_DIRECTIONS).fillna(out["phase"].map(lambda value: f"Phase {value}" if pd.notna(value) else "Signal"))
    else:
        out["phase"] = pd.Series(pd.NA, index=out.index, dtype="Int64")
        out["direction"] = "Signal"
    return out


def metric_frame(base: pd.DataFrame, metric: str, value: pd.Series) -> pd.DataFrame:
    out = base[["timestamp", "date", "hour", "minute", "grain", "signal_id", "phase", "direction", "source_table"]].copy()
    out["metric"] = metric
    out["value"] = pd.to_numeric(value, errors="coerce")
    out = out.dropna(subset=["value"])
    return out


def load_table(conn: sqlite3.Connection, table_name: str, columns: list[str]) -> pd.DataFrame:
    if not sqlite_table_exists(conn, table_name):
        return pd.DataFrame()
    cols = ", ".join(f'"{column}"' for column in columns)
    return pd.read_sql_query(f'SELECT {cols} FROM "{table_name}" WHERE TimeStamp IS NOT NULL', conn)


def observation_frames_for_db(path: Path) -> Iterable[pd.DataFrame]:
    with sqlite3.connect(path) as conn:
        aog = load_table(
            conn,
            "arrival_on_green",
            ["TimeStamp", "DeviceId", "Phase", "Total_Actuations", "Percent_AOG"],
        )
        if not aog.empty:
            base = normalize_base(aog, "arrival_on_green")
            yield metric_frame(base, "volume", base["Total_Actuations"])
            yield metric_frame(base, "aogPct", base["Percent_AOG"] * 100.0)
            yield metric_frame(base, "aorPct", 100.0 - base["Percent_AOG"] * 100.0)

        wait = load_table(
            conn,
            "phase_wait",
            ["TimeStamp", "DeviceId", "Phase", "AvgPhaseWait", "MaxPhaseWait", "TotalSkips"],
        )
        if not wait.empty:
            base = normalize_base(wait, "phase_wait")
            yield metric_frame(base, "avgWait", base["AvgPhaseWait"])
            yield metric_frame(base, "maxWait", base["MaxPhaseWait"])
            yield metric_frame(base, "skips", base["TotalSkips"])

        split = load_table(
            conn,
            "split_failures",
            ["TimeStamp", "DeviceId", "Phase", "Green_Time", "Green_Occupancy", "Red_Occupancy", "Split_Failure"],
        )
        if not split.empty:
            base = normalize_base(split, "split_failures")
            yield metric_frame(base, "greenTime", base["Green_Time"])
            yield metric_frame(base, "greenOccupancyPct", base["Green_Occupancy"] * 100.0)
            yield metric_frame(base, "redOccupancyPct", base["Red_Occupancy"] * 100.0)
            yield metric_frame(base, "splitFailures", base["Split_Failure"])

        terms = load_table(
            conn,
            "terminations",
            ["TimeStamp", "DeviceId", "Phase", "PerformanceMeasure", "Total"],
        )
        if not terms.empty:
            base = normalize_base(terms, "terminations")
            metric_by_measure = {
                "GapOut": "gapOuts",
                "MaxOut": "maxOuts",
                "ForceOff": "forceOffs",
            }
            for measure, metric in metric_by_measure.items():
                subset = base.loc[base["PerformanceMeasure"] == measure]
                if not subset.empty:
                    yield metric_frame(subset, metric, subset["Total"])

        coordination = load_table(
            conn,
            "coordination_agg",
            ["TimeStamp", "DeviceId", "Pattern", "CycleLength", "ActualCycleLength", "ActualOffset"],
        )
        if not coordination.empty:
            base = normalize_base(coordination, "coordination_agg")
            yield metric_frame(base, "cycleLength", base["CycleLength"])
            yield metric_frame(base, "actualCycleLength", base["ActualCycleLength"])
            yield metric_frame(base, "actualOffset", base["ActualOffset"])


def create_empty_store(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE moe_observations_15min (
            timestamp TIMESTAMP,
            date DATE,
            hour SMALLINT,
            minute SMALLINT,
            grain VARCHAR,
            signal_id VARCHAR,
            phase INTEGER,
            direction VARCHAR,
            source_table VARCHAR,
            metric VARCHAR,
            value DOUBLE
        )
        """
    )


def append_frame(conn: duckdb.DuckDBPyConnection, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    ordered = frame[
        [
            "timestamp",
            "date",
            "hour",
            "minute",
            "grain",
            "signal_id",
            "phase",
            "direction",
            "source_table",
            "metric",
            "value",
        ]
    ].copy()
    conn.register("incoming_observations", ordered)
    conn.execute("INSERT INTO moe_observations_15min SELECT * FROM incoming_observations")
    conn.unregister("incoming_observations")
    return int(len(ordered))


def write_dimension_tables(
    conn: duckdb.DuckDBPyConnection,
    intersections: pd.DataFrame,
    source_rows: list[dict[str, Any]],
    output: Path,
) -> None:
    metric_rows = pd.DataFrame(
        [
            {
                "metric": metric,
                "label": config["label"],
                "unit": config["unit"],
                "hourly_rollup": config["rollup"],
            }
            for metric, config in METRIC_DEFINITIONS.items()
        ]
    )
    metadata = pd.DataFrame(
        [
            {"key": "store_kind", "value": "moe_analytics"},
            {"key": "grain", "value": "15min,hourly"},
            {"key": "generated_at", "value": pd.Timestamp.now(tz="UTC").isoformat()},
            {"key": "output", "value": str(output)},
        ]
    )
    conn.register("intersections_frame", intersections)
    conn.register("metric_rows", metric_rows)
    conn.register("source_rows", pd.DataFrame(source_rows))
    conn.register("metadata_rows", metadata)
    conn.execute("CREATE TABLE intersections AS SELECT * FROM intersections_frame")
    conn.execute("CREATE TABLE metric_definitions AS SELECT * FROM metric_rows")
    conn.execute("CREATE TABLE source_databases AS SELECT * FROM source_rows")
    conn.execute("CREATE TABLE metadata AS SELECT * FROM metadata_rows")
    conn.unregister("intersections_frame")
    conn.unregister("metric_rows")
    conn.unregister("source_rows")
    conn.unregister("metadata_rows")


def create_hourly_rollup(conn: duckdb.DuckDBPyConnection) -> None:
    union_queries = []
    for rollup in sorted({config["rollup"] for config in METRIC_DEFINITIONS.values()}):
        metrics = [metric for metric, config in METRIC_DEFINITIONS.items() if config["rollup"] == rollup]
        metric_list = ", ".join(f"'{metric}'" for metric in metrics)
        union_queries.append(
            f"""
            SELECT date_trunc('hour', timestamp) AS timestamp,
                   CAST(date_trunc('hour', timestamp) AS DATE) AS date,
                   CAST(date_part('hour', timestamp) AS SMALLINT) AS hour,
                   CAST(0 AS SMALLINT) AS minute,
                   'hourly' AS grain,
                   signal_id,
                   phase,
                   direction,
                   source_table,
                   metric,
                   {ROLLUP_SQL[rollup]} AS value,
                   COUNT(*) AS sample_count
            FROM moe_observations_15min
            WHERE metric IN ({metric_list})
            GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
            """
        )
    conn.execute("CREATE TABLE moe_observations_hourly AS " + "\nUNION ALL\n".join(union_queries))


def create_summary_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE moe_daily_summary AS
        SELECT CAST(timestamp AS DATE) AS date,
               grain,
               signal_id,
               phase,
               direction,
               metric,
               AVG(value) AS avg_value,
               MIN(value) AS min_value,
               MAX(value) AS max_value,
               COUNT(*) AS point_count
        FROM (
            SELECT timestamp, grain, signal_id, phase, direction, metric, value FROM moe_observations_15min
            UNION ALL
            SELECT timestamp, grain, signal_id, phase, direction, metric, value FROM moe_observations_hourly
        )
        GROUP BY 1, 2, 3, 4, 5, 6
        """
    )
    conn.execute(
        """
        CREATE VIEW moe_observations AS
        SELECT timestamp, date, hour, minute, grain, signal_id, phase, direction, source_table, metric, value, 1 AS sample_count
        FROM moe_observations_15min
        UNION ALL
        SELECT timestamp, date, hour, minute, grain, signal_id, phase, direction, source_table, metric, value, sample_count
        FROM moe_observations_hourly
        """
    )
    conn.execute(
        """
        CREATE TABLE store_summary AS
        SELECT grain,
               COUNT(*) AS row_count,
               COUNT(DISTINCT signal_id) AS signal_count,
               COUNT(DISTINCT metric) AS metric_count,
               MIN(timestamp) AS date_from,
               MAX(timestamp) AS date_to
        FROM moe_observations
        GROUP BY grain
        ORDER BY grain
        """
    )


def optimize_store(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE sorted_15min AS
        SELECT *
        FROM moe_observations_15min
        ORDER BY grain, metric, signal_id, phase, timestamp
        """
    )
    conn.execute("DROP TABLE moe_observations_15min")
    conn.execute("ALTER TABLE sorted_15min RENAME TO moe_observations_15min")
    conn.execute(
        """
        CREATE TABLE sorted_hourly AS
        SELECT *
        FROM moe_observations_hourly
        ORDER BY grain, metric, signal_id, phase, timestamp
        """
    )
    conn.execute("DROP TABLE moe_observations_hourly")
    conn.execute("ALTER TABLE sorted_hourly RENAME TO moe_observations_hourly")
    conn.execute("CREATE INDEX idx_15min_metric_signal_time ON moe_observations_15min(metric, signal_id, timestamp)")
    conn.execute("CREATE INDEX idx_15min_signal_metric_time ON moe_observations_15min(signal_id, metric, timestamp)")
    conn.execute("CREATE INDEX idx_hourly_metric_signal_time ON moe_observations_hourly(metric, signal_id, timestamp)")
    conn.execute("CREATE INDEX idx_hourly_signal_metric_time ON moe_observations_hourly(signal_id, metric, timestamp)")
    conn.execute("CREATE INDEX idx_daily_metric_signal_date ON moe_daily_summary(metric, signal_id, date)")


def build_store(output: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    intersections = load_intersections()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    source_rows: list[dict[str, Any]] = []
    total_rows = 0
    with duckdb.connect(str(output)) as conn:
        create_empty_store(conn)
        for signal_id in intersections["signal_id"]:
            path = historical_db(str(signal_id))
            source_rows.append(
                {
                    "signal_id": str(signal_id),
                    "sqlite_path": None if path is None else str(path),
                    "exists": path is not None,
                }
            )
            if path is None:
                print(f"Skipping signal {signal_id}: no historical SQLite DB found")
                continue
            for frame in observation_frames_for_db(path):
                total_rows += append_frame(conn, frame)

        write_dimension_tables(conn, intersections, source_rows, output)
        create_hourly_rollup(conn)
        create_summary_tables(conn)
        optimize_store(conn)
        summary = {
            "output": str(output),
            "intersections": int(len(intersections)),
            "source_databases": int(sum(1 for row in source_rows if row["exists"])),
            "metrics": int(len(METRIC_DEFINITIONS)),
            "rows_15min": total_rows,
            "rows_hourly": int(conn.execute("SELECT COUNT(*) FROM moe_observations_hourly").fetchone()[0]),
            "date_from": conn.execute("SELECT MIN(timestamp) FROM moe_observations_15min").fetchone()[0].isoformat(),
            "date_to": conn.execute("SELECT MAX(timestamp) FROM moe_observations_15min").fetchone()[0].isoformat(),
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a read-optimized MOE analytics DuckDB store.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build_store(args.output), indent=2))


if __name__ == "__main__":
    main()
