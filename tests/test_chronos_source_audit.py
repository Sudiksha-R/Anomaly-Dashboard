import importlib.util
from pathlib import Path
import tempfile
import unittest
import duckdb

spec=importlib.util.spec_from_file_location('chronos_source_audit',Path(__file__).resolve().parents[1]/'scripts/check_chronos_source.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class SourceAuditTests(unittest.TestCase):
    def test_valid_historical_data_does_not_claim_live_and_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.duckdb'
            with duckdb.connect(str(source)) as db:
                db.execute('CREATE TABLE moe_observations_5min(timestamp TIMESTAMP,signal_id VARCHAR,phase INTEGER,metric VARCHAR,value DOUBLE)')
                db.execute("INSERT INTO moe_observations_5min VALUES ('2026-05-02 00:00:00','1252',2,'volume',10)")
            result=module.audit(source)
            self.assertTrue(result['data_checks_pass'])
            self.assertTrue(result['unchanged'])
            self.assertFalse(result['live_ready'])
    def test_bad_counts_duplicates_and_misaligned_time_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.duckdb'
            with duckdb.connect(str(source)) as db:
                db.execute('CREATE TABLE moe_observations_5min(timestamp TIMESTAMP,signal_id VARCHAR,phase INTEGER,metric VARCHAR,value DOUBLE)')
                db.execute("INSERT INTO moe_observations_5min VALUES ('2026-05-02 00:01:00','1252',2,'volume',-1),('2026-05-02 00:01:00','1252',2,'volume',10)")
            result=module.audit(source)
            self.assertFalse(result['data_checks_pass'])
            self.assertEqual(result['duplicate_keys'],1)
            self.assertEqual(result['invalid_rows'],2)
            self.assertTrue(result['unchanged'])
