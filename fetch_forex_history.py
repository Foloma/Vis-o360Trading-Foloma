#!/usr/bin/env python3
"""
Extrai candles M15 e H1 da API Deriv via WebSocket (ticks_history) e grava
numa base de dados SQLite SEPARADA (forex_backtest_candles.db).

Uso:
    python3 fetch_forex_history.py            # extrai últimos 180 dias
    python3 fetch_forex_history.py 365        # extrai últimos 365 dias

Requer: pip install websockets

NOTAS DE DESIGN:
- Itera do mais recente para o mais antigo. Para quando a API devolve lote vazio
  (i.e., chegou ao limite real de retenção), não antes.
- Schema: PRIMARY KEY (symbol, granularity, epoch) — M15 e H1 coexistem sem colisão.
- Retry: 3 tentativas por lote; se todas falharem, avança para o lote anterior.
- FIX 16.4: cada lote abre a sua própria ligação WS. Se a rede cair a meio,
  só o lote em curso falha — o retry seguinte abre uma ligação nova. Erros
  definitivos da API (ex: símbolo inválido) são distinguidos de erros de rede
  e NÃO disparam retry.
"""
import asyncio
import websockets
import json
import sqlite3
import os
import sys
import time

DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SYMBOLS = ["frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxEURGBP", "frxAUDUSD", "frxUSDCAD"]
GRANULARITIES = [900, 3600]   # M15, H1
GRAN_LABELS = {900: 'M15', 3600: 'H1'}
BATCH_SIZE = 5000
MAX_RETRIES = 3
RETRY_DELAY = 3

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


async def fetch_candles_once(symbol, granularity, start_epoch, end_epoch):
    """
    Uma tentativa. Abre a sua própria ligação WS, pede as velas, fecha.
    Levanta APIError para erros definitivos da API (sem retry).
    Outras exceções (rede, timeout) ficam para o chamador decidir se retry.
    """
    async with websockets.connect(DERIV_WS_URL, ping_interval=20, ping_timeout=20) as ws:
        request = {
            "ticks_history": symbol,
            "style": "candles",
            "granularity": granularity,
            "start": start_epoch,
            "end": end_epoch,
            "adjust_start_time": 1,
            "count": BATCH_SIZE
        }
        await ws.send(json.dumps(request))
        response = await ws.recv()
        data = json.loads(response)
        if 'error' in data:
            # Erro definitivo da API — não retenta
            raise APIError(f"API error: {data['error']}")
        return data.get('candles', [])


async def fetch_candles_with_retry(symbol, granularity, start_epoch, end_epoch):
    """
    Retry até MAX_RETRIES. Cada retry abre uma ligação nova (via fetch_candles_once).
    APIError propaga imediatamente (sem retry).
    """
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return await fetch_candles_once(symbol, granularity, start_epoch, end_epoch)
        except APIError:
            raise
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                print(f"    Retry {attempt + 1}/{MAX_RETRIES} (erro de rede): {e}")
                await asyncio.sleep(RETRY_DELAY)
            else:
                print(f"    Todas as {MAX_RETRIES} tentativas falharam: {e}")
    raise last_error


async def fetch_symbol_granularity(symbol, granularity, start_epoch, end_epoch):
    """
    Itera do mais recente para o mais antigo. Para quando a API devolve lote vazio
    (limite real de retenção atingido), não antes.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    inserted = 0
    current_end = end_epoch
    gran_label = GRAN_LABELS.get(granularity, str(granularity))

    while current_end > start_epoch:
        current_start = max(current_end - (BATCH_SIZE * granularity), start_epoch)

        try:
            candles = await fetch_candles_with_retry(symbol, granularity, current_start, current_end)
        except APIError as e:
            # Erro definitivo — abandonar este símbolo/granularidade
            print(f"  [{symbol}/{gran_label}] Erro da API (não recuperável): {e}")
            break
        except Exception as e:
            # Todas as tentativas de rede falharam neste lote — avança para o lote anterior
            print(f"  [{symbol}/{gran_label}] Lote {current_start}-{current_end} abandonado: {e}")
            current_end = current_start
            continue

        if not candles:
            print(f"  [{symbol}/{gran_label}] Lote vazio em {current_start}-{current_end} → limite de retenção atingido.")
            break

        for c in candles:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?)",
                    (symbol, granularity, c['epoch'], c['open'], c['high'], c['low'], c['close'])
                )
                inserted += 1
            except Exception as e:
                print(f"  [{symbol}/{gran_label}] Erro a inserir candle epoch={c.get('epoch')}: {e}")

        conn.commit()
        print(f"  [{symbol}/{gran_label}] {current_start}-{current_end}: {len(candles)} candles (total: {inserted})")
        current_end = current_start
        await asyncio.sleep(0.5)

    conn.close()
    return inserted


async def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    end_epoch = int(time.time())
    start_epoch = end_epoch - (days * 86400)

    init_db()
    print(f"A puxar {days} dias de candles para {len(SYMBOLS)} pares × {len(GRANULARITIES)} granularidades...")
    print(f"Intervalo: {start_epoch} → {end_epoch}")
    print()

    total = 0
    for symbol in SYMBOLS:
        print(f"=== {symbol} ===")
        for gran in GRANULARITIES:
            try:
                n = await fetch_symbol_granularity(symbol, gran, start_epoch, end_epoch)
                total += n
                print(f"  [{symbol}/{GRAN_LABELS[gran]}] Total: {n} candles")
            except Exception as e:
                print(f"  [{symbol}/{GRAN_LABELS[gran]}] ERRO FATAL: {e}")
        print()

    print(f"✅ Concluído. Total inserido: {total} candles em {DB_PATH}")
    print()
    print("Verificação sugerida:")
    print(f"  sqlite3 {DB_PATH} 'SELECT symbol, granularity, COUNT(*), MIN(epoch), MAX(epoch) FROM candles GROUP BY symbol, granularity;'")


if __name__ == '__main__':
    asyncio.run(main())
