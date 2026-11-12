"""Export the reviewed Chronos replay as a portable, read-only UI snapshot."""
import csv,json,math,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'experiments/bee_ridge_forecasting/results/calendar_epochs/continuous_review'
DEST=ROOT/'anomaly-dashboard/frontend/public/chronos-review.json'
def number(v):
 try:n=float(v);return n if math.isfinite(n) else None
 except (TypeError,ValueError):return None
def read(name):
 with (SOURCE/name).open() as f:return list(csv.DictReader(f))
points=[]
for r in read('bucket_review.csv'):
 points.append(dict(day=r['day'],origin=r['origin'],actual=number(r['actual']),prediction=number(r['prediction']),lower_threshold=number(r['lower_threshold']),scored=r['scored']=='True',low=r['low']=='True',input_available=r['input_available']=='True',event_id=number(r['event_id'])))
events=[]
for r in read('candidate_events.csv'):
 eid=int(r['event_id']);members=[p for p in points if p['event_id']==eid]
 assert len(members)>=2 and all(p['scored'] and p['low'] for p in members)
 expected=sum(p['prediction'] for p in members);observed=sum(p['actual'] for p in members)
 events.append(dict(event_id=eid,day=r['day'],start=r['start'],end=r['end_exclusive'],confirmed_at=r['confirmed_at'],duration_minutes=int(r['duration_minutes']),observed=observed,expected=expected,drop_percent=100*(expected-observed)/expected if expected>0 else None))
assert len({p['origin'] for p in points})==len(points)
payload=dict(schema_version=1,model='Chronos-2 · weekly + calendar',signal_id='1252',phase=2,dates=sorted({p['day'] for p in points}),bucket_minutes=5,persistence_buckets=2,points=points,events=events,source_sha256=hashlib.sha256((SOURCE/'bucket_review.csv').read_bytes()).hexdigest())
DEST.write_text(json.dumps(payload,allow_nan=False,separators=(',',':'))+'\n')
print(f'Exported {len(points)} buckets and {len(events)} candidate events to {DEST}')
