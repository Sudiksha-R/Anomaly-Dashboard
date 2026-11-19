"""Run on a compute allocation. Serves the separate one-hour request queue."""
import sys,os,time,json,fcntl
from pathlib import Path
APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP/'backend'))
from chronos_requests import QUEUE,MODEL,write_json,process

def main():
    QUEUE.mkdir(parents=True,exist_ok=True)
    lock=(QUEUE/'worker.lock').open('w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    write_json(QUEUE/'heartbeat.json',{'ready':False,'time':time.time()})
    os.environ['HF_HUB_OFFLINE']='1'
    import torch,numpy as np
    from chronos import Chronos2Pipeline
    torch.set_num_threads(4)
    if not torch.cuda.is_available():raise RuntimeError('Start this worker inside a GPU allocation.')
    model=Chronos2Pipeline.from_pretrained(str(MODEL),device_map='cuda',local_files_only=True)
    def predict(r):
        x=np.asarray(r['history'],dtype=np.float32);w=np.asarray(r['weekly'],dtype=np.float32);cal=np.asarray(r['calendar'],dtype=np.float32)
        future=np.concatenate([np.full((1,3),np.nan,dtype=np.float32),w[:,12:],cal[:,12:]])
        assert np.isnan(future[0]).all()
        task=dict(context=torch.tensor(np.concatenate([x[None,:],w[:,:12],cal[:,:12]])),future_covariates=torch.tensor(future),n_targets=1,n_covariates=34,n_future_covariates=34)
        with torch.inference_mode():q,_=model.predict_quantiles([task],prediction_length=3,context_length=12,quantile_levels=[.5],batch_size=35,cross_learning=False)
        return q[0][0,:,0].cpu().clamp(min=0).tolist()
    # A previous worker may have stopped mid-request; recompute from immutable input.
    for f in QUEUE.glob('*/status.json'):
        if json.loads(f.read_text()).get('status')=='running':write_json(f,{'status':'queued'})
    print('Chronos one-hour worker ready',flush=True)
    try:
        while True:
            write_json(QUEUE/'heartbeat.json',{'ready':True,'time':time.time()})
            for f in sorted(QUEUE.glob('*/status.json')):
                if json.loads(f.read_text()).get('status')!='queued':continue
                write_json(f,{'status':'running'})
                try:process(f.parent,predict)
                except Exception as e:write_json(f,{'status':'failed','error':str(e)})
                write_json(QUEUE/'heartbeat.json',{'ready':True,'time':time.time()})
            time.sleep(2)
    finally:write_json(QUEUE/'heartbeat.json',{'ready':False,'time':time.time()})
if __name__=='__main__':main()
