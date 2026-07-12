import json
import time
import logging
import threading
from collections import deque

logger = logging.getLogger(__name__)

# Pares de Forex suportados (código Deriv)
FOREX_SYMBOLS = {
    'frxEURUSD': 'EUR/USD',
    'frxGBPUSD': 'GBP/USD',
    'frxUSDJPY': 'USD/JPY',
    'frxEURGBP': 'EUR/GBP',
    'frxAUDUSD': 'AUD/USD',
    'frxUSDCAD': 'USD/CAD',
}

# Mapeamento de formatos amigáveis para código Deriv
SYMBOL_ALIASES = {}
for _code, _name in FOREX_SYMBOLS.items():
    # frxEURUSD -> eurusd
    SYMBOL_ALIASES[_name.replace('/', '').upper()] = _code
    SYMBOL_ALIASES[_code.upper()] = _code

# Máximo de ticks por par (para cálculo de indicadores)
MAX_TICKS_PER_SYMBOL = 200

# Tempo máximo de vida de um pedido de velas pendente (segundos)
CANDLE_REQUEST_TIMEOUT = 10


class ForexDataManager:
    """
    Gere os dados de Forex recebidos via WebSocket da Deriv.
    Mantém um buffer de ticks para cada par e disponibiliza
    funções de acesso para o cálculo de indicadores.
    Suporta receção assíncrona de velas (candles) via callback.
    """

    def __init__(self):
        self._ticks = {sym: deque(maxlen=MAX_TICKS_PER_SYMBOL) for sym in FOREX_SYMBOLS}
        self._candles = {sym: deque(maxlen=300) for sym in FOREX_SYMBOLS}
        self._lock = threading.RLock()
        self._client = None  # Será injetado depois

        # Mapeia req_id -> (symbol, timestamp) para associar respostas de velas
        self._pending_candle_requests = {}
        self._candle_lock = threading.Lock()

    def _normalize_symbol(self, symbol):
        """Converte qualquer formato (EURUSD, EUR/USD, frxEURUSD) para o código Deriv."""
        s = symbol.upper().replace('/', '').strip()
        return SYMBOL_ALIASES.get(s, s)

    def set_client(self, client):
        """
        Injeta a instância do DerivWebSocketClient para podermos
        subscrever ticks de Forex.
        """
        self._client = client

    def subscribe_all(self):
        """
        Subscreve todos os pares de Forex no WebSocket da Deriv.
        """
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
        """
        Callback chamado quando um tick de Forex é recebido.
        tick_data é o dicionário com 'symbol', 'price', 'timestamp'.
        """
        symbol = tick_data.get('symbol')
        if symbol not in FOREX_SYMBOLS:
            return  # Não é um par Forex que nos interessa

        price = tick_data.get('price')
        timestamp = tick_data.get('timestamp', time.time())

        with self._lock:
            self._ticks[symbol].append({
                'price': float(price),
                'timestamp': float(timestamp)
            })

    def on_candles(self, data, req_id=None):
        """
        Callback chamado quando uma resposta de ticks_history (candles) chega.
        Pode ser chamado diretamente pelo DerivWebSocketClient se este
        suportar encaminhamento com req_id, ou via on_candles_callback.
        """
        candles = data.get('candles', [])
        if not candles:
            return

        # Se temos req_id, procuramos o símbolo associado
        symbol = None
        if req_id is not None:
            with self._candle_lock:
                entry = self._pending_candle_requests.pop(req_id, None)
                if entry:
                    symbol = entry[0]

        # Se não encontramos pelo req_id, tentamos inferir pelo primeiro candle
        if symbol is None:
            # O campo 'symbol' pode não estar presente; tentar extrair
            first_candle = candles[0] if candles else {}
            raw_sym = first_candle.get('symbol', data.get('echo_req', {}).get('ticks_history'))
            if raw_sym:
                symbol = self._normalize_symbol(raw_sym)

        # Se mesmo assim não temos símbolo, ignoramos
        if symbol is None or symbol not in FOREX_SYMBOLS:
            logger.debug("Velas recebidas sem símbolo associado — ignorando")
            return

        with self._lock:
            # Proteção contra duplicados: verificar se a última vela já existe
            existing = self._candles[symbol]
            if existing and candles:
                last_existing = existing[-1]
                first_new = candles[0]
                if (last_existing.get('epoch') == first_new.get('epoch') and
                    last_existing.get('close') == float(first_new.get('close', 0))):
                    # Já temos estas velas — ignorar
                    return

            for candle in candles:
                self._candles[symbol].append({
                    'epoch': candle.get('epoch'),
                    'open': float(candle['open']),
                    'high': float(candle['high']),
                    'low': float(candle['low']),
                    'close': float(candle['close'])
                })

            logger.info(f"📈 Velas Forex recebidas: {symbol} ({len(candles)} velas)")

    def get_recent_ticks(self, symbol, count=100):
        """
        Retorna os últimos `count` ticks para o símbolo pedido.
        """
        symbol = self._normalize_symbol(symbol)
        with self._lock:
            ticks = list(self._ticks.get(symbol, []))
            return ticks[-count:] if count else ticks

    def get_recent_candles(self, symbol, count=50):
        """
        Retorna as últimas `count` velas para o símbolo pedido.
        """
        symbol = self._normalize_symbol(symbol)
        with self._lock:
            candles = list(self._candles.get(symbol, []))
            return candles[-count:] if count else candles

    def get_latest_price(self, symbol):
        """
        Retorna o preço mais recente de um par.
        """
        symbol = self._normalize_symbol(symbol)
        with self._lock:
            ticks = self._ticks.get(symbol, [])
            if ticks:
                return ticks[-1]['price']
            return None

    def request_candles(self, symbol, granularity=60, count=50):
        """
        Pede velas (candles) à Deriv para um par Forex.
        Regista o req_id para associar a resposta futura.
        """
        symbol = self._normalize_symbol(symbol)
        if symbol not in FOREX_SYMBOLS:
            logger.error(f"Símbolo Forex inválido: {symbol}")
            return None

        if not self._client or not self._client.authorized:
            logger.warning("Cliente não autorizado — não foi possível pedir velas Forex")
            return None

        req_id = self._client._next_req()
        with self._candle_lock:
            self._pending_candle_requests[req_id] = (symbol, time.time())

        try:
            self._client.ws.send(json.dumps({
                "ticks_history": symbol,
                "style": "candles",
                "granularity": granularity,
                "count": count,
                "end": "latest",
                "req_id": req_id
            }))
            logger.info(f"📈 Pedido de velas Forex: {symbol} (req_id={req_id})")
        except Exception as e:
            logger.error(f"Erro ao pedir velas Forex {symbol}: {e}")
            with self._candle_lock:
                self._pending_candle_requests.pop(req_id, None)

    def _cleanup_orphaned_requests(self):
        """Remove pedidos de velas pendentes que expiraram."""
        now = time.time()
        with self._candle_lock:
            expired = [
                req_id for req_id, (_, ts) in self._pending_candle_requests.items()
                if now - ts > CANDLE_REQUEST_TIMEOUT
            ]
            for req_id in expired:
                self._pending_candle_requests.pop(req_id, None)
        if expired:
            logger.debug(f"Limpeza de {len(expired)} pedidos de velas Forex órfãos")

    def get_status(self):
        """
        Retorna um resumo do estado atual do módulo Forex.
        """
        self._cleanup_orphaned_requests()
        with self._lock:
            return {
                'pairs': {
                    sym: {
                        'name': name,
                        'latest_price': self.get_latest_price(sym),
                        'tick_count': len(self._ticks[sym]),
                        'candle_count': len(self._candles[sym])
                    }
                    for sym, name in FOREX_SYMBOLS.items()
                }
            }
