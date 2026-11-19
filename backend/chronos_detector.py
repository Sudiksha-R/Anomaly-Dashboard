"""Chronological Chronos scoring. No dependency on the other anomaly methods."""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

STORE = Path(__file__).resolve().parents[1] / 'data' / 'chronos_detector.sqlite'

def connect(path=STORE):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    return db

def initialize(path=STORE):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, signal TEXT, phase INTEGER,
          start TEXT, end TEXT, status TEXT, clock TEXT, metadata TEXT, state TEXT);
        CREATE TABLE IF NOT EXISTS forecasts(run TEXT, origin TEXT, target TEXT, horizon INTEGER,
          prediction REAL, lower_threshold REAL, issued_at TEXT, actual REAL, scored_at TEXT,
          PRIMARY KEY(run,origin,horizon));
        CREATE TABLE IF NOT EXISTS points(run TEXT, origin TEXT, actual REAL, prediction REAL,
          lower_threshold REAL, scored INTEGER, low INTEGER, input_available INTEGER,
          anomaly_score REAL, event_id INTEGER, PRIMARY KEY(run,origin));
        CREATE TABLE IF NOT EXISTS events(run TEXT, event_id INTEGER, start TEXT, end TEXT,
          confirmed_at TEXT, status TEXT, recovery_at TEXT, observed REAL, expected REAL,
          reference_lower REAL, PRIMARY KEY(run,event_id));
        ''')

def advance(db, run, origin, actual, prediction, lower):
    """Score one closed bucket, with two-low trigger and two-bucket recovery.

    Retain an onset reference to avoid recovery caused solely by a lower forecast.
    This heuristic needs incident-label validation; it is not incident confirmation.
    """
    state = json.loads(db.execute('SELECT state FROM runs WHERE id=?', (run,)).fetchone()[0])
    available = prediction is not None
    scored = available and actual is not None
    low = bool(scored and actual < lower)
    close = (datetime.fromisoformat(origin) + timedelta(minutes=5)).isoformat(sep=' ')
    active = state.get('active')
    event_id = active
    if active is not None:
        event = db.execute('SELECT * FROM events WHERE run=? AND event_id=?', (run, active)).fetchone()
        if not scored:
            state['recovery'] = 0
            db.execute("UPDATE events SET status='data_gap',end=? WHERE run=? AND event_id=?", (close, run, active))
        else:
            recovered = actual >= max(lower, event['reference_lower'])
            state['recovery'] = state.get('recovery', 0) + 1 if recovered else 0
            status = 'recovered' if state['recovery'] >= 2 else 'active'
            db.execute('UPDATE events SET status=?,end=?,recovery_at=?,observed=observed+?,expected=expected+? WHERE run=? AND event_id=?',
                       (status, close, close if status == 'recovered' else None, actual, prediction, run, active))
            if status == 'recovered':
                state['active'] = None
                state['pending'] = None
                state['recovery'] = 0
    else:
        pending = state.get('pending')
        contiguous = pending and datetime.fromisoformat(origin) - datetime.fromisoformat(pending['origin']) == timedelta(minutes=5)
        if low and contiguous:
            event_id = state.get('next_id', 1)
            state.update(active=event_id, next_id=event_id+1, pending=None, recovery=0)
            reference = max(pending['lower'], lower)
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (run, event_id, pending['origin'], close, close, 'active', None,
                        pending['actual']+actual, pending['prediction']+prediction, reference))
            db.execute('UPDATE points SET event_id=? WHERE run=? AND origin=?', (event_id, run, pending['origin']))
        else:
            state['pending'] = dict(origin=origin, actual=actual, prediction=prediction, lower=lower) if low else None
    score = max(0., (prediction-actual) / max(prediction-lower, 1e-6)) if scored else None
    db.execute('INSERT INTO points VALUES(?,?,?,?,?,?,?,?,?,?)',
               (run, origin, actual, prediction, lower, int(scored), int(low), int(available), score, event_id))
    db.execute('UPDATE runs SET state=? WHERE id=?', (json.dumps(state), run))

def snapshot(run, path=STORE):
    with connect(path) as db:
        db.execute("BEGIN")
        row = db.execute('SELECT * FROM runs WHERE id=?', (run,)).fetchone()
        if row is None:
            raise KeyError(run)
        points = []
        for p in db.execute('SELECT * FROM points WHERE run=? ORDER BY origin', (run,)):
            p = dict(p); p['day'] = p['origin'][:10]
            for key in ('scored', 'low', 'input_available'): p[key] = bool(p[key])
            points.append(p)
        events = []
        for e in db.execute('SELECT * FROM events WHERE run=? ORDER BY event_id', (run,)):
            e = dict(e); e['day'] = e['start'][:10]
            e['peak_score'] = db.execute('SELECT max(anomaly_score) FROM points WHERE run=? AND event_id=?', (run,e['event_id'])).fetchone()[0]
            e['duration_minutes'] = int((datetime.fromisoformat(e['end'])-datetime.fromisoformat(e['start'])).total_seconds()/60)
            e['drop_percent'] = 100*(1-e['observed']/e['expected']) if e['expected'] else None
            events.append(e)
        return dict(schema_version=1, model='Chronos-2 · weekly + calendar', signal_id=row['signal'], phase=row['phase'],
                    dates=sorted({p['day'] for p in points}) or [row['start'][:10]], points=points, events=events,
                    run_id=run, status=row['status'], replay_clock=row['clock'], metadata=json.loads(row['metadata']))
