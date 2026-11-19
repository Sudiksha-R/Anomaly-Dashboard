"""Read-only source audit; never rebuilds data or starts model inference."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import duckdb

APP = Path(__file__).resolve().parents[1]

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(8*1024*1024), b''): h.update(block)
    return h.hexdigest()

def audit(source):
    source = Path(source).resolve()
    if not source.is_file(): raise FileNotFoundError(source)
    before = digest(source)
    with duckdb.connect(str(source), read_only=True) as db:
        total, first, last = db.execute("SELECT count(*),min(timestamp),max(timestamp) FROM moe_observations_5min WHERE metric='volume'").fetchone()
        bad = db.execute("""SELECT count(*) FROM moe_observations_5min WHERE metric='volume' AND
          (timestamp IS NULL OR signal_id IS NULL OR phase IS NULL OR value IS NULL
           OR NOT isfinite(value) OR value<0 OR value<>floor(value)
           OR timestamp<>time_bucket(INTERVAL '5 minutes',timestamp))""").fetchone()[0]
        duplicates = db.execute("""SELECT count(*) FROM (SELECT timestamp,signal_id,phase FROM moe_observations_5min
          WHERE metric='volume' GROUP BY 1,2,3 HAVING count(*)>1)""").fetchone()[0]
        series = [dict(zip(['signal','phase','rows','first','last'], r)) for r in db.execute("""
          SELECT signal_id,phase,count(*),min(timestamp),max(timestamp) FROM moe_observations_5min
          WHERE metric='volume' GROUP BY 1,2 ORDER BY 1,2""").fetchall()]
    after = digest(source)
    return dict(source=str(source),checked_at_utc=datetime.now(timezone.utc).isoformat(),rows=total,
                first=first,last=last,invalid_rows=bad,duplicate_keys=duplicates,series=series,
                sha256_before=before,sha256_after=after,unchanged=before==after,
                data_checks_pass=bool(total and not bad and not duplicates and before==after),
                live_ready=False,
                live_blockers=['No verified live ingestion connection or completion watermark',
                               'Source timestamps have no verified timezone',
                               'Bucket absence does not establish zero traffic'],
                note='Valid historical rows do not prove freshness, complete coverage, or an active feed.')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=APP/'data/moe_volume_5min.duckdb')
    a=p.parse_args()
    result=audit(a.source)
    print(json.dumps(result,indent=2,default=str))
    if not result['data_checks_pass']: raise SystemExit(1)
