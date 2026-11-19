import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from chronos_detector import initialize,connect,advance,snapshot

class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.sqlite';initialize(self.path)
        with connect(self.path) as db:
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',('test','1252',2,'2026-05-02 00:00:00','2026-05-03 00:00:00','running','2026-05-02 00:00:00','{}','{}'))
    def tearDown(self): self.tmp.cleanup()
    def score(self, values):
        with connect(self.path) as db:
            for i,(actual,prediction,lower) in enumerate(values):
                advance(db,'test',str(datetime(2026,5,2)+timedelta(minutes=5*i)),actual,prediction,lower)
        return snapshot('test',self.path)
    def test_trigger_and_recovery_does_not_follow_lower_forecast(self):
        s=self.score([(10,40,30),(10,40,30),(10,12,4),(10,12,4),(35,40,30),(35,40,30)])
        e=s['events'][0]
        self.assertEqual(e['confirmed_at'],'2026-05-02 00:10:00')
        self.assertEqual(e['recovery_at'],'2026-05-02 00:30:00')
        self.assertEqual(e['status'],'recovered')
    def test_missing_resets_pending(self):
        s=self.score([(10,40,30),(None,40,30),(10,40,30)])
        self.assertEqual(s['events'],[])
        self.assertFalse(s['points'][1]['scored'])
    def test_missing_does_not_recover_active_event(self):
        s=self.score([(10,40,30),(10,40,30),(None,None,None)])
        self.assertEqual(s['events'][0]['status'],'data_gap')
        self.assertIsNone(s['points'][-1]['anomaly_score'])
    def test_strict_threshold(self):
        s=self.score([(30,40,30),(30,40,30)])
        self.assertFalse(any(p['low'] for p in s['points']))
        self.assertEqual(s['events'],[])

if __name__=='__main__':unittest.main()
