#!/usr/bin/env python3
"""
Extrai candles M15 e H1 da API Deriv via WebSocket (ticks_history) e grava
numa base de dados SQLite SEPARADA (forex_backtest_candles.db).

Uso:
    python3 fetch_forex_history.py            # extrai últimos 180 dias
    python3 fetch_forex_history.py 365        # extrai últimos 365 dias

Requer: websocket-client (já presente no requirements.txt)

NOTAS DE DESIGN:
- Itera do mais recente para o mais antigo. Para quando a API devolve lote vazio
  (i.e., chegou ao limite real de retenção), não antes.
- Schema: PRIMARY KEY (symbol, granularity, epoch) — M15 e H1 coexistem sem colisão.
- Retry: 3 tentativas por lote; cada retry abre uma ligação nova.
- FIX 16.4: cada lote abre a sua própria ligação WS. Se a rede cair a meio,
  só o lote em curso falha. Erros definitivos da API são distinguidos de erros
  de rede e NÃO disparam retry.
- Versão síncrona (websocket-client), para não depender de instalações adicionais
  no Web Shell do Render que não persistem entre sessões.
"""
import json
import sqlite3
import os
import sys
import time

import websocket  # websocket-client (sync) — já instalado

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SYMBOLS = ["frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxEURGBP", "frxAUDUSD", "frxUSDCAD"]
GRANULARITIES = [900, 3600]   # M15, H1
GRAN_LABELS = {900: 'M15', 3600: 'H1'}
BATCH_SIZE = 5000
MAX_RETRIES = 3
RETRY_DELAY = 3
WS_TIMEOUT = 20

DATA_PATH = os.environ.get('DATA_PATH', '/var/data')
DB_PATH = os.path.join(DATA_PATH, 'forex_backtest_candles.db')


class APIError(Exception):
    """Erro definitivo devolvido pela API Deriv (não vale a pena retry)."""
    pass


def init_db():
    os.makedirs(DATA_PATH, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute('''CREATE TABLE IF NOT EXISTS candles (
        symbol TEXT NOT NULL,
        granularity INTEGER NOT NULL,
        epoch INTEGER NOT NULL,
        open REAL, high REAL, low REAL, close REAL,
        PRIMARY KEY (symbol, granularity, epoch))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_symbol_gran_epoch ON candles(symbol, granularity, epoch)')
    conn.commit()
    conn.close()
    print(f"DB inicializada: {DB_PATH}")


def fetch_candles_once(symbol, granularity, start_epoch, end_epoch):
    """Uma tentativa. Abre a sua própria ligação WS, pede as velas, fecha."""
    ws = websocket.create_connection(DERIV_WS_URL, timeout=WS_TIMEOUT)
    try:
        ws.send(json.dumps({
            "ticks_history": symbol,
            "style": "candles",
            "granularity": granularity,
            "start": start_epoch,
            "end": end_epoch,
            "count": BATCH_SIZE
        }))
        data = json.loads(ws.recv())
        if 'error' in data:
            raise APIError(f"API error: {data['error']}")
        return data.get('candles', [])
    finally:
        try: ws.close()
        except Exception: pass


def fetch_candles_with_retry(symbol, granularity, start_epoch, end_epoch):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return fetch_candles_once(symbol, granularity, start_epoch, end_epoch)
        except APIError:
            raise
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                print(f"    Retry {attempt + 1}/{MAX_RETRIES} (rede): {e}")
                time.sleep(RETRY_DELAY)
            else:
                print(f"    Todas as tentativas falharam: {e}")
    raise last_error


def fetch_symbol_granularity(symbol, granularity, start_epoch, end_epoch):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    inserted = 0
    current_end = end_epoch
    label = GRAN_LABELS.get(granularity, str(granularity))
    while current_end > start_epoch:
        current_start = max(current_end - (BATCH_SIZE * granularity), start_epoch)
        try:
            candles = fetch_candles_with_retry(symbol, granularity, current_start, current_end)
        except APIError as e:
            print(f"  [{symbol}/{label}] Erro API: {e}"); break
        except Exception as e:
            print(f"  [{symbol}/{label}] Lote abandonado: {e}")
            current_end = current_start; continue
        if not candles:
            print(f"  [{symbol}/{label}] Lote vazio → limite de retenção atingido.")
            break
        for c in candles:
            try:
                conn.execute("INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?)",
                    (symbol, granularity, c['epoch'], c['open'], c['high'], c['low'], c['close']))
                inserted += 1
            except Exception as e:
                print(f"  [{symbol}/{label}] Erro inserir: {e}")
        conn.commit()
        print(f"  [{symbol}/{label}] {current_start}-{current_end}: {len(candles)} velas (total: {inserted})")
        current_end = current_start
        time.sleep(0.5)
    conn.close()
    return inserted


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    end_epoch = int(time.time())
    start_epoch = end_epoch - (days * 86400)
    init_db()
    print(f"A puxar {days} dias para {len(SYMBOLS)} pares × {len(GRANULARITIES)} granularidades...")
    total = 0
    for symbol in SYMBOLS:
        print(f"=== {symbol} ===")
        for gran in GRANULARITIES:
            try:
                n = fetch_symbol_granularity(symbol, gran, start_epoch, end_epoch)
                total += n
                print(f"  [{symbol}/{GRAN_LABELS[gran]}] Total: {n}")
            except Exception as e:
                print(f"  [{symbol}/{GRAN_LABELS[gran]}] FATAL: {e}")
    print(f"\n✅ Concluído. Total: {total} velas em {DB_PATH}")


if __name__ == '__main__':
    main()
