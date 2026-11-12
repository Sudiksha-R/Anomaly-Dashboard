from __future__ import annotations

import sys
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))

from main import anomalies, changepoints_endpoint, meta, overview, timeline  # noqa: E402


class StoreApiTests(unittest.TestCase):
    def test_overview_includes_entire_corridor(self) -> None:
        payload = overview(None, None, None, 3.5, None)
        self.assertEqual(len(payload["intersections"]), 14)
        self.assertEqual(payload["summary"]["modeled_intersections"], 13)
        no_data = next(item for item in payload["intersections"] if item["severity"] == "No data")
        self.assertIsNone(no_data["latest_anomaly"])

    def test_anomalies_are_ranked(self) -> None:
        items = anomalies(None, None, None, None, 20, 3.5, None)["items"]
        self.assertTrue(items)
        scores = [item["abs_score"] for item in items]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_every_entity_has_a_timeline(self) -> None:
        entities = meta()["entities"]
        self.assertEqual(len(entities), 26)
        for entity in entities:
            self.assertTrue(timeline(entity["entity_id"], None, None, 3.5)["points"])

    def test_changepoints_are_materialized(self) -> None:
        self.assertGreater(changepoints_endpoint(None, None, None)["count"], 0)

    def test_threshold_changes_anomaly_results(self) -> None:
        default = overview(None, None, None, 3.5, None)["summary"]["anomaly_count"]
        strict = overview(None, None, None, 10.0, None)["summary"]["anomaly_count"]
        self.assertLess(strict, default)
        strict_items = anomalies(None, None, None, None, 100, 10.0, None)["items"]
        self.assertTrue(all(item["abs_score"] > 10 for item in strict_items))

    def test_intersection_history_contains_ten_latest_filtered_events(self) -> None:
        rows = overview(None, None, None, 3.5, None)["intersections"]
        for row in rows:
            events = row["recent_events"]
            self.assertLessEqual(len(events), 10)
            self.assertTrue(all(event["geh"] > 3.5 for event in events))
            timestamps = [event["event_time"] for event in events]
            self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_intersection_selection_scopes_all_views(self) -> None:
        intersections = meta()["intersections"]
        self.assertEqual(len(intersections), 14)
        selection = ",".join(item["signal_id"] for item in intersections[:2])
        payload = overview(None, None, None, 3.5, selection)
        self.assertEqual(len(payload["intersections"]), 2)
        self.assertLessEqual(payload["summary"]["modeled_intersections"], 2)
        self.assertTrue(all(item["signal_id"] in selection.split(",") for item in changepoints_endpoint(None, None, selection)["items"]))

    def test_clearing_intersections_returns_empty_views(self) -> None:
        payload = overview(None, None, None, 3.5, "__none__")
        self.assertEqual(payload["intersections"], [])
        self.assertEqual(payload["summary"]["modeled_intersections"], 0)
        self.assertEqual(anomalies(None, None, None, None, 20, 3.5, "__none__")["items"], [])
        self.assertEqual(changepoints_endpoint(None, None, "__none__")["items"], [])


if __name__ == "__main__":
    unittest.main()
