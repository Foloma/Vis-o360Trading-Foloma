#!/usr/bin/env python3
"""Reconstrói outcomes dos 125 sinais OOS pós-17/09 via candles Deriv M15 (paginado)."""
import os, sqlite3, json, time, csv
from curl_cffi import requests as curl_requests

DB = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')
OUT = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'forex_outcome_oos.csv')
WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
CUTOFF_DATE = '2026-09-17'
CUTOFF_EPOCH = int(time.mktime(time.strptime(CUTOFF_DATE, '%Y-%m-%d')))

def fetch_all(session, sym):
    """Faz chamadas paginadas até cobrir o CUTOFF_EPOCH usando curl_cffi."""
    all_candles = {}
    end = "latest"
    attempts = 0
    while True:
        attempts += 1
        if attempts > 20:
            print(f"  AVISO: {sym} atingiu limite de paginação.")
            break
        payload = {"ticks_history": sym, "style": "candles",
                   "granularity": 900, "count": 3000, "end": end}
        response = session.ws_connect(WS_URL)
        try:
            response.send_str(json.dumps(payload))
            while True:
                msg = response.recv()
                if msg:
                    r = json.loads(msg)
                    if r.get('msg_type') == 'candles':
                        if 'error' in r:
                            raise RuntimeError(r['error'])
                        cs = r.get('candles', [])
                        if not cs:
                            return all_candles
                        for c in cs:
                            all_candles[c['epoch']] = c
                        oldest = min(c['epoch'] for c in cs)
                        print(f"  {sym}: +{len(cs)} velas (mais antiga: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(oldest))})")
                        if oldest <= CUTOFF_EPOCH:
                            return all_candles
                        end = str(oldest - 1)
                        break
        finally:
            response.close()

conn = sqlite3.connect(DB, timeout=10)
rows = conn.execute(
    "SELECT id, symbol, direction, timestamp, price_at_signal, breakdown_json "
    "FROM forex_signal_log WHERE strategy_used='ensemble' AND confidence >= 80 "
    "AND date(timestamp,'unixepoch') >= ? ORDER BY timestamp",
    (CUTOFF_DATE,)
).fetchall()
print(f"Sinais pendentes: {len(rows)}")

session = curl_requests.Session(impersonate="chrome")
cache = {}
for sym in sorted(set(r[1] for r in rows)):
    cache[sym] = fetch_all(session, sym)
    print(f"  {sym}: TOTAL {len(cache[sym])} velas")
session.close()

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
