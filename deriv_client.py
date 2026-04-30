import websocket
import json
import threading
import time
import logging

logger = logging.getLogger(__name__)

class DerivWebSocketClient:
    def __init__(self, config, on_tick_callback=None):
        self.config = config
        self.ws = None
        self.ws_thread = None
        self.connected = False
        self.authorized = False
        self.balance = 0
        self.currency = 'USD'
        self.current_symbol = 'R_100'
        self.on_tick_callback = on_tick_callback
        self.trading_bot = None
        self.payment_system = None
        self.markup_percentage = 0
        self.subscribed_symbols = set()
        self.user_token = None
        self.active_trades = {}
        self.pending_trade = None
        self.processed_contracts = set()
        self._trade_lock = threading.Lock()
        self._digit_analyzer = None
        self._balance_subscribed = False
        self._stop_event = threading.Event()

    # ── Dependências ─────────────────────────────────────────────
    def set_digit_analyzer(self, a): self._digit_analyzer = a
    def set_trading_bot(self, b):
        self.trading_bot = b
        if b:
            b.balance = self.balance
            b.currency = self.currency
            b.client = self
    def set_payment_system(self, p): self.payment_system = p
    def set_user_token(self, t):
        self.user_token = t
        logger.info("🔑 Token configurado")

    # ── Conexão (loop manual com ping garantido) ─────────────────
    def connect(self):
        if self.ws_thread and self.ws_thread.is_alive():
            logger.info("Thread de conexão já está em execução")
            return
        self._stop_event.clear()
        self.ws_thread = threading.Thread(target=self._run_forever, daemon=True)
        self.ws_thread.start()
        logger.info("🔌 Thread de conexão iniciada")

    def _run_forever(self):
        while not self._stop_event.is_set():
            # Limpar qualquer estado residual antes de uma nova tentativa
            self.subscribed_symbols.clear()
            self.processed_contracts.clear()
            self.pending_trade = None
            self.active_trades.clear()
            self._balance_subscribed = False
            self.connected = False
            self.authorized = False

            try:
                logger.info("🔌 Tentando ligação à Deriv...")
                self.ws = websocket.WebSocketApp(
                    self.config.WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                # Enviar ping a cada 30 segundos para nunca ultrapassar o idle timeout
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"Erro no loop de conexão: {e}")
            finally:
                self.connected = False
                self.authorized = False
                self._balance_subscribed = False
                if self.ws:
                    self.ws = None
            if not self._stop_event.is_set():
                logger.info("🔄 Reconectando em 2 segundos...")
                time.sleep(2)

    # ── Callbacks do WebSocket ─────────────────────────────────
    def _on_open(self, ws):
        logger.info("✅ WebSocket conectado")
        self.connected = True
        self.authorize()

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get('msg_type', '')
            if msg_type not in ['tick', 'balance', 'time']:
                logger.info(f"📨 [{msg_type}]")
            handlers = {
                'authorize':        self._on_authorize,
                'tick':             self._on_tick,
                'proposal':         self._on_proposal,
                'buy':              self._on_buy_response,
                'proposal_open_contract': self._on_poc,
                'balance':          self._on_balance,
                'error':            self._on_error,
            }
            handler = handlers.get(msg_type)
            if handler:
                handler(data)
        except json.JSONDecodeError:
            logger.error("Mensagem JSON inválida")
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {e}")

    def _on_error(self, ws, error):
        logger.error(f"Erro WS: {error}")

    def _on_close(self, ws, close_code, close_msg):
        logger.warning(f"WebSocket fechado ({close_code}): {close_msg}")
        self.connected = False
        self.authorized = False
        self._balance_subscribed = False

    def authorize(self):
        if not self.user_token:
            logger.error("❌ Token não configurado!")
            return
        try:
            self.ws.send(json.dumps({"authorize": self.user_token, "req_id": 1}))
            logger.info("🔐 A autorizar...")
        except Exception as e:
            logger.error(f"Erro autorizar: {e}")

    def _on_authorize(self, data):
        if data.get('error'):
            logger.error(f"❌ Auth erro: {data['error']}")
            self.authorized = False
        else:
            logger.info("✅ Autorizado com sucesso!")
            self.authorized = True
            self._subscribe_balance()
            # Subscreen automaticamente o símbolo atual (apenas após autorização)
            if self.current_symbol:
                self._subscribe_ticks(self.current_symbol)

    def _subscribe_balance(self):
        try:
            self.ws.send(json.dumps({"balance": 1, "subscribe": 1, "req_id": 2}))
            self._balance_subscribed = True
            logger.info("💰 Subscrito a actualizações de saldo")
        except Exception as e:
            logger.error(f"Erro subscrever saldo: {e}")

    def _on_balance(self, data):
        try:
            bd = data.get('balance', {})
            if bd:
                self.balance = float(bd.get('balance', 0))
                self.currency = bd.get('currency', 'USD')
                if self.trading_bot:
                    self.trading_bot.balance = self.balance
                    self.trading_bot.currency = self.currency
                logger.info(f"💰 Saldo: {self.balance:.2f} {self.currency}")
        except Exception as e:
            logger.error(f"Erro saldo: {e}")

    def _subscribe_ticks(self, symbol):
        """Chamada apenas após autorização. Proteção contra duplicados."""
        if not self.authorized:
            logger.warning("Tentativa de subscrição antes da autorização – ignorando.")
            return
        if symbol in self.subscribed_symbols:
            return
        try:
            self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": 3}))
            self.subscribed_symbols.add(symbol)
            self.current_symbol = symbol
            logger.info(f"📊 Subscrito ticks: {symbol}")
        except Exception as e:
            logger.error(f"Erro subscrever ticks: {e}")

    def _on_tick(self, data):
        try:
            tick = data.get('tick', {})
            if not tick:
                return
            if self.on_tick_callback:
                self.on_tick_callback({
                    'symbol':    tick.get('symbol', self.current_symbol),
                    'price':     float(tick.get('quote', 0)),
                    'timestamp': tick.get('epoch', time.time())
                })
        except Exception as e:
            logger.error(f"Erro no on_tick: {e}")

    # ── Métodos de trade mantidos integralmente ─────────────────
    def place_trade(self, contract_type, amount, is_digit=False):
        # ... (mantenha o código existente)
        pass

    def _on_proposal(self, data):
        # ... (mantenha o código existente)
        pass

    # ... (restantes métodos de trade, inalterados)
