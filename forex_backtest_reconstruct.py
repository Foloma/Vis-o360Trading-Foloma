"""
Reconstrói indicadores para sinais históricos de forex_signal_log.

⚠️ AVISO METODOLÓGICO:
Os valores aqui obtidos são RECALCULADOS retroativamente a partir de velas
históricas da Deriv. NÃO são necessariamente o que o sistema viu no momento
do sinal (por causa de only_closed=True, maturidade da vela, ajustes em tempo real).

Uso legítimo: teste EXPLORATÓRIO da hipótese trend-vs-reversão.
Uso ilegítimo: prova de edge. Para isso, usar apenas dados do logging estendido (Opção B).
"""

import os
import csv
import json
import time
import sqlite3
import logging
import websocket
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

DATA_PATH = os.environ.get('DATA_PATH', '/var/data')
DB_PATH = os.path.join(DATA_PATH, 'foloma.db')
OUTPUT_CSV = os.path.join(DATA_PATH, 'forex_backtest_reconstruction.csv')
WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"

# Janela à volta do timestamp do sinal para reconstruir as velas necessárias
CANDLES_BEFORE = 60   # M15: 60 velas = 15h antes do sinal, suficiente para ADX(14) + warmup
CANDLES_AFTER  = 5    # para confirmar preço pós-sinal


