from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "pipeline"))

import duckdb  # noqa: E402
import build_interruption_store as builder  # noqa: E402


class InterruptionStoreBuilderTests(unittest.TestCase):
    def test_builder_groups_consecutive_15_minute_volume_drops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            source = tmp / "moe.duckdb"
            output = tmp / "interruptions.duckdb"

            rows: list[tuple[datetime, str, int, str, float]] = []
            current_day = datetime(2026, 2, 23, 8, 0)
            for weeks_back in (3, 2, 1):
                historical_day = current_day - timedelta(weeks=weeks_back)
                rows.extend(
                    [
                        (historical_day, "1000", 2, "Eastbound", 100.0),
                        (historical_day + timedelta(minutes=15), "1000", 2, "Eastbound", 100.0),
                    ]
                )
            rows.extend(
                [
                    (current_day, "1000", 2, "Eastbound", 20.0),
                    (current_day + timedelta(minutes=15), "1000", 2, "Eastbound", 20.0),
                    (current_day + timedelta(minutes=30), "1000", 2, "Eastbound", 100.0),
                ]
            )

            with duckdb.connect(str(source)) as conn:
                conn.execute(
                    """
                    CREATE TABLE moe_observations_15min (
                        timestamp TIMESTAMP,
                        signal_id VARCHAR,
                        phase INTEGER,
                        direction VARCHAR,
                        metric VARCHAR,
                        value DOUBLE
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO moe_observations_15min VALUES (?, ?, ?, ?, 'volume', ?)",
                    rows,
                )
                conn.execute(
                    """
                    CREATE TABLE intersections (
                        signal_id VARCHAR,
                        name VARCHAR,
                        lat DOUBLE,
                        lon DOUBLE,
                        route VARCHAR
                    )
                    """
                )
                conn.execute("INSERT INTO intersections VALUES ('1000', 'Test Signal', 27.1, -82.1, 'Bee Ridge')")

            summary = builder.build_store(source, output)

            self.assertEqual(summary["signals"], 1)
            self.assertGreaterEqual(summary["events"], 1)
            with duckdb.connect(str(output), read_only=True) as conn:
                event = conn.execute(
                    """
                    SELECT name, duration_minutes, observed_volume,
                           expected_volume, avg_reduction_pct
                    FROM interruption_events
                    WHERE start_time = '2026-02-23 08:00:00'
                    """
                ).fetchone()
                self.assertEqual(event[0], "Test Signal")
                self.assertEqual(event[1], 30)
                self.assertEqual(event[2], 20.0)
                self.assertEqual(event[3], 100.0)
                self.assertEqual(event[4], 80.0)

    def test_builder_groups_consecutive_5_minute_volume_drops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            source = tmp / "moe_5min.duckdb"
            output = tmp / "interruptions_5min.duckdb"
            current_day = datetime(2026, 2, 23, 8, 0)
            rows: list[tuple[datetime, str, int, str, float]] = []
            for weeks_back in (3, 2, 1):
                historical_day = current_day - timedelta(weeks=weeks_back)
                rows.extend(
                    (historical_day + timedelta(minutes=offset), "1000", 2, "Eastbound", 40.0)
                    for offset in range(0, 30, 5)
                )
            rows.extend(
                (current_day + timedelta(minutes=offset), "1000", 2, "Eastbound", 8.0)
                for offset in range(0, 30, 5)
            )

            with duckdb.connect(str(source)) as conn:
                conn.execute(
                    """
                    CREATE TABLE moe_observations_5min (
                        timestamp TIMESTAMP,
                        signal_id VARCHAR,
                        phase INTEGER,
                        direction VARCHAR,
                        metric VARCHAR,
                        value DOUBLE
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO moe_observations_5min VALUES (?, ?, ?, ?, 'volume', ?)",
                    rows,
                )
                conn.execute(
                    """
                    CREATE TABLE intersections (
                        signal_id VARCHAR, name VARCHAR, lat DOUBLE, lon DOUBLE, route VARCHAR
                    )
                    """
                )
                conn.execute("INSERT INTO intersections VALUES ('1000', 'Test Signal', 27.1, -82.1, 'Bee Ridge')")

            summary = builder.build_store(source, output, interval_minutes=5)

            self.assertEqual(summary["interval_minutes"], 5)
            with duckdb.connect(str(output), read_only=True) as conn:
                event = conn.execute(
                    """
                    SELECT duration_minutes, observed_volume, expected_volume, avg_reduction_pct
                    FROM interruption_events
                    WHERE start_time = '2026-02-23 08:00:00'
                    """
                ).fetchone()
                self.assertEqual(event, (30, 8.0, 40.0, 80.0))
                metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
                self.assertEqual(metadata["grain"], "5min")
                self.assertEqual(metadata["smoothing_intervals"], "6")


if __name__ == "__main__":
    unittest.main()
