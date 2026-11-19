import sys,json,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
import duckdb
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import chronos_requests as cr

class RequestTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.source=self.root/'source.duckdb'
  with duckdb.connect(str(self.source)) as db:
   db.execute('CREATE TABLE moe_observations_5min(timestamp TIMESTAMP,signal_id VARCHAR,phase INTEGER,metric VARCHAR,value DOUBLE)')
   for i in range(15):db.execute('INSERT INTO moe_observations_5min VALUES(?,?,?,?,?)',[datetime(2026,5,11,8)+timedelta(minutes=i*5),'1252',2,'volume',20+i])
  self.patch=patch.object(cr,'SOURCE',self.source);self.patch.start()
 def tearDown(self):self.patch.stop();self.tmp.cleanup()
 def test_future_values_do_not_enter_inputs(self):
  a=cr.prepare('1252',2,'2026-05-11',8)
  with duckdb.connect(str(self.source)) as db:db.execute("UPDATE moe_observations_5min SET value=99999 WHERE timestamp>='2026-05-11 09:00:00'")
  b=cr.prepare('1252',2,'2026-05-11',8)
  for key in ('history','weekly','calendar'):self.assertEqual(a[key],b[key])
  self.assertEqual(len(a['history']),12)
 def test_missing_input_rejected(self):
  with duckdb.connect(str(self.source)) as db:db.execute("DELETE FROM moe_observations_5min WHERE timestamp='2026-05-11 08:05:00'")
  with self.assertRaisesRegex(ValueError,'12 valid'):cr.prepare('1252',2,'2026-05-11',8)
 def test_predictions_persist_before_actual_read(self):
  req=cr.prepare('1252',2,'2026-05-11',8);path=self.root/'job';cr.write_json(path/'input.json',req)
  def outcomes(r):
   self.assertTrue((path/'predictions.json').exists())
   self.assertEqual(json.loads((path/'predictions.json').read_text())['predictions'],[40,40,40])
   return [10,10,None]
  with patch.object(cr,'read_outcomes',outcomes):cr.process(path,lambda r:[40,40,40])
  result=json.loads((path/'result.json').read_text())
  self.assertEqual(len(result['points']),3);self.assertEqual(len(result['events']),1)
  self.assertFalse(result['points'][2]['scored']);self.assertIn('unresolved',result['events'][0]['status'])
 def test_invalid_hour_rejected(self):
  for h in (-1,24,8.5,True):
   with self.assertRaises(ValueError):cr.prepare('1252',2,'2026-05-11',h)
