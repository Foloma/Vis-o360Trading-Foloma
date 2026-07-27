import json
import time
import logging
import threading
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
CANDLE_REQUEST_TIMEOUT = 10


class ForexDataManager:
    """
    Gere os dados de Forex recebidos via WebSocket da Deriv.
    Mantém um buffer de ticks para cada par e disponibiliza
    funções de acesso para o cálculo de indicadores.
    Suporta múltiplas granularidades (M1, M5, M15).
    """

    def __init__(self):
        self._ticks = {sym: deque(maxlen=MAX_TICKS_PER_SYMBOL) for sym in FOREX_SYMBOLS}
        self._candles = {
            sym: {
                60: deque(maxlen=300),   # M1
                300: deque(maxlen=300),  # M5
                900: deque(maxlen=300)   # M15
            } for sym in FOREX_SYMBOLS
        }
        self._lock = threading.RLock()
        self._client = None

        # Mapeia req_id -> (symbol, granularity, timestamp)
        self._pending_candle_requests = {}
        self._candle_lock = threading.Lock()

    def _normalize_symbol(self, symbol):
        """Converte qualquer formato (EURUSD, EUR/USD, frxEURUSD) para o código Deriv."""
        s = symbol.upper().replace('/', '').strip()
        return SYMBOL_ALIASES.get(s, s)

    def set_client(self, client):
        """Injeta a instância do DerivWebSocketClient."""
        self._client = client

    def subscribe_all(self):
        """Subscreve todos os pares de Forex no WebSocket da Deriv."""
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
        tick_data deve conter 'symbol', 'price', 'timestamp'.
        """
        symbol = tick_data.get('symbol')
        if symbol not in FOREX_SYMBOLS:
            return  # Não é um par Forex

        price = tick_data.get('price')
        timestamp = tick_data.get('timestamp', time.time())

        with self._lock:
            self._ticks[symbol].append({
                'price': float(price),
                'timestamp': float(timestamp)
            })

    # ========================================================================
    # CORRIGIDO: deduplicação de velas refeita para nunca travar a atualização
    # ========================================================================
    def on_candles(self, data, req_id=None):
        """
        Callback chamado quando uma resposta de ticks_history (candles) chega.
        Usa o req_id para identificar o símbolo e a granularidade.
        """
        candles = data.get('candles', [])
        if not candles:
            return

        symbol = None
        granularity = 60  # fallback

        # 1. Tentar obter do pedido pendente
        if req_id is not None:
            with self._candle_lock:
                entry = self._pending_candle_requests.pop(req_id, None)
                if entry:
                    symbol, granularity, _ = entry

        # 2. Se não encontrou, extrair do echo_req (Deriv reenvia o pedido original)
        if symbol is None:
            echo = data.get('echo_req', {})
            raw_sym = echo.get('ticks_history')
            if raw_sym:
                symbol = self._normalize_symbol(raw_sym)
            granularity = echo.get('granularity', granularity)

        # 3. Último recurso: usar o primeiro candle
        if symbol is None:
            first_candle = candles[0] if candles else {}
            raw_sym = first_candle.get('symbol')
            if raw_sym:
                symbol = self._normalize_symbol(raw_sym)

        if symbol is None or symbol not in FOREX_SYMBOLS:
            logger.debug("Velas recebidas sem símbolo associado — ignorando")
            return

        with self._lock:
            # Garantir que o deque para esta granularidade existe
            if granularity not in self._candles[symbol]:
                self._candles[symbol][granularity] = deque(maxlen=300)

            existing = self._candles[symbol][granularity]
            last_epoch = existing[-1]['epoch'] if existing else 0

            # Processar cada vela individualmente, ignorando apenas as já existentes
            for candle in candles:
                epoch = candle.get('epoch')
                if epoch is None:
                    continue
                if epoch <= last_epoch:
                    # Esta vela (ou uma mais antiga) já está no cache
                    continue
                existing.append({
                    'epoch': epoch,
                    'open': float(candle['open']),
                    'high': float(candle['high']),
                    'low': float(candle['low']),
                    'close': float(candle['close'])
                })
                last_epoch = epoch   # atualizar o último epoch conhecido

            if existing:
                logger.info(f"📈 Velas Forex recebidas: {symbol} (granularity={granularity}, {len(candles)} velas)")

    def get_recent_ticks(self, symbol, count=100):
        """Retorna os últimos `count` ticks para o símbolo pedido."""
        symbol = self._normalize_symbol(symbol)
        with self._lock:
            ticks = list(self._ticks.get(symbol, []))
            return ticks[-count:] if count else ticks

    def get_recent_candles(self, symbol, count=50, granularity=60):
        """Retorna as últimas `count` velas para o símbolo e granularidade pedidos."""
        symbol = self._normalize_symbol(symbol)
        with self._lock:
            if symbol not in self._candles:
                return []
            if granularity not in self._candles[symbol]:
                return []
            candles = list(self._candles[symbol][granularity])
            return candles[-count:] if count else candles

    def get_latest_price(self, symbol):
        """Retorna o preço mais recente de um par, com fallback para o último fecho das velas."""
        symbol = self._normalize_symbol(symbol)
        with self._lock:
            # 1. Tentar tick mais recente
            ticks = self._ticks.get(symbol, [])
            if ticks:
                return ticks[-1]['price']

            # 2. Fallback: último fecho das velas M1 → M5 → M15
            for granularity in [60, 300, 900]:
                candles = self._candles.get(symbol, {}).get(granularity, [])
                if candles:
                    return candles[-1]['close']

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
            logger.info(f"📈 Pedido de velas Forex: {symbol} (granularity={granularity}, req_id={req_id})")
        except Exception as e:
            logger.error(f"Erro ao pedir velas Forex {symbol}: {e}")
            with self._candle_lock:
                self._pending_candle_requests.pop(req_id, None)

    def _cleanup_orphaned_requests(self):
        """Remove pedidos de velas pendentes que expiraram."""
        now = time.time()
        with self._candle_lock:
            expired = [
                req_id for req_id, (_, _, ts) in self._pending_candle_requests.items()
                if now - ts > CANDLE_REQUEST_TIMEOUT
            ]
            for req_id in expired:
                self._pending_candle_requests.pop(req_id, None)
        if expired:
            logger.debug(f"Limpeza de {len(expired)} pedidos de velas Forex órfãos")

    def get_status(self):
        """Retorna um resumo do estado atual do módulo Forex."""
        self._cleanup_orphaned_requests()
        with self._lock:
            return {
                'pairs': {
                    sym: {
                        'name': name,
                        'latest_price': self.get_latest_price(sym),
                        'tick_count': len(self._ticks[sym]),
                        'candles_m1': len(self._candles[sym].get(60, [])),
                        'candles_m5': len(self._candles[sym].get(300, [])),
                        'candles_m15': len(self._candles[sym].get(900, []))
                    }
                    for sym, name in FOREX_SYMBOLS.items()
                }
            }
