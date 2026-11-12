from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "pipeline"))

import duckdb  # noqa: E402
import build_moe_analytics_store as builder  # noqa: E402


class MoeAnalyticsStoreBuilderTests(unittest.TestCase):
    def test_builder_creates_15min_and_hourly_rollups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            derived = tmp / "derived"
            derived.mkdir()
            reference = derived / "reference_data.sqlite"
            with sqlite3.connect(reference) as conn:
                conn.execute(
                    """
                    CREATE TABLE bee_ridge_intersections (
                        DeviceId INTEGER,
                        name TEXT,
                        latitude DOUBLE,
                        longitude DOUBLE,
                        route TEXT
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO bee_ridge_intersections VALUES (1000, 'Test Signal', 27.1, -82.1, 'Bee Ridge')"
                )

            signal_dir = derived / "dask_historical_1000_all_days_feb_to_may"
            signal_dir.mkdir()
            sqlite_path = signal_dir / "atspm_hourly_results.sqlite"
            with sqlite3.connect(sqlite_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE arrival_on_green (
                        TimeStamp TEXT,
                        DeviceId INTEGER,
                        Phase INTEGER,
                        Total_Actuations DOUBLE,
                        Percent_AOG DOUBLE
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO arrival_on_green VALUES (?, 1000, 2, ?, ?)",
                    [
                        ("2026-02-02 08:00:00", 10, 0.5),
                        ("2026-02-02 08:15:00", 20, 0.75),
                    ],
                )
                conn.execute(
                    """
                    CREATE TABLE phase_wait (
                        TimeStamp TEXT,
                        DeviceId INTEGER,
                        Phase INTEGER,
                        AvgPhaseWait DOUBLE,
                        MaxPhaseWait DOUBLE,
                        TotalSkips DOUBLE
                    )
                    """
                )
                conn.execute("INSERT INTO phase_wait VALUES ('2026-02-02 08:00:00', 1000, 2, 30, 45, 1)")
                conn.execute(
                    """
                    CREATE TABLE split_failures (
                        TimeStamp TEXT,
                        DeviceId INTEGER,
                        Phase INTEGER,
                        Green_Time DOUBLE,
                        Green_Occupancy DOUBLE,
                        Red_Occupancy DOUBLE,
                        Split_Failure DOUBLE
                    )
                    """
                )
                conn.execute("INSERT INTO split_failures VALUES ('2026-02-02 08:00:00', 1000, 2, 70, 0.4, 0.6, 2)")
                conn.execute(
                    """
                    CREATE TABLE terminations (
                        TimeStamp TEXT,
                        DeviceId INTEGER,
                        Phase INTEGER,
                        PerformanceMeasure TEXT,
                        Total DOUBLE
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO terminations VALUES ('2026-02-02 08:00:00', 1000, 2, ?, ?)",
                    [("GapOut", 3), ("MaxOut", 4), ("ForceOff", 5)],
                )
                conn.execute(
                    """
                    CREATE TABLE coordination_agg (
                        TimeStamp TEXT,
                        DeviceId INTEGER,
                        Pattern INTEGER,
                        CycleLength DOUBLE,
                        ActualCycleLength DOUBLE,
                        ActualOffset DOUBLE
                    )
                    """
                )
                conn.execute("INSERT INTO coordination_agg VALUES ('2026-02-02 08:00:00', 1000, 1, 120, 118, 12)")

            old_reference = builder.REFERENCE_DB
            old_derived = builder.DERIVED_ROOT
            try:
                builder.REFERENCE_DB = reference
                builder.DERIVED_ROOT = derived
                output = tmp / "moe_analytics.duckdb"
                summary = builder.build_store(output)
            finally:
                builder.REFERENCE_DB = old_reference
                builder.DERIVED_ROOT = old_derived

            self.assertEqual(summary["intersections"], 1)
            self.assertEqual(summary["source_databases"], 1)
            self.assertGreater(summary["rows_15min"], 0)
            self.assertGreater(summary["rows_hourly"], 0)

            with duckdb.connect(str(output), read_only=True) as conn:
                tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
                self.assertIn("moe_observations_15min", tables)
                self.assertIn("moe_observations_hourly", tables)
                self.assertIn("moe_daily_summary", tables)

                hourly_volume = conn.execute(
                    """
                    SELECT value
                    FROM moe_observations_hourly
                    WHERE metric = 'volume' AND signal_id = '1000' AND phase = 2
                    """
                ).fetchone()[0]
                self.assertEqual(hourly_volume, 30)

                hourly_aog = conn.execute(
                    """
                    SELECT value
                    FROM moe_observations_hourly
                    WHERE metric = 'aogPct' AND signal_id = '1000' AND phase = 2
                    """
                ).fetchone()[0]
                self.assertEqual(hourly_aog, 62.5)


if __name__ == "__main__":
    unittest.main()