# ============================================================
# EXTRAÇÃO DOS SINAIS HISTÓRICOS
# ============================================================
def extrair_sinais():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT id, symbol, direction, signal_type, confidence,
               breakdown_json, price_at_signal, timestamp,
               outcome, price_after
        FROM forex_signal_log
        WHERE signal_type = 'ensemble'
          AND outcome IN ('win','loss')
          AND evaluated = 1
        ORDER BY timestamp ASC
    """).fetchall()
    conn.close()

    sinais = []
    for r in rows:
        sinais.append({
            'id': r[0], 'symbol': r[1], 'direction': r[2], 'signal_type': r[3],
            'confidence': r[4], 'breakdown_json': r[5], 'price_at_signal': r[6],
            'timestamp': r[7], 'outcome': r[8], 'price_after': r[9]
        })
    logger.info(f"Extraídos {len(sinais)} sinais históricos do ensemble com outcome.")
    return sinais


# ============================================================
# CLIENTE WS SIMPLES PARA PEDIR CANDLES
# ============================================================
class CandleFetcher:
    def __init__(self, url=WS_URL, timeout=15):
        self.url = url
        self.timeout = timeout
        self._req_counter = 1000
        self._lock = None

    def _next_req(self):
        self._req_counter += 1
        return self._req_counter

    def fetch_range(self, symbol, granularity, start_epoch, count):
        """
        Pede `count` velas a partir de `start_epoch` (end = start + count*granularity).
        A API devolve velas por `end` = "latest" ou epoch. Usamos end explícito.
        """
        end_epoch = start_epoch + count * granularity

        ws = websocket.create_connection(self.url, timeout=self.timeout)
        try:
            req_id = self._next_req()
            ws.send(json.dumps({
                "ticks_history": symbol,
                "style": "candles",
                "granularity": granularity,
                "start": start_epoch,
                "end": end_epoch,
                "count": count,
                "adjust_start_time": 1,
                "req_id": req_id
            }))

            deadline = time.time() + self.timeout
            while time.time() < deadline:
                msg = ws.recv()
                if not msg:
                    return None
                data = json.loads(msg)
                if data.get('msg_type') != 'candles':
                    continue
                if data.get('req_id') != req_id:
                    continue
                if data.get('error'):
                    logger.error(f"Erro candles {symbol}: {data['error']}")
                    return None
                candles = data.get('candles', [])
                if not candles:
                    return None
                return candles
            return None
        except Exception as e:
            logger.error(f"Erro ao pedir candles {symbol}: {e}")
            return None
        finally:
            try:
                ws.close()
            except Exception:
                pass


# ============================================================
# RECÁLCULO DE INDICADORES (espelho do forex_indicators.py)
# ============================================================
def _ema(prices, period):
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = (p - ema) * k + ema
    return ema


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0, diff))
        losses.append(max(0, -diff))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    if avg_l == 0:
        return 100.0
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    rs = avg_g / avg_l if avg_l else 0
    return 100 - (100 / (1 + rs))


def _adx(candles, period=14):
    if len(candles) < period * 2:
        return None
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    closes = [c['close'] for c in candles]

    tr, pdm, mdm = [], [], []
    for i in range(1, len(candles)):
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        pdm.append(up if up > dn and up > 0 else 0)
        mdm.append(dn if dn > up and dn > 0 else 0)

    if len(tr) < period * 2:
        return None

    atr = sum(tr[:period]) / period
    pdi = (sum(pdm[:period]) / period) / atr * 100 if atr else 0
    mdi = (sum(mdm[:period]) / period) / atr * 100 if atr else 0
    dxs = []
    den = pdi + mdi
    dxs.append(abs(pdi - mdi) / den * 100 if den else 0)

    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period
        pdi = (pdi * (period - 1) + (pdm[i] / atr * 100 if atr else 0)) / period
        mdi = (mdi * (period - 1) + (mdm[i] / atr * 100 if atr else 0)) / period
        den = pdi + mdi
        dxs.append(abs(pdi - mdi) / den * 100 if den else 0)

    if len(dxs) < period:
        return round(sum(dxs) / len(dxs), 2)
    return round(sum(dxs[-period:]) / period, 2)


def _bollinger(candles, period=20, std_dev=2):
    if len(candles) < period:
        return None, None, None
    prices = [c['close'] for c in candles[-period:]]
    middle = sum(prices) / period
    var = sum((p - middle) ** 2 for p in prices) / period
    std = var ** 0.5
    return middle + std_dev * std, middle, middle - std_dev * std


def _momentum(candles, period=10):
    if len(candles) < period + 1:
        return None
    return candles[-1]['close'] - candles[-period - 1]['close']


# ============================================================
# RECONSTRUÇÃO DE UM SINAL
# ============================================================
def reconstruir_sinal(sinal, fetcher):
    """
    Pede velas M15 suficientes para cobrir o sinal e recalcula indicadores
    com a última vela ANTES do timestamp do sinal (only_closed=True implícito).
    """
    symbol = sinal['symbol']
    ts = sinal['timestamp']

    # Arredondar para o início do candle M15 que contém o sinal
    candle_start = int(ts // 900) * 900

    # Pedir velas que terminem no candle anterior ao sinal
    end_epoch = candle_start
    start_epoch = end_epoch - CANDLES_BEFORE * 900

    candles = fetcher.fetch_range(symbol, 900, start_epoch, CANDLES_BEFORE)
    if not candles or len(candles) < 30:
        logger.warning(f"Sinal {sinal['id']} ({symbol}): velas insuficientes.")
        return None

    # Última vela fechada ANTES do sinal (aproximação)
    candles_closed = [c for c in candles if c['epoch'] < candle_start]
    if len(candles_closed) < 30:
        logger.warning(f"Sinal {sinal['id']} ({symbol}): poucas velas fechadas.")
        return None

    closes = [c['close'] for c in candles_closed]
    price = closes[-1]

    rsi = _rsi(closes, 14)
    adx = _adx(candles_closed, 14)
    ema50 = _ema(closes, 50)
    upper, middle, lower = _bollinger(candles_closed, 20)
    momentum = _momentum(candles_closed, 10)

    ema_dist_pct = ((price - ema50) / ema50 * 100) if ema50 else None
    pct_b = None
    if upper is not None and lower is not None and upper > lower:
        pct_b = (price - lower) / (upper - lower)

    return {
        'id': sinal['id'],
        'symbol': symbol,
        'timestamp_iso': datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        'direction': sinal['direction'],
        'confidence': sinal['confidence'],
        'outcome': sinal['outcome'],
        'price_at_signal': sinal['price_at_signal'],
        'price_after': sinal['price_after'],
        # valores recalculados:
        'rsi_at_signal': round(rsi, 2) if rsi is not None else None,
        'adx_at_signal': adx,
        'pct_b_at_signal': round(pct_b, 4) if pct_b is not None else None,
        'ema_dist_pct_at_signal': round(ema_dist_pct, 4) if ema_dist_pct is not None else None,
        'momentum_at_signal': round(momentum, 6) if momentum is not None else None,
    }


# ============================================================
# MAIN
# ============================================================
def main():
    sinais = extrair_sinais()
    if not sinais:
        logger.warning("Nenhum sinal elegível encontrado.")
        return

    fetcher = CandleFetcher()
    resultados = []
    total = len(sinais)

    for i, s in enumerate(sinais, 1):
        logger.info(f"[{i}/{total}] Reconstruir sinal {s['id']} ({s['symbol']})")
        r = reconstruir_sinal(s, fetcher)
        if r:
            resultados.append(r)
        # evitar rate limit
        time.sleep(0.3)

    if not resultados:
        logger.warning("Nenhum resultado reconstruído.")
        return

    with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(resultados[0].keys()))
        writer.writeheader()
        writer.writerows(resultados)

    logger.info(f"CSV escrito: {OUTPUT_CSV} ({len(resultados)} linhas)")


if __name__ == '__main__':
    main()
