from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import duckdb


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = APP_ROOT / "data" / "moe_analytics.duckdb"
DEFAULT_OUTPUT = APP_ROOT / "data" / "interruption_events.duckdb"

HISTORY_WEEKS = (1, 2, 3)
MIN_HISTORY_DAYS = 2
MIN_BASELINE_VOLUME_15MIN = 10.0


def sql_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def build_store(
    source: Path = DEFAULT_SOURCE,
    output: Path = DEFAULT_OUTPUT,
    interval_minutes: int = 15,
    source_table: str | None = None,
) -> dict[str, Any]:
    if interval_minutes not in (5, 15):
        raise ValueError("interval_minutes must be 5 or 15")
    source_table = source_table or f"moe_observations_{interval_minutes}min"
    if not source_table.replace("_", "").isalnum():
        raise ValueError("source_table contains invalid characters")
    smoothing_intervals = max(1, 30 // interval_minutes)
    min_baseline_volume = MIN_BASELINE_VOLUME_15MIN * interval_minutes / 15
    source = source.resolve()
    output = output.resolve()
    if not source.exists():
        raise FileNotFoundError(f"MOE analytics store not found: {source}")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()

    try:
        with duckdb.connect(str(temporary)) as conn:
            conn.execute(f"ATTACH '{sql_path(source)}' AS source (READ_ONLY)")
            conn.execute(
                f"""
                CREATE TABLE interruption_points AS
                WITH volume AS (
                    SELECT timestamp,
                           signal_id,
                           phase,
                           direction,
                           value AS raw_volume,
                           AVG(value) OVER (
                               PARTITION BY signal_id, phase, CAST(timestamp AS DATE)
                               ORDER BY timestamp
                               ROWS BETWEEN {smoothing_intervals - 1} PRECEDING AND CURRENT ROW
                           ) AS observed_volume
                    FROM source.{source_table}
                    WHERE metric = 'volume'
                      AND phase IN (2, 6)
                ), compared AS (
                    SELECT current.timestamp,
                           current.signal_id,
                           current.phase,
                           current.direction,
                           current.raw_volume,
                           current.observed_volume,
                           history.expected_volume,
                           history.history_days
                    FROM volume current
                    LEFT JOIN volume week_1
                      ON week_1.signal_id = current.signal_id
                     AND week_1.phase = current.phase
                     AND week_1.timestamp = current.timestamp - INTERVAL '{HISTORY_WEEKS[0]} week'
                    LEFT JOIN volume week_2
                      ON week_2.signal_id = current.signal_id
                     AND week_2.phase = current.phase
                     AND week_2.timestamp = current.timestamp - INTERVAL '{HISTORY_WEEKS[1]} week'
                    LEFT JOIN volume week_3
                      ON week_3.signal_id = current.signal_id
                     AND week_3.phase = current.phase
                     AND week_3.timestamp = current.timestamp - INTERVAL '{HISTORY_WEEKS[2]} week'
                    LEFT JOIN LATERAL (
                        SELECT AVG(value) AS expected_volume,
                               COUNT(*) AS history_days
                        FROM (VALUES
                            (week_1.observed_volume),
                            (week_2.observed_volume),
                            (week_3.observed_volume)
                        ) historical(value)
                        WHERE value >= {min_baseline_volume}
                    ) history ON TRUE
                )
                SELECT timestamp,
                       signal_id,
                       phase,
                       direction,
                       raw_volume,
                       observed_volume,
                       expected_volume,
                       observed_volume - expected_volume AS volume_delta,
                       100.0 * (expected_volume - observed_volume) / NULLIF(expected_volume, 0) AS reduction_pct,
                       history_days,
                       observed_volume < expected_volume AS is_drop
                FROM compared
                WHERE history_days >= {MIN_HISTORY_DAYS}
                  AND expected_volume >= {min_baseline_volume}
                """
            )
            conn.execute(
                f"""
                CREATE TABLE interruption_events AS
                WITH ordered AS (
                    SELECT *,
                           LAG(timestamp) OVER entity_window AS previous_timestamp,
                           LAG(is_drop, 1, FALSE) OVER entity_window AS previous_is_drop
                    FROM interruption_points
                    WINDOW entity_window AS (
                        PARTITION BY signal_id, phase
                        ORDER BY timestamp
                    )
                ), marked AS (
                    SELECT *,
                           CASE
                               WHEN is_drop AND (
                                   NOT previous_is_drop
                                   OR previous_timestamp IS NULL
                                   OR timestamp > previous_timestamp + INTERVAL '{interval_minutes} minutes'
                               ) THEN 1
                               ELSE 0
                           END AS starts_event
                    FROM ordered
                ), grouped AS (
                    SELECT *,
                           SUM(starts_event) OVER (
                               PARTITION BY signal_id, phase
                               ORDER BY timestamp
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                           ) AS event_number
                    FROM marked
                ), summaries AS (
                    SELECT signal_id,
                           phase,
                           direction,
                           event_number,
                           MIN(timestamp) AS start_time,
                           MAX(timestamp) + INTERVAL '{interval_minutes} minutes' AS end_time,
                           DATE_DIFF('minute', MIN(timestamp), MAX(timestamp)) + {interval_minutes} AS duration_minutes,
                           COUNT(*) AS point_count,
                           AVG(raw_volume) AS raw_observed_volume,
                           AVG(observed_volume) AS observed_volume,
                           AVG(expected_volume) AS expected_volume,
                           AVG(observed_volume - expected_volume) AS volume_delta,
                           AVG(reduction_pct) AS avg_reduction_pct,
                           MAX(reduction_pct) AS max_reduction_pct,
                           MIN(history_days) AS min_history_days
                    FROM grouped
                    WHERE is_drop
                    GROUP BY signal_id, phase, direction, event_number
                )
                SELECT CONCAT(
                           signal_id, '-', phase, '-',
                           STRFTIME(start_time, '%Y%m%d%H%M')
                       ) AS event_id,
                       summaries.signal_id,
                       intersections.name,
                       summaries.phase,
                       summaries.direction,
                       start_time,
                       end_time,
                       duration_minutes,
                       point_count,
                       raw_observed_volume,
                       observed_volume,
                       expected_volume,
                       volume_delta,
                       avg_reduction_pct,
                       max_reduction_pct,
                       min_history_days,
                       CASE
                           WHEN avg_reduction_pct >= 70 THEN 'Critical'
                           WHEN avg_reduction_pct >= 50 THEN 'High'
                           WHEN avg_reduction_pct >= 30 THEN 'Watch'
                           ELSE 'Low'
                       END AS severity
                FROM summaries
                LEFT JOIN source.intersections intersections USING (signal_id)
                """
            )
            conn.execute("CREATE TABLE intersections AS SELECT * FROM source.intersections")
            conn.execute(
                """
                CREATE TABLE metadata AS
                SELECT * FROM (VALUES
                    ('store_kind', 'interruption_events'),
                    ('source_store', ?),
                    ('grain', ?),
                    ('interval_minutes', ?),
                    ('smoothing_intervals', ?),
                    ('history_weeks', ?),
                    ('min_history_days', ?),
                    ('min_baseline_volume', ?),
                    ('generated_at', CAST(CURRENT_TIMESTAMP AS VARCHAR))
                ) values_table(key, value)
                """,
                [
                    str(source),
                    f"{interval_minutes}min",
                    str(interval_minutes),
                    str(smoothing_intervals),
                    ",".join(str(week) for week in HISTORY_WEEKS),
                    str(MIN_HISTORY_DAYS),
                    str(min_baseline_volume),
                ],
            )
            conn.execute("CREATE INDEX interruption_event_time_idx ON interruption_events(start_time)")
            conn.execute("CREATE INDEX interruption_event_entity_idx ON interruption_events(signal_id, phase, direction)")
            conn.execute("CREATE INDEX interruption_point_entity_time_idx ON interruption_points(signal_id, phase, timestamp)")
            conn.execute("ANALYZE")

            point_summary = conn.execute(
                """
                SELECT COUNT(*) AS point_count,
                       COUNT(DISTINCT signal_id) AS signal_count,
                       MIN(timestamp) AS date_from,
                       MAX(timestamp) AS date_to
                FROM interruption_points
                """
            ).fetchone()
            event_count = conn.execute("SELECT COUNT(*) FROM interruption_events").fetchone()[0]
            qualified_event_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM interruption_events
                WHERE duration_minutes >= 30 AND avg_reduction_pct >= 30
                """
            ).fetchone()[0]

        os.replace(temporary, output)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise

    return {
        "output": str(output),
        "source": str(source),
        "interval_minutes": interval_minutes,
        "points": int(point_summary[0]),
        "signals": int(point_summary[1]),
        "events": int(event_count),
        "default_qualified_events": int(qualified_event_count),
        "date_from": point_summary[2].isoformat() if point_summary[2] else None,
        "date_to": point_summary[3].isoformat() if point_summary[3] else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a resolution-specific interruption/EOI event store.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--interval-minutes", type=int, choices=(5, 15), default=15)
    parser.add_argument("--source-table")
    args = parser.parse_args()
    print(json.dumps(build_store(args.source, args.output, args.interval_minutes, args.source_table), indent=2))


if __name__ == "__main__":
    main()
