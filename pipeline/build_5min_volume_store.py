from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = Path(__file__).resolve().parents[1]
DERIVED_ROOT = PROJECT_ROOT / "derived"
REFERENCE_DB = DERIVED_ROOT / "reference_data.sqlite"
DEFAULT_OUTPUT = APP_ROOT / "data" / "moe_volume_5min.duckdb"
PHASE_DIRECTIONS = {2: "Eastbound", 6: "Westbound"}


def sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def load_reference_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    with sqlite3.connect(REFERENCE_DB) as conn:
        mappings = pd.read_sql_query(
            """
            SELECT CAST(DeviceId AS VARCHAR) AS signal_id,
                   CAST(Parameter AS INTEGER) AS detector,
                   CAST(Phase AS INTEGER) AS phase
            FROM mapping_effective
            WHERE Function = 'Advance'
              AND Phase IN (2, 6)
              AND DeviceId IS NOT NULL
              AND Parameter IS NOT NULL
            """,
            conn,
        ).drop_duplicates()
        intersections = pd.read_sql_query(
            """
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
            """,
            conn,
        ).drop_duplicates("signal_id", keep="first")
    mappings["direction"] = mappings["phase"].map(PHASE_DIRECTIONS)
    return mappings, intersections


def raw_event_files(signal_id: str) -> list[Path]:
    preferred = DERIVED_ROOT / f"dask_historical_{signal_id}_all_days_feb_to_may" / "days"
    roots = [preferred] if preferred.exists() else sorted(
        DERIVED_ROOT.glob(f"dask_historical_{signal_id}_all_days_*/days")
    )
    for root in roots:
        files = sorted(root.glob(f"*/signal_{signal_id}_*_filtered.csv"))
        if files:
            return files
    return []


def build_store(output: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    if not REFERENCE_DB.exists():
        raise FileNotFoundError(f"Reference database not found: {REFERENCE_DB}")

    mappings, intersections = load_reference_data()
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()

    files_read = 0
    signals_read = 0
    try:
        with duckdb.connect(str(temporary)) as conn:
            conn.execute("PRAGMA threads=8")
            conn.execute(
                """
                CREATE TABLE moe_observations_5min (
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

            for signal_id in sorted(mappings["signal_id"].unique()):
                files = raw_event_files(signal_id)
                signal_mapping = mappings.loc[mappings["signal_id"] == signal_id]
                if not files or signal_mapping.empty:
                    continue

                conn.register("signal_mapping", signal_mapping)
                file_list = "[" + ",".join(sql_string(path) for path in files) + "]"
                conn.execute(
                    f"""
                    INSERT INTO moe_observations_5min
                    WITH detector_events AS (
                        SELECT TRY_STRPTIME(timeStamp, '%m-%d-%Y %H:%M:%S.%f') AS event_time,
                               CAST(signalID AS VARCHAR) AS signal_id,
                               CAST(eventParam AS INTEGER) AS detector
                        FROM read_csv(
                            {file_list},
                            header = TRUE,
                            columns = {{
                                'signalID': 'INTEGER',
                                'timeStamp': 'VARCHAR',
                                'eventCode': 'INTEGER',
                                'eventParam': 'INTEGER'
                            }}
                        )
                        WHERE eventCode = 82
                    ), bucketed AS (
                        SELECT TIME_BUCKET(INTERVAL '5 minutes', events.event_time) AS timestamp,
                               mapping.signal_id,
                               mapping.phase,
                               mapping.direction,
                               COUNT(*)::DOUBLE AS value
                        FROM detector_events events
                        INNER JOIN signal_mapping mapping
                          ON mapping.signal_id = events.signal_id
                         AND mapping.detector = events.detector
                        WHERE events.event_time IS NOT NULL
                        GROUP BY 1, 2, 3, 4
                    )
                    SELECT timestamp,
                           CAST(timestamp AS DATE),
                           DATE_PART('hour', timestamp)::SMALLINT,
                           DATE_PART('minute', timestamp)::SMALLINT,
                           '5min',
                           signal_id,
                           phase,
                           direction,
                           'raw_advance_detector_events',
                           'volume',
                           value
                    FROM bucketed
                    """
                )
                conn.unregister("signal_mapping")
                files_read += len(files)
                signals_read += 1

            conn.register("intersection_frame", intersections)
            conn.execute("CREATE TABLE intersections AS SELECT * FROM intersection_frame")
            conn.unregister("intersection_frame")
            conn.execute(
                """
                CREATE TABLE metadata AS
                SELECT * FROM (VALUES
                    ('store_kind', 'moe_volume'),
                    ('grain', '5min'),
                    ('interval_minutes', '5'),
                    ('source', 'raw advance-detector eventCode 82'),
                    ('generated_at', CAST(CURRENT_TIMESTAMP AS VARCHAR))
                ) values_table(key, value)
                """
            )
            conn.execute("CREATE INDEX volume_5min_entity_time_idx ON moe_observations_5min(signal_id, phase, timestamp)")
            conn.execute("ANALYZE")
            summary = conn.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT signal_id), MIN(timestamp), MAX(timestamp)
                FROM moe_observations_5min
                """
            ).fetchone()

        os.replace(temporary, output)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise

    return {
        "output": str(output),
        "rows": int(summary[0]),
        "signals": int(summary[1]),
        "source_files": files_read,
        "signals_read": signals_read,
        "date_from": summary[2].isoformat() if summary[2] else None,
        "date_to": summary[3].isoformat() if summary[3] else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build true 5-minute volume from raw advance-detector events.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build_store(args.output), indent=2))


if __name__ == "__main__":
    main()
