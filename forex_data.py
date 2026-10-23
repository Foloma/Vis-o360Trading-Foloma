import json
import time
import os
import sqlite3
import logging
import threading
import datetime
from collections import deque

logger = logging.getLogger(__name__)

FOREX_SYMBOLS = {
    'frxEURUSD': 'EUR/USD',
    'frxGBPUSD': 'GBP/USD',
    'frxUSDJPY': 'USD/JPY',
    'frxEURGBP': 'EUR/GBP',
    'frxAUDUSD': 'AUD/USD',
    'frxUSDCAD': 'USD/CAD',
}

SYMBOL_ALIASES = {}
for _code, _name in FOREX_SYMBOLS.items():
    SYMBOL_ALIASES[_name.replace('/', '').upper()] = _code
    SYMBOL_ALIASES[_code.upper()] = _code

MAX_TICKS_PER_SYMBOL = 200
CANDLE_REQUEST_TIMEOUT = 60  # Aumentado para 60s para evitar descartes no arranque


class ForexDataManager:
    """
    Gere os dados de Forex recebidos via WebSocket da Deriv.
    Mantém um buffer de ticks para cada par e disponibiliza
    funções de acesso para o cálculo de indicadores.
    Suporta múltiplas granularidades (M1, M5, M15, M30, H1).
    """

    def __init__(self):
        self._ticks = {sym: deque(maxlen=MAX_TICKS_PER_SYMBOL) for sym in FOREX_SYMBOLS}
        self._candles = {
            sym: {
                60: deque(maxlen=300),    # M1
                300: deque(maxlen=300),   # M5
                900: deque(maxlen=300),   # M15
                1800: deque(maxlen=300),  # M30
                3600: deque(maxlen=300)   # H1
            } for sym in FOREX_SYMBOLS
        }
        self._lock = threading.RLock()
        self._client = None

        self._pending_candle_requests = {}
        self._candle_lock = threading.Lock()

    def _normalize_symbol(self, symbol):
        s = symbol.upper().replace('/', '').strip()
        return SYMBOL_ALIASES.get(s, s)

    def set_client(self, client):
        self._client = client

    def subscribe_all(self):
        if not self._client or not self._client.authorized:
            logger.warning("Cliente não autorizado — não foi possível subscrever Forex")
            return

        for symbol in FOREX_SYMBOLS:
            try:
                self._client.ws.send(json.dumps({
                    "ticks": symbol,
                    "subscribe": 1,
                    "req_id": self._client._next_req()
                }))
                self._client.subscribed_symbols.add(symbol)
                logger.info(f"📊 Subscrição de Forex enviada: {symbol}")
            except Exception as e:
                logger.error(f"Erro ao subscrever Forex {symbol}: {e}")

    def on_tick(self, tick_data):
        symbol = tick_data.get('symbol')
        if symbol not in FOREX_SYMBOLS:
            return

        price = tick_data.get('price')
        if price is None:
            return

        timestamp = tick_data.get('timestamp', time.time())

        with self._lock:
            self._ticks[symbol].append({
                'price': float(price),
                'timestamp': float(timestamp)
            })

    def on_candles(self, data, req_id=None):
        candles = data.get('candles', [])
        if not candles:
            return

        symbol = None
        granularity = 60

        if req_id is not None:
            with self._candle_lock:
                entry = self._pending_candle_requests.pop(req_id, None)
                if entry:
                    symbol, granularity, _ = entry
                else:
                    # Retry automático se a resposta for descartada
                    logger.warning(f"⚠️ Resposta descartada (req_id {req_id}). A tentar retry...")
                    echo = data.get('echo_req', {})
                    raw_sym = echo.get('ticks_history')
                    if raw_sym:
                        sym_retry = self._normalize_symbol(raw_sym)
                        gran_retry = echo.get('granularity', 60)
                        count_retry = echo.get('count', 250)
                        logger.info(f"🔄 Retry: pedir novamente {sym_retry} (gran={gran_retry}, count={count_retry})")
                        self.request_candles(sym_retry, granularity=gran_retry, count=count_retry)
                    return

        if symbol is None:
            echo = data.get('echo_req', {})
            raw_sym = echo.get('ticks_history')
            if raw_sym:
                symbol = self._normalize_symbol(raw_sym)
            granularity = echo.get('granularity', granularity)

        if symbol is None:
            first_candle = candles[0] if candles else {}
            raw_sym = first_candle.get('symbol')
            if raw_sym:
                symbol = self._normalize_symbol(raw_sym)

        if symbol is None or symbol not in FOREX_SYMBOLS:
            return

        with self._lock:
            if granularity not in self._candles[symbol]:
                self._candles[symbol][granularity] = deque(maxlen=300)

            existing = self._candles[symbol][granularity]
            last_epoch = existing[-1]['epoch'] if existing else 0
            now = time.time()

            for candle in candles:
                epoch = candle.get('epoch')
                if epoch is None:
                    continue
                
                candle_data = {
                    'epoch': epoch,
                    'open': float(candle['open']),
                    'high': float(candle['high']),
                    'low': float(candle['low']),
                    'close': float(candle['close']),
                    'fetched_at': now
                }
                
                if existing and epoch == existing[-1]['epoch']:
                    existing[-1] = candle_data
                elif epoch > last_epoch:
                    existing.append(candle_data)
                    last_epoch = epoch
                
                try:
                    db_path = os.path.join(os.environ.get('DATA_PATH', '/var/data'), 'foloma.db')
                    conn = sqlite3.connect(db_path, timeout=5)
                    conn.execute(
                        "INSERT OR REPLACE INTO candles (symbol, granularity, epoch, open, high, low, close) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (symbol, granularity, epoch, candle_data['open'], candle_data['high'], candle_data['low'], candle_data['close'])
                    )
                    conn.commit()
                    conn.close()
                except Exception as e:
                    logger.error(f"Erro ao persistir vela na DB: {e}")

    def get_recent_ticks(self, symbol, count=100):
        symbol = self._normalize_symbol(symbol)
        with self._lock:
            ticks = list(self._ticks.get(symbol, []))
            return ticks[-count:] if count else ticks

    def get_recent_candles(self, symbol, count=50, granularity=60, only_closed=True):
        symbol = self._normalize_symbol(symbol)
        with self._lock:
            if symbol not in self._candles or granularity not in self._candles[symbol]:
                return []
            candles = list(self._candles[symbol][granularity])
            if only_closed:
                candles = [c for c in candles if c.get('fetched_at', 0) >= c['epoch'] + granularity]
            return candles[-count:] if count else candles

    def get_latest_price(self, symbol):
        symbol = self._normalize_symbol(symbol)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        is_weekend = False
        if now_utc.weekday() == 4 and now_utc.hour >= 22:
            is_weekend = True
        elif now_utc.weekday() == 5:
            is_weekend = True
        elif now_utc.weekday() == 6 and now_utc.hour < 22:
            is_weekend = True

        if is_weekend:
            return None

        with self._lock:
            ticks = self._ticks.get(symbol, [])
            if ticks:
                last_tick = ticks[-1]
                if time.time() - last_tick['timestamp'] > 60:
                    return None
                return last_tick['price']

            for granularity in [60, 300, 900, 1800, 3600]:
                candles = self._candles.get(symbol, {}).get(granularity, [])
                if candles:
                    return candles[-1]['close']

            return None

    def request_candles(self, symbol, granularity=60, count=50):
        self._cleanup_orphaned_requests()

        symbol = self._normalize_symbol(symbol)
        if symbol not in FOREX_SYMBOLS:
            return None

        if not self._client or not self._client.authorized:
            return None

        req_id = self._client._next_req()
        with self._candle_lock:
            self._pending_candle_requests[req_id] = (symbol, granularity, time.time())

        try:
            self._client.ws.send(json.dumps({
                "ticks_history": symbol,
                "style": "candles",
                "granularity": granularity,
                "count": count,
                "end": "latest",
                "req_id": req_id
            }))
        except Exception as e:
            logger.error(f"Erro ao pedir velas Forex {symbol}: {e}")
            with self._candle_lock:
                self._pending_candle_requests.pop(req_id, None)

    def _cleanup_orphaned_requests(self):
        now = time.time()
        with self._candle_lock:
            expired = [
                req_id for req_id, (_, _, ts) in self._pending_candle_requests.items()
                if now - ts > CANDLE_REQUEST_TIMEOUT
            ]
            for req_id in expired:
                self._pending_candle_requests.pop(req_id, None)

    def get_status(self):
        # NÃO limpa pedidos pendentes aqui para não estragar o /api/status
        with self._lock:
            return {
                'pairs': {
                    sym: {
                        'name': name,
                        'latest_price': self.get_latest_price(sym),
                        'tick_count': len(self._ticks[sym]),
                        'candles_m1': len(self._candles[sym].get(60, [])),
                        'candles_m5': len(self._candles[sym].get(300, [])),
                        'candles_m15': len(self._candles[sym].get(900, [])),
                        'candles_m30': len(self._candles[sym].get(1800, [])),
                        'candles_h1': len(self._candles[sym].get(3600, []))
                    }
                    for sym, name in FOREX_SYMBOLS.items()
                }
            }
