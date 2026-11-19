"""Isolated file queue for one-hour Chronos requests. Source is always read-only."""
import hashlib,json,os,time,fcntl
from datetime import datetime,timedelta
from pathlib import Path
import duckdb
import numpy as np
import pandas as pd

APP=Path(__file__).resolve().parents[1]
ROOT=APP.parent
SOURCE=APP/'data/moe_volume_5min.duckdb'
QUEUE=APP/'data/chronos_requests'
MODEL=ROOT/'experiments/bee_ridge_forecasting/results/calendar_epochs/weekly_calendar/finetuned-ckpt'
THRESHOLDS=ROOT/'experiments/bee_ridge_forecasting/results/five_minute_calibration/thresholds.csv'

def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value,allow_nan=False));os.replace(tmp,path)

def finite(values):
    return [float(x) if x is not None and np.isfinite(x) else None for x in values]

def available(signal=None,phase=None,day=None):
    with duckdb.connect(str(SOURCE),read_only=True) as db:
        if signal is None:
            return {'series':[{'signal':str(s),'phase':int(p)} for s,p in db.execute("SELECT DISTINCT signal_id,phase FROM moe_observations_5min WHERE metric='volume' AND phase IN (2,6) ORDER BY 1,2").fetchall()]}
        if day is None:
            return {'dates':[str(r[0]) for r in db.execute("SELECT DISTINCT cast(timestamp AS DATE) FROM moe_observations_5min WHERE metric='volume' AND signal_id=? AND phase=? ORDER BY 1",[signal,phase]).fetchall()]}
        start=datetime.strptime(day,'%Y-%m-%d')
        rows=db.execute("SELECT timestamp,value FROM moe_observations_5min WHERE metric='volume' AND signal_id=? AND phase=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",[signal,phase,start,start+timedelta(days=1)]).fetchall()
        hours=[]
        for h in range(24):
            t=start+timedelta(hours=h);group=[(ts,v) for ts,v in rows if t<=ts<t+timedelta(hours=1)]
            valid=len(group)==12 and {ts for ts,v in group}=={t+timedelta(minutes=5*i) for i in range(12)} and all(v is not None and np.isfinite(v) and v>=0 for ts,v in group)
            hours.append({'hour':h,'available':bool(valid),'observations':len(group)})
        return {'hours':hours}

