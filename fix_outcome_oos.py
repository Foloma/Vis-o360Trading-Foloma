#!/usr/bin/env python3
"""Reconstrói outcomes dos 125 sinais OOS pós-17/09 via candles Deriv M15."""
import os, sqlite3, json, time, csv
import websocket

DB = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')
OUT = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'forex_outcome_oos.csv')
WS = "wss://ws.derivws.com/websockets/v3?app_id=1089"
CUTOFF = '2026-09-17'


def fetch(ws, sym, count=4000):
    ws.send(json.dumps({"ticks_history": sym, "style": "candles",
                        "granularity": 900, "count": count, "end": "latest"}))
    while True:
        r = json.loads(ws.recv())
        if r.get('msg_type') == 'candles':
            if 'error' in r:
                raise RuntimeError(r['error'])
            return r.get('candles', [])


conn = sqlite3.connect(DB, timeout=10)
rows = conn.execute(
    "SELECT id, symbol, direction, timestamp, price_at_signal, breakdown_json "
    "FROM forex_signal_log WHERE strategy_used='ensemble' AND confidence >= 80 "
    "AND date(timestamp,'unixepoch') >= ? ORDER BY timestamp",
    (CUTOFF,)
).fetchall()
print(f"Pendentes: {len(rows)}")

ws = websocket.create_connection(WS, timeout=30)
cache = {}
try:
    for sym in sorted(set(r[1] for r in rows)):
        cs = fetch(ws, sym)
        cache[sym] = {c['epoch']: c for c in cs}
        print(f"  {sym}: {len(cs)} candles")
finally:
    ws.close()

updates = []
for sid, sym, direction, ts, price_then, bj in rows:
    cs = cache.get(sym, {})
    if not cs or price_then is None:
        updates.append((sid, 'nodata', None))
        continue
    start = int(float(ts) // 900) * 900
    nc = cs.get(start + 900)
    if not nc:
        later = sorted(e for e in cs if e > start)
        nc = cs[later[0]] if later else None
    if not nc:
        updates.append((sid, 'nodata', None))
        continue
    pa = nc['close']
    up = pa > price_then
    outcome = 'win' if ((direction == 'BUY') == up) else 'loss'
    updates.append((sid, outcome, pa))

now = time.time()
for sid, o, pa in updates:
    conn.execute(
        "UPDATE forex_signal_log SET evaluated=1, outcome=?, price_after=?, "
        "evaluated_at=? WHERE id=?",
        (o, pa, now, sid)
    )
conn.commit()

wins = sum(1 for _, o, _ in updates if o == 'win')
losses = sum(1 for _, o, _ in updates if o == 'loss')
nod = sum(1 for _, o, _ in updates if o == 'nodata')
print(f"\nResultado: {wins}W / {losses}L / {nod} nodata")
if wins + losses:
    print(f"WR = {wins / (wins + losses) * 100:.1f}%")

outcome_by_id = {sid: (o, pa) for sid, o, pa in updates}
with open(OUT, 'w', newline='', encoding='utf-8') as f:
    w = csv.writer(f)
    w.writerow(['id', 'symbol', 'direction', 'timestamp', 'confidence', 'outcome',
                'price_at_signal', 'price_after', 'rsi', 'adx', 'pct_b', 'ema_dist_pct'])
    for sid, sym, direction, ts, pt, bj in rows:
        o, pa = outcome_by_id.get(sid, ('nodata', None))
        rsi = adx = pct_b = ema = None
        if bj:
            try:
                raw = json.loads(bj).get('_raw_indicators', {})
                rsi = raw.get('rsi_14')
                adx = raw.get('adx_14')
                pct_b = raw.get('pct_b')
                ema = raw.get('ema_dist_pct')
            except Exception:
                pass
        w.writerow([sid, sym, direction, ts, 80, o, pt, pa, rsi, adx, pct_b, ema])

print(f"CSV: {OUT}")
conn.close()
