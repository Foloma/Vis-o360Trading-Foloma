import websocket
import json
import threading
import time
import logging
from collections import deque

logger = logging.getLogger(__name__)

class DerivWebSocketClient:
    # Estados da conexão
    ST_CONNECTING   = 'CONNECTING'
    ST_CONNECTED    = 'CONNECTED'
    ST_STREAMING    = 'STREAMING'
    ST_STALE        = 'STALE'
    ST_RECONNECTING = 'RECONNECTING'

    def __init__(self, config, on_tick_callback=None):
        self.config = config
        self.ws = None
        self.ws_thread = None
        self.connected = False
        self.authorized = False
        self.streaming = False
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
        self.pending_trade_time = 0
        self._trade_lock = threading.Lock()
        self._digit_analyzer = None
        self._balance_subscribed = False
        self._stop_event = threading.Event()
        self._last_tick_time = None
        self._last_trade_time = 0
        self._processed_contracts = deque(maxlen=1000)
        self._processed_lock = threading.Lock()
        self._req_counter = 1000
        self.connection_state = self.ST_CONNECTING
        self._watchdog_enabled = False
        self._thread_monitor_enabled = False

    # ── Dependências ─────────────────────────────────────────────
    def set_digit_analyzer(self, a): self._digit_analyzer = a
    def set_trading_bot(self, b):
        self.trading_bot = b
        if b: b.balance, b.currency, b.client = self.balance, self.currency, self
    def set_payment_system(self, p): self.payment_system = p
    def set_user_token(self, t):
        self.user_token = t
        logger.info("🔑 Token configurado")

    # ── Conexão ──────────────────────────────────────────────────
    def connect(self):
        # Garantir que nenhum evento de paragem fica ativo
        self._stop_event.clear()
        # Se a thread antiga ainda estiver a correr, deixamos que ela própria termine
        if self.ws_thread and self.ws_thread.is_alive():
            logger.info("Thread de conexão já está ativa – a monitorizar.")
            self._start_thread_monitor()
            return
        logger.info("🔌 A iniciar thread de conexão...")
        self.ws_thread = threading.Thread(target=self._run_forever, daemon=True)
        self.ws_thread.start()
        self._start_thread_monitor()

    def _start_thread_monitor(self):
        """Vigia a thread de ligação e reinicia-a se morrer."""
        if self._thread_monitor_enabled:
            return
        self._thread_monitor_enabled = True
        def monitor():
            while not self._stop_event.is_set():
                time.sleep(10)
                if self.ws_thread is None or not self.ws_thread.is_alive():
                    logger.warning("🔄 Thread de ligação morreu! Reiniciando...")
                    self.ws_thread = threading.Thread(target=self._run_forever, daemon=True)
                    self.ws_thread.start()
        threading.Thread(target=monitor, daemon=True).start()

    def _run_forever(self):
        logger.info("▶️ Thread _run_forever iniciada")
        while not self._stop_event.is_set():
            self._cleanup_state()
            self.connection_state = self.ST_CONNECTING
            try:
                logger.info("🔌 A ligar à Deriv...")
                self.ws = websocket.WebSocketApp(
                    self.config.WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_close
                )
                self.ws.run_forever(ping_interval=15, ping_timeout=10)
            except Exception as e:
                logger.error(f"Erro no loop de conexão: {e}", exc_info=True)
            finally:
                self.connected, self.authorized, self.streaming = False, False, False
                if self.ws: self.ws = None
            if not self._stop_event.is_set():
                self.connection_state = self.ST_RECONNECTING
                logger.info("🔄 A aguardar 2 segundos antes de nova tentativa...")
                time.sleep(2)

    def _cleanup_state(self):
        self.subscribed_symbols.clear()
        self.pending_trade = None
        self.pending_trade_time = 0
        self.active_trades.clear()
        self._balance_subscribed = False
        self.connected, self.authorized, self.streaming = False, False, False
        self._last_tick_time = None

    # ── Callbacks do WebSocket ──────────────────────────────────
    def _on_open(self, ws):
        logger.info("✅ WebSocket conectado (on_open)")
        self.connected = True
        self.connection_state = self.ST_CONNECTED
        self.authorize()

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get('msg_type', '')
            if msg_type not in ['tick', 'balance', 'time']:
                logger.info(f"📨 [{msg_type}]")
            handlers = {
                'authorize':              self._on_authorize,
                'tick':                   self._on_tick,
                'proposal':               self._on_proposal,
                'buy':                    self._on_buy_response,
                'proposal_open_contract': self._on_poc,
                'balance':                self._on_balance,
                'error':                  self._on_api_error,
            }
            handler = handlers.get(msg_type)
            if handler: handler(data)
        except json.JSONDecodeError:
            logger.error("Mensagem JSON inválida recebida: %s", message[:200])
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {e}", exc_info=True)

    def _on_ws_error(self, ws, error):
        logger.error(f"Erro de conexão WebSocket: {error}")

    def _on_close(self, ws, close_code, close_msg):
        logger.warning(f"WebSocket fechado (código {close_code}): {close_msg}")
        self.connected, self.authorized, self.streaming = False, False, False
        self.connection_state = self.ST_STALE

    # ── Watchdog ─────────────────────────────────────────────────
    def _start_watchdog(self):
        if self._watchdog_enabled:
            return
        self._watchdog_enabled = True
        def watchdog():
            while not self._stop_event.is_set():
                time.sleep(10)
                if not self.connected:
                    continue
                if self.streaming and self._last_tick_time is not None:
                    if time.time() - self._last_tick_time > 60:
                        logger.warning("🛑 Watchdog: >60s sem ticks. Forçando reconexão.")
                        self.streaming = False
                        try: self.ws.close()
                        except: pass
                        self.connected, self.authorized = False, False
                elif self.connected and not self.streaming:
                    if self._auth_time and time.time() - self._auth_time > 90:
                        logger.warning("🛑 Watchdog: 90s ligado sem stream. Forçando reconexão.")
                        try: self.ws.close()
                        except: pass
                        self.connected, self.authorized = False, False
        threading.Thread(target=watchdog, daemon=True).start()

    # ── Autenticação ────────────────────────────────────────────
    def authorize(self):
        if not self.user_token:
            logger.error("❌ Token não configurado!"); return
        try:
            self.ws.send(json.dumps({"authorize": self.user_token, "req_id": self._next_req()}))
            self._auth_time = time.time()
            logger.info("🔐 Pedido de autorização enviado")
        except Exception as e: logger.error(f"Erro autorizar: {e}")

    def _on_authorize(self, data):
        logger.info("📬 Resposta de autorização recebida")
        if data.get('error'):
            self.authorized = False
            logger.error(f"❌ Auth erro: {data['error']}")
        else:
            logger.info("✅ Autorizado com sucesso!")
            self.authorized = True
            time.sleep(0.5)
            self._subscribe_balance()
            if self.current_symbol:
                self._subscribe_ticks(self.current_symbol)
            self._start_watchdog()

    # ── Saldo ───────────────────────────────────────────────────
    def _subscribe_balance(self):
        try:
            self.ws.send(json.dumps({"balance": 1, "subscribe": 1, "req_id": self._next_req()}))
            self._balance_subscribed = True
        except Exception as e: logger.error(f"Erro subs. saldo: {e}")

    def _on_balance(self, data):
        bd = data.get('balance', {})
        if bd:
            self.balance = float(bd.get('balance', 0))
            self.currency = bd.get('currency', 'USD')
            if self.trading_bot: self.trading_bot.balance, self.trading_bot.currency = self.balance, self.currency

    # ── Ticks ───────────────────────────────────────────────────
    def _subscribe_ticks(self, symbol):
        if not self.authorized: return
        if symbol in self.subscribed_symbols: return
        try:
            self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": self._next_req()}))
            self.subscribed_symbols.add(symbol)
            self.current_symbol = symbol
            logger.info(f"📊 Subscrição de ticks para {symbol} enviada")
        except Exception as e: logger.error(f"Erro subs. ticks: {e}")

    def _on_tick(self, data):
        tick = data.get('tick', {})
        if not tick: return
        if not self.streaming:
            self.streaming = True
            self.connection_state = self.ST_STREAMING
            logger.info("📡 Estado STREAMING ativado! Primeiro tick real recebido.")
        self._last_tick_time = time.time()
        if self.on_tick_callback:
            self.on_tick_callback({
                'symbol':    tick.get('symbol', self.current_symbol),
                'price':     float(tick.get('quote', 0)),
                'timestamp': tick.get('epoch', time.time())
            })

    # ── Colocar Trade ──────────────────────────────────────────
    def _next_req(self):
        self._req_counter += 1
        return self._req_counter

    def place_trade(self, contract_type, amount, is_digit=False):
        with self._trade_lock:
            # Bloquear trades se não houver stream ativo
            if not self.streaming:
                logger.warning("🚫 Trade bloqueado: sistema não está em estado STREAMING.")
                return False
            if amount > self.balance * 0.02:
                logger.warning("🚫 Trade bloqueado: excede 2% do saldo.")
                return False
            if time.time() - self._last_trade_time < 2:
                logger.warning("⏱️ Trade bloqueado: intervalo mínimo de 2s.")
                return False
            if not self.authorized:
                logger.warning("🚫 Trade bloqueado: não autorizado.")
                return False
            if self.pending_trade is not None:
                if time.time() - self.pending_trade_time > 60:
                    logger.warning("Trade pendente expirou (60s). Forçando limpeza.")
                    self.pending_trade = None
                else:
                    logger.warning("⚠️ Já existe um trade pendente. Aguarde.")
                    return False

            self._last_trade_time = time.time()
            if is_digit:
                duration = self.config.DIGIT_CONTRACT_DURATION
                duration_unit = 't'
                contract_type_full = 'DIGITODD' if contract_type == 'CALL' else 'DIGITEVEN'
            else:
                duration = self.config.CONTRACT_DURATION
                duration_unit = self.config.CONTRACT_DURATION_UNIT
                contract_type_full = 'CALL' if contract_type == 'CALL' else 'PUT'

            req_id = self._next_req()
            self.pending_trade = {
                'amount': amount, 'contract_type': contract_type,
                'is_digit': is_digit, 'timestamp': time.time(),
                'status': 'waiting_proposal', 'req_id': req_id
            }
            self.pending_trade_time = time.time()

            try:
                self.ws.send(json.dumps({
                    "proposal": 1, "amount": amount, "basis": "stake",
                    "contract_type": contract_type_full, "currency": self.currency,
                    "duration": duration, "duration_unit": duration_unit,
                    "symbol": self.current_symbol, "req_id": req_id
                }))
                logger.info(f"📝 Proposta de trade enviada (req_id={req_id})")
                return True
            except Exception as e:
                logger.error(f"❌ Erro ao enviar trade: {e}")
                self.pending_trade = None
                return False

    # ── Fluxo de Proposta / Compra ────────────────────────────────
    def _on_proposal(self, data):
        if self.pending_trade is None:
            logger.info("Proposta recebida, mas sem trade pendente. Ignorando.")
            return
        if data.get('req_id') != self.pending_trade.get('req_id'):
            logger.info("Proposta ignorada (req_id não coincide).")
            return
        if data.get('error'):
            logger.error(f"❌ Erro na proposta: {data['error']}")
            self.pending_trade = None
            return
        p = data.get('proposal', {})
        pid, ask = p.get('id'), p.get('ask_price')
        if not pid or ask is None:
            logger.error("Proposta inválida (sem id ou ask_price).")
            self.pending_trade = None
            return
        if self.pending_trade and 'proposal_id' in self.pending_trade:
            logger.warning("⚠️ BUY já enviado para esta proposta. Ignorando duplicado.")
            return
        self.pending_trade['proposal_id'] = pid
        self.ws.send(json.dumps({"buy": pid, "price": ask, "req_id": self._next_req()}))
        logger.info(f"📤 Ordem BUY enviada para proposta {pid}")

    def _on_buy_response(self, data):
        if data.get('error'):
            logger.error(f"❌ Erro na compra: {data['error']}")
            self.pending_trade = None
            return
        bd = data.get('buy', {})
        cid, bp = bd.get('contract_id'), bd.get('buy_price', 0)
        if not cid:
            logger.error("Resposta de compra sem contract_id.")
            self.pending_trade = None
            return
        logger.info(f"✅ Contrato adquirido: {cid} por ${bp}")
        if self.pending_trade:
            amt, action = self.pending_trade.get('amount', 0), self.pending_trade.get('contract_type', '')
            if self.trading_bot:
                self.trading_bot.register_trade({
                    'contract_id': cid, 'symbol': self.current_symbol,
                    'action': action, 'amount': amt, 'price': bp,
                    'result': 'pending', 'confidence': 70
                })
            self.active_trades[cid] = {
                'contract_id': cid, 'amount': amt, 'buy_price': bp,
                'timestamp': time.time(), 'action': action
            }
            self._subscribe_contract(cid)
            self.pending_trade = None

    def _subscribe_contract(self, cid):
        try:
            self.ws.send(json.dumps({
                "proposal_open_contract": 1, "contract_id": cid,
                "subscribe": 1, "req_id": self._next_req()
            }))
            logger.info(f"📡 Subscrição de contrato {cid} enviada")
        except Exception as e:
            logger.error(f"Erro ao subscrever contrato {cid}: {e}")

    def _on_poc(self, data):
        c = data.get('proposal_open_contract', {})
        cid = c.get('contract_id')
        if not cid or not c.get('is_sold'):
            return
        with self._processed_lock:
            if cid in self._processed_contracts:
                return
            self._processed_contracts.append(cid)
        bp, sp = c.get('buy_price', 0), c.get('sell_price', 0)
        profit = sp - bp
        amt = self.active_trades.get(cid, {}).get('amount', bp)
        logger.info(f"📊 RESULTADO [{cid}]: {'✅ GANHO' if profit > 0 else '❌ PERDA'} ${abs(profit):.2f}")
        if self.trading_bot:
            self.trading_bot.on_trade_result({
                'contract_id': cid, 'buy_price': bp, 'sell_price': sp,
                'profit': profit, 'amount': amt, 'is_win': profit > 0
            })
        if cid in self.active_trades:
            del self.active_trades[cid]

    def _on_api_error(self, data):
        err = data.get('error', {})
        logger.error(f"Erro da API Deriv: {err.get('message', 'desconhecido')} (código: {err.get('code', 'N/A')})")

    def request_deposit(self, amount, currency, method):
        return {'status': 'pending', 'message': f'Depósito ${amount} solicitado.', 'amount': amount, 'method': method}

    def request_withdrawal(self, amount, currency, method):
        if amount > self.balance: return {'error': 'Saldo insuficiente'}
        return {'status': 'pending', 'message': f'Saque ${amount} solicitado.', 'amount': amount, 'method': method}
