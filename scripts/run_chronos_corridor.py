"""Independent-series Chronos inference, batched at each causal replay clock."""
import argparse,csv,json,os,sys,hashlib
from datetime import datetime,timedelta
from pathlib import Path
APP=Path(__file__).resolve().parents[1]; ROOT=APP.parent
MODEL_ROOT=ROOT/'experiments/bee_ridge_forecasting'
sys.path.insert(0,str(APP/'backend'))
from chronos_detector import initialize,connect,advance

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--store',type=Path,required=True)
 p.add_argument('--dates',nargs='+',default=['2026-05-02','2026-05-11','2026-05-18'])
 a=p.parse_args()
 if a.store.resolve().parent==(APP/'data').resolve():p.error('Use a staging store outside dashboard data.')
 import duckdb,numpy as np,pandas as pd,torch
 from chronos import Chronos2Pipeline
 source=duckdb.connect(str(APP/'data/moe_volume_5min.duckdb'),read_only=True)
 series=[(str(s),int(ph)) for s,ph in source.execute("SELECT DISTINCT signal_id,phase FROM moe_observations_5min WHERE metric='volume' AND phase IN (2,6) ORDER BY 1,2").fetchall()]
 latest=source.execute("SELECT max(timestamp) FROM moe_observations_5min WHERE metric='volume'").fetchone()[0]
 threshold_path=MODEL_ROOT/'results/five_minute_calibration/thresholds.csv'
 with threshold_path.open() as f: thresholds={int(r['horizon_minutes']):float(r['residual_q05']) for r in csv.DictReader(f)}
 os.environ['HF_HUB_OFFLINE']='1';os.environ['HF_HOME']=str(MODEL_ROOT/'.cache/huggingface')
 checkpoint=MODEL_ROOT/'results/calendar_epochs/weekly_calendar/finetuned-ckpt'
 device='cuda' if torch.cuda.is_available() else 'cpu';torch.set_num_threads(4)
 initialize(a.store)
 metadata=dict(mode='historical_replay',checkpoint=str(checkpoint),device=device,thresholds=thresholds,threshold_sha256=hashlib.sha256(threshold_path.read_bytes()).hexdigest(),context_buckets=12,horizons=[5,10,15],cross_learning=False,persistence=2,recovery_buckets=2,recovery_rule='Two observations at or above current and onset lower thresholds',timezone='unverified source clock',calibration='Frozen validation; selection reused validation',batch='corridor-v1')
 model=Chronos2Pipeline.from_pretrained(str(checkpoint),device_map=device,local_files_only=True)
 for day in a.dates:
  start=datetime.fromisoformat(day);end=min(start+timedelta(days=1),latest-timedelta(minutes=5))
  if start<datetime(2026,4,3) or end<=start:raise ValueError('Dates must be after calibration and inside source coverage')
  ids={key:f'corridor-v1-{key[0]}-p{key[1]}-{start:%Y%m%d}' for key in series}
  with connect(a.store) as db:
   existing={r['id']:r['status'] for r in db.execute('SELECT id,status FROM runs')}
   if any(existing.get(v) not in (None,'complete') for v in ids.values()):raise ValueError('Incomplete run: choose a fresh staging store')
   ids={k:v for k,v in ids.items() if v not in existing}
   for (signal,phase),rid in ids.items():db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)',(rid,signal,phase,str(start),str(end),'running',str(start),json.dumps(metadata),'{}'))
  if not ids:continue
  try:
   clock=start
   while clock<=end+timedelta(minutes=10):
    closed=clock-timedelta(minutes=5)
    outcomes=source.execute("SELECT signal_id,phase,value FROM moe_observations_5min WHERE metric='volume' AND timestamp=?",[closed]).fetchall()
    observed={}
    for signal,phase,value in outcomes:
     key=(str(signal),int(phase))
     if key in observed:raise ValueError('Duplicate outcome')
     observed[key]=float(value) if value is not None and np.isfinite(value) else None
    with connect(a.store) as db:
     for key,rid in ids.items():
      for f in db.execute('SELECT * FROM forecasts WHERE run=? AND target=? AND scored_at IS NULL',(rid,str(closed))).fetchall():
       actual=observed.get(key)
       db.execute('UPDATE forecasts SET actual=?,scored_at=? WHERE run=? AND origin=? AND horizon=?',(actual,str(clock),rid,f['origin'],f['horizon']))
       if f['horizon']==5:advance(db,rid,f['target'],actual,f['prediction'],f['lower_threshold'])
      db.execute('UPDATE runs SET clock=? WHERE id=?',(str(clock),rid))
    if clock<end:
     times=pd.date_range(clock-timedelta(hours=1),periods=15,freq='5min');needed=list(times[:12])
     for k in (1,2,3):needed.extend(times-pd.Timedelta(weeks=k))
     assert max(needed)<clock
     marks=','.join('?' for _ in needed)
     frame=source.execute(f"SELECT signal_id,phase,timestamp,value FROM moe_observations_5min WHERE metric='volume' AND timestamp<? AND timestamp IN ({marks})",[clock,*[t.to_pydatetime() for t in needed]]).df()
     if frame.duplicated(['signal_id','phase','timestamp']).any():raise ValueError('Duplicate history')
     groups={(str(s),int(ph)):g.set_index('timestamp').value for (s,ph),g in frame.groupby(['signal_id','phase'])}
     cal=np.concatenate([np.eye(24,dtype=np.float32)[times.hour],np.eye(7,dtype=np.float32)[times.dayofweek]],axis=1).T
     batch=[];keys=[];predictions={k:[None]*3 for k in ids}
     for key in ids:
      s=groups.get(key,pd.Series(dtype=float));x=s.reindex(times[:12]).to_numpy(dtype=np.float32)
      if not np.isfinite(x).all():continue
      w=np.stack([s.reindex(times-pd.Timedelta(weeks=k)).to_numpy(dtype=np.float32) for k in (1,2,3)]);w[~np.isfinite(w)]=np.nan
      future=np.concatenate([np.full((1,3),np.nan,dtype=np.float32),w[:,12:],cal[:,12:]])
      assert np.isnan(future[0]).all()
      batch.append(dict(context=torch.tensor(np.concatenate([x[None,:],w[:,:12],cal[:,:12]])),future_covariates=torch.tensor(future),n_targets=1,n_covariates=34,n_future_covariates=34));keys.append(key)
     if batch:
      with torch.inference_mode():q,_=model.predict_quantiles(batch,prediction_length=3,context_length=12,quantile_levels=[.5],batch_size=1120,cross_learning=False)
      for key,z in zip(keys,q):
       values=z[0,:,0].cpu().clamp(min=0).tolist()
       if not all(np.isfinite(values)):raise ValueError('Nonfinite forecast')
       predictions[key]=values
     with connect(a.store) as db:
      for key,rid in ids.items():
       for i,value in enumerate(predictions[key]):
        h=(i+1)*5
        db.execute('INSERT INTO forecasts VALUES(?,?,?,?,?,?,?,?,?)',(rid,str(clock),str(clock+timedelta(minutes=i*5)),h,value,max(0,value+thresholds[h]) if value is not None else None,str(clock),None,None))
     if clock.minute==0:print(f'{clock}: {len(ids)} independent series committed',flush=True)
    clock+=timedelta(minutes=5)
   with connect(a.store) as db:
    for rid in ids.values():db.execute("UPDATE runs SET status='complete' WHERE id=?",(rid,))
   print('Completed day',day,flush=True)
  except BaseException:
   with connect(a.store) as db:
    for rid in ids.values():db.execute("UPDATE runs SET status='failed' WHERE id=?",(rid,))
   raise
 source.close()
if __name__=='__main__':main()