def prepare(signal,phase,day,hour):
    if not isinstance(signal,str) or not signal.isdigit() or not isinstance(day,str) or type(phase) is not int:raise ValueError("Invalid intersection, phase or date.")
    if not isinstance(hour,int) or isinstance(hour,bool) or not 0<=hour<24:raise ValueError('Choose an hour from 0 to 23.')
    if phase not in (2,6):raise ValueError('Choose phase 2 or 6.')
    start=datetime.strptime(day,'%Y-%m-%d')+timedelta(hours=hour);origin=start+timedelta(hours=1)
    times=pd.date_range(start,periods=15,freq='5min')
    needed=list(times[:12])
    for k in (1,2,3):needed.extend(times-pd.Timedelta(weeks=k))
    assert max(needed)<origin
    with duckdb.connect(str(SOURCE),read_only=True) as db:
        marks=','.join('?' for _ in needed)
        frame=db.execute(f"SELECT timestamp,value FROM moe_observations_5min WHERE metric='volume' AND signal_id=? AND phase=? AND timestamp<? AND timestamp IN ({marks}) ORDER BY timestamp",[signal,phase,origin,*[t.to_pydatetime() for t in needed]]).df()
    if frame.timestamp.duplicated().any():raise ValueError('Duplicate source timestamps; prediction cannot safely proceed.')
    s=frame.set_index('timestamp').value
    history=finite(s.reindex(times[:12]).to_numpy())
    if any(v is None or v<0 for v in history):raise ValueError('This hour does not have all 12 valid five-minute counts. Select another hour.')
    weekly=[finite(s.reindex(times-pd.Timedelta(weeks=k)).to_numpy()) for k in (1,2,3)]
    calendar=np.concatenate([np.eye(24)[times.hour],np.eye(7)[times.dayofweek]],axis=1).T.tolist()
    warnings=[]
    if any(v is None for week in weekly for v in week):warnings.append('Some previous-week values are missing; the model receives missing values, not zeros.')
    if origin<datetime(2026,4,3):warnings.append('This date overlaps training/validation history. Results are exploratory, not held-out evaluation.')
    stat=SOURCE.stat()
    version={'source_size':stat.st_size,'source_mtime_ns':stat.st_mtime_ns,'model_files':[(p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(MODEL.iterdir()) if p.is_file()], 'threshold_sha256':hashlib.sha256(THRESHOLDS.read_bytes()).hexdigest()}
    return dict(signal=str(signal),phase=phase,day=day,hour=hour,start=str(start),origin=str(origin),history=history,weekly=weekly,calendar=calendar,warnings=warnings,version=version)

def worker_ready():
    try:
        h=json.loads((QUEUE/'heartbeat.json').read_text());return h.get('ready') and time.time()-h['time']<90
    except (OSError,ValueError,KeyError):return False

def _submit(payload):
    request=prepare(**payload)
    key=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()[:32]
    path=QUEUE/key
    if (path/'status.json').exists():
        status=json.loads((path/'status.json').read_text())
        if status['status'] in ('complete','queued','running'):return {'id':key,**status,'cached':status['status']=='complete'}
    if not worker_ready():raise RuntimeError('Chronos model worker is offline. Start the GPU worker; the other dashboard tabs remain available.')
    path.mkdir(parents=True,exist_ok=True)
    write_json(path/'input.json',request)
    write_json(path/'status.json',{'status':'queued','created_at':time.time()})
    return {'id':key,'status':'queued','cached':False}

def submit(payload):
    QUEUE.mkdir(parents=True,exist_ok=True)
    with (QUEUE/'submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _submit(payload)

def result(key):
    if len(key)!=32 or any(c not in '0123456789abcdef' for c in key):raise ValueError('Invalid request ID')
    path=QUEUE/key
    if not (path/'status.json').exists():raise FileNotFoundError(key)
    value={'id':key,**json.loads((path/'status.json').read_text()),'worker_ready':bool(worker_ready())}
    if value['status']=='complete':value['result']=json.loads((path/'result.json').read_text())
    return value

def read_outcomes(request):
    origin=datetime.fromisoformat(request['origin'])
    with duckdb.connect(str(SOURCE),read_only=True) as db:
        rows=db.execute("SELECT timestamp,value FROM moe_observations_5min WHERE metric='volume' AND signal_id=? AND phase=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",[request['signal'],request['phase'],origin,origin+timedelta(minutes=15)]).fetchall()
    if len({t for t,v in rows})!=len(rows):raise ValueError('Duplicate outcomes')
    mapping=dict(rows)
    return finite([mapping.get(origin+timedelta(minutes=5*i)) for i in range(3)])

def process(path,predict):
    request=json.loads((path/'input.json').read_text())
    stat=SOURCE.stat()
    if stat.st_size!=request['version']['source_size'] or stat.st_mtime_ns!=request['version']['source_mtime_ns']:raise ValueError('Source changed after submission. Submit the hour again.')
    # predict receives only history, historical weekly values and calendar inputs.
    predictions=finite(predict(request))
    if len(predictions)!=3 or any(v is None or v<0 for v in predictions):raise ValueError('Invalid model output')
    saved_at=time.time()
    write_json(path/'predictions.json',{'predictions':predictions,'saved_at':saved_at})
    # The only access to future actual volume is AFTER the above atomic publication.
    actual=read_outcomes(request)
    thresholds=pd.read_csv(THRESHOLDS).set_index('horizon_minutes').residual_q05.to_dict()
    origin=datetime.fromisoformat(request['origin']);points=[]
    for i,p in enumerate(predictions):
        lower=max(0,p+thresholds[(i+1)*5]);a=actual[i]
        points.append(dict(time=str(origin+timedelta(minutes=i*5)),prediction=p,actual=a,lower_threshold=lower,low=a is not None and a<lower,scored=a is not None))
    # Only three forecast buckets: consecutive low counts form a candidate;
    # there is insufficient follow-up to establish two-bucket recovery.
    events=[];pending=[]
    for i,point in enumerate(points):
        if point['low']:pending.append(i)
        else:
            if len(pending)>=2:events.append(pending)
            pending=[]
    if len(pending)>=2:events.append(pending)
    events=[dict(start=points[ix[0]]['time'],triggered_at=str(origin+timedelta(minutes=(ix[1]+1)*5)),end=str(origin+timedelta(minutes=15)),status='unresolved within forecast window') for ix in events]
    value=dict(**{k:request[k] for k in ('signal','phase','start','origin','history','warnings')},weekly_available=[sum(v is not None for v in w) for w in request['weekly']],points=points,events=events,predictions_saved_at=saved_at,scored_at=time.time())
    write_json(path/'result.json',value)
    write_json(path/'status.json',{'status':'complete'})
