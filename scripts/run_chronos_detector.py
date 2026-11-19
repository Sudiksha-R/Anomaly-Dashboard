"""Run actual neural inference against a historical clock; never train on replay data."""
import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parent
MODEL_ROOT = ROOT / 'experiments/bee_ridge_forecasting'
sys.path.insert(0, str(APP / 'backend'))
from chronos_detector import STORE, advance, connect, initialize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signal', default='1252')
    parser.add_argument('--phase', type=int, default=2)
    parser.add_argument('--start', required=True, help='Source-clock ISO start, on a five-minute boundary')
    parser.add_argument('--end', required=True, help='Exclusive end of primary detection buckets')
    parser.add_argument('--run-id', required=True, help='Unique ID; existing runs are never overwritten')
    parser.add_argument('--store', type=Path, default=STORE)
    args = parser.parse_args()
    start, end = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
    if start >= end or any(t.second or t.microsecond or t.minute % 5 or t.tzinfo for t in (start,end)):
        parser.error('Use increasing timezone-naive five-minute boundaries in the source clock.')
    # Detector calibration uses March 23–April 2; replay only later data.
    if start < datetime(2026,4,3): parser.error('Replay must be after the validation/calibration period.')
    import numpy as np
    import pandas as pd
    import duckdb
    import torch
    from chronos import Chronos2Pipeline
    os.environ['HF_HOME'] = str(MODEL_ROOT / '.cache/huggingface')
    os.environ['HF_HUB_OFFLINE'] = '1'
    checkpoint = MODEL_ROOT / 'results/calendar_epochs/weekly_calendar/finetuned-ckpt'
    threshold_path = MODEL_ROOT / 'results/five_minute_calibration/thresholds.csv'
    with threshold_path.open() as f:
        thresholds = {int(r['horizon_minutes']): float(r['residual_q05']) for r in csv.DictReader(f)}
    source = duckdb.connect(str(APP / 'data/moe_volume_5min.duckdb'), read_only=True)
    bounds = source.execute("SELECT min(timestamp),max(timestamp) FROM moe_observations_5min WHERE metric='volume' AND cast(signal_id as varchar)=? AND phase=?", [args.signal,args.phase]).fetchone()
    source_end = source.execute("SELECT max(timestamp) FROM moe_observations_5min WHERE metric='volume'").fetchone()[0]
    # Coverage comes from the dataset, not this signal's last nonmissing row.
    # Trailing signal gaps must be replayed as unknown rather than excluded.
    if bounds[1] is None or source_end is None or end + timedelta(minutes=10) > source_end + timedelta(minutes=5):
        parser.error('Requested range lacks source coverage for all three forecast horizons.')
    initialize(args.store)
    metadata = dict(mode='historical_replay', checkpoint=str(checkpoint), threshold_sha256=hashlib.sha256(threshold_path.read_bytes()).hexdigest(),
                    thresholds=thresholds, context_buckets=12, horizons=[5,10,15], persistence=2, recovery_buckets=2,
                    recovery_rule='Two observed buckets at or above both current and onset lower thresholds',
                    timezone='unverified source clock', calibration='Validation reused for model selection; exploratory thresholds',
                    device='cuda' if torch.cuda.is_available() else 'cpu')
    with connect(args.store) as db:
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)', (args.run_id,args.signal,args.phase,str(start),str(end),'starting',str(start),json.dumps(metadata),'{}'))
    try:
        torch.set_num_threads(4)
        model = Chronos2Pipeline.from_pretrained(str(checkpoint), device_map=metadata['device'], local_files_only=True)
        clock = start
        while clock <= end + timedelta(minutes=10):
            # Only closed outcomes are read. Forecasts were committed at their issue clock.
            with connect(args.store) as db:
                due = db.execute('SELECT * FROM forecasts WHERE run=? AND scored_at IS NULL AND target<=? ORDER BY target,origin',
                                 (args.run_id,str(clock-timedelta(minutes=5)))).fetchall()
                for f in due:
                    rows = source.execute("SELECT value FROM moe_observations_5min WHERE metric='volume' AND cast(signal_id as varchar)=? AND phase=? AND timestamp=?", [args.signal,args.phase,f['target']]).fetchall()
                    actual = float(rows[0][0]) if len(rows)==1 and rows[0][0] is not None and np.isfinite(rows[0][0]) else None
                    db.execute('UPDATE forecasts SET actual=?,scored_at=? WHERE run=? AND origin=? AND horizon=?', (actual,str(clock),args.run_id,f['origin'],f['horizon']))
                    if f['horizon']==5:
                        advance(db,args.run_id,f['target'],actual,f['prediction'],f['lower_threshold'])
                db.execute("UPDATE runs SET clock=?,status='running' WHERE id=?", (str(clock),args.run_id))
            if clock < end:
                # This SQL boundary prevents target volumes entering the model context.
                history = source.execute("SELECT timestamp,value FROM moe_observations_5min WHERE metric='volume' AND cast(signal_id as varchar)=? AND phase=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",
                                         [args.signal,args.phase,clock-timedelta(weeks=3,hours=1),clock]).df()
                if history.timestamp.duplicated().any(): raise ValueError('Duplicate source timestamps; cannot infer safely')
                s = history.set_index('timestamp').value
                times = pd.date_range(clock-timedelta(hours=1), periods=15, freq='5min')
                x = s.reindex(times[:12]).to_numpy(dtype=np.float32)
                predictions = [None]*3
                if np.isfinite(x).all():
                    weekly = np.stack([s.reindex(times-pd.Timedelta(weeks=k)).to_numpy(dtype=np.float32) for k in (1,2,3)])
                    weekly[~np.isfinite(weekly)] = np.nan
                    calendar = np.concatenate([np.eye(24,dtype=np.float32)[times.hour],np.eye(7,dtype=np.float32)[times.dayofweek]],axis=1).T
                    context = np.concatenate([x[None,:],weekly[:,:12],calendar[:,:12]])
                    future = np.concatenate([np.full((1,3),np.nan,dtype=np.float32),weekly[:,12:],calendar[:,12:]])
                    assert np.isnan(future[0]).all()
                    with torch.inference_mode():
                        q,_ = model.predict_quantiles([dict(context=torch.tensor(context),future_covariates=torch.tensor(future),n_targets=1,n_covariates=34,n_future_covariates=34)], prediction_length=3,context_length=12,quantile_levels=[.5],batch_size=35,cross_learning=False)
                    predictions = q[0][0,:,0].cpu().clamp(min=0).tolist()
                    if not all(np.isfinite(predictions)): raise ValueError('Nonfinite model output')
                with connect(args.store) as db:
                    for i,prediction in enumerate(predictions):
                        h=(i+1)*5
                        db.execute('INSERT INTO forecasts VALUES(?,?,?,?,?,?,?,?,?)', (args.run_id,str(clock),str(clock+timedelta(minutes=i*5)),h,prediction,max(0,prediction+thresholds[h]) if prediction is not None else None,str(clock),None,None))
                if clock.minute == 0: print(f'{args.run_id}: forecast committed at {clock}',flush=True)
            clock += timedelta(minutes=5)
        with connect(args.store) as db: db.execute("UPDATE runs SET status='complete' WHERE id=?", (args.run_id,))
        print('Replay complete:',args.run_id,flush=True)
    except BaseException:
        with connect(args.store) as db: db.execute("UPDATE runs SET status='failed' WHERE id=?", (args.run_id,))
        raise
    finally:
        source.close()

if __name__ == '__main__': main()
