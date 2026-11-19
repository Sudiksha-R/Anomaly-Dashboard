"""Validate isolated corridor runs and optionally append them atomically to Chronos only."""
import argparse,hashlib,json,sqlite3,sys
from datetime import datetime,timedelta
from pathlib import Path
APP=Path(__file__).resolve().parents[1];ROOT=APP.parent
sys.path.insert(0,str(APP/'backend'))
from chronos_detector import STORE

def hash_file(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
 return h.hexdigest()

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--publish',action='store_true');a=p.parse_args()
 folder=ROOT/'artifacts/reports/chronos_corridor_expansion'
 src=sqlite3.connect(f'file:{folder / "detector.sqlite"}?mode=ro',uri=True);src.row_factory=sqlite3.Row
 old=sqlite3.connect(f'file:{STORE}?mode=ro',uri=True);old.row_factory=sqlite3.Row
 runs=src.execute('SELECT * FROM runs').fetchall()
 assert len(runs)==78 and all(r['status']=='complete' for r in runs)
 summary=[]
 for r in runs:
  points=src.execute('SELECT * FROM points WHERE run=? ORDER BY origin',(r['id'],)).fetchall()
  forecasts=src.execute('SELECT * FROM forecasts WHERE run=?',(r['id'],)).fetchall()
  n=int((datetime.fromisoformat(r['end'])-datetime.fromisoformat(r['start'])).total_seconds()/300)
  assert len(points)==n and len(forecasts)==n*3
  for i,v in enumerate(points):
   assert datetime.fromisoformat(v['origin'])==datetime.fromisoformat(r['start'])+timedelta(minutes=5*i)
   assert bool(v['scored'])==(v['actual'] is not None and v['prediction'] is not None)
   assert bool(v['low'])==bool(v['scored'] and v['actual']<v['lower_threshold'])
   assert (v['anomaly_score'] is not None)==bool(v['scored'])
  for f in forecasts:
   issue=datetime.fromisoformat(f['issued_at']);target=datetime.fromisoformat(f['target']);scored=datetime.fromisoformat(f['scored_at'])
   assert target==issue+timedelta(minutes=f['horizon']-5)
   assert scored==target+timedelta(minutes=5) and issue<scored
  events=src.execute('SELECT * FROM events WHERE run=?',(r['id'],)).fetchall()
  for e in events:
   start=datetime.fromisoformat(e['start']);trigger=datetime.fromisoformat(e['confirmed_at'])
   assert trigger==start+timedelta(minutes=10)
   onset=[v for v in points if start<=datetime.fromisoformat(v['origin'])<trigger]
   assert len(onset)==2 and all(v['low'] for v in onset)
   assert e['reference_lower']==max(v['lower_threshold'] for v in onset)
   if e['status']=='recovered':
    recovery=datetime.fromisoformat(e['recovery_at'])
    tail=[v for v in points if recovery-timedelta(minutes=10)<=datetime.fromisoformat(v['origin'])<recovery]
    assert len(tail)==2 and all(v['scored'] and v['actual']>=max(v['lower_threshold'],e['reference_lower']) for v in tail)
  summary.append(dict(run=r['id'],signal=r['signal'],phase=r['phase'],day=r['start'][:10],buckets=n,scored=sum(v['scored'] for v in points),low=sum(v['low'] for v in points),events=len(events),unresolved=sum(e['status']!='recovered' for e in events)))
 comparisons=[]
 for r in old.execute("SELECT * FROM runs WHERE id LIKE 'chronos-%'"):
  rid=f"corridor-v1-{r['signal']}-p{r['phase']}-{r['start'][:10].replace('-','')}"
  previous={v['origin']:dict(v) for v in old.execute('SELECT * FROM points WHERE run=?',(r['id'],))}
  current=src.execute('SELECT * FROM points WHERE run=?',(rid,)).fetchall();delta=0.
  for v in current:
   prior=previous[v['origin']]
   for key in ('actual','scored','low','input_available'):assert v[key]==prior[key],(rid,key)
   if v['prediction'] is not None:delta=max(delta,abs(v['prediction']-prior['prediction']))
  assert delta<0.001,delta
  comparisons.append(dict(old=r['id'],new=rid,max_prediction_difference=delta))
 baseline=json.loads((ROOT/'artifacts/reports/chronos_live_readiness/integrity.json').read_text())['after']
 protected={str(ROOT/k):v['sha256'] for k,v in baseline.items() if not k.endswith('chronos_detector.sqlite')}
 for path,expected in protected.items():assert hash_file(Path(path))==expected,path
 # Keep a database backup and a digest of all old rows; never rebuild or replace the active store.
 old_content={table:[tuple(v) for v in old.execute(f'SELECT * FROM {table} ORDER BY 1,2')] for table in ('runs','forecasts','points','events')}
 if a.publish:
  backup=folder/'chronos_before_expansion.sqlite'
  if backup.exists():raise ValueError('Backup already exists; inspect previous publication before retrying')
  with sqlite3.connect(backup) as b:old.backup(b)
  with sqlite3.connect(STORE) as dest:
   dest.execute('BEGIN IMMEDIATE')
   for table in ('runs','forecasts','points','events'):
    rows=[tuple(v) for v in src.execute(f'SELECT * FROM {table}')]
    marks=','.join('?' for _ in rows[0]) if rows else ''
    if rows:dest.executemany(f'INSERT INTO {table} VALUES({marks})',rows)
   for table,rows in old_content.items():
    now={tuple(v) for v in dest.execute(f'SELECT * FROM {table}')}
    assert all(v in now for v in rows),table
  for path,expected in protected.items():assert hash_file(Path(path))==expected,path
 report=dict(published=a.publish,runs=len(summary),buckets=sum(r['buckets'] for r in summary),scored=sum(r['scored'] for r in summary),events=sum(r['events'] for r in summary),unresolved=sum(r['unresolved'] for r in summary),comparisons=comparisons,protected_stores_unchanged=True,summary=summary)
 (folder/'validation.json').write_text(json.dumps(report,indent=2))
 print(json.dumps({k:v for k,v in report.items() if k!='summary'},indent=2))
if __name__=='__main__':main()
