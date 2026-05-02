import websocket
import json
import threading
import time
import logging
from collections import deque

logger = logging.getLogger(__name__)

class DerivWebSocketClient:
    # Estados
    ST_DISCONNECTED = 'DISCONNECTED'
    ST_CONNECTING   = 'CONNECTING'
    ST_AUTHORIZING  = 'AUTHORIZING'
    ST_CONNECTED    = 'CONNECTED'
    ST_STREAMING    = 'STREAMING'

    def __init__(self, config, on_tick_callback=None):
        self.config = config
        self.ws = None
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
        self.state = self.ST_DISCONNECTED
        self._auth_time = 0
        self._keep_alive_thread = None
        self._watchdog_thread = None
        self._ws_thread = None

    # ── Dependências ─────────────────────────────────────────────
    def set_digit_analyzer(self, a): self._digit_analyzer = a
    def set_trading_bot(self, b):
        self.trading_bot = b
        if b: b.balance, b.currency, b.client = self.balance, self.currency, self
    def set_payment_system(self, p): self.payment_system = p
    def set_user_token(self, t):
        self.user_token = t
        logger.info("🔑 Token configurado")

    # ── Conexão Pública ──────────────────────────────────────────
    def connect(self):
        if self._stop_event.is_set():
            self._stop_event.clear()
        if self._ws_thread and self._ws_thread.is_alive():
            logger.info("Thread de conexão já ativa")
            return
        self._ws_thread = threading.Thread(target=self._run_forever, daemon=True)
        self._ws_thread.start()
        logger.info("🔌 Thread de ligação iniciada")

    # ── Loop Principal (reconexão automática) ────────────────────
    def _run_forever(self):
        logger.info("▶️ Loop de conexão iniciado")
        backoff = 1
        while not self._stop_event.is_set():
            self._reset_state()
            self.state = self.ST_CONNECTING
            try:
                logger.info("🔌 A ligar à Deriv...")
                self.ws = websocket.create_connection(self.config.WS_URL)
                # Fase de autorização
                self.state = self.ST_AUTHORIZING
                self._authorize()
                # Aguardar resposta de autorização
                if not self._wait_for_authorization(timeout=10):
                    logger.error("Timeout de autorização")
                    continue
                # Subscreen saldo e ticks
                self._subscribe_balance()
                if self.current_symbol:
                    self._subscribe_ticks(self.current_symbol)
                self.state = self.ST_CONNECTED
                logger.info("🟢 Conectado e autorizado")
                # Iniciar keep‑alive e watchdog
                self._start_keep_alive()
                self._start_watchdog()
                # Loop de leitura
                self._read_loop()
            except Exception as e:
                logger.error(f"Erro na conexão: {e}", exc_info=True)
            finally:
                self._teardown_connection()
            if self._stop_event.is_set():
                break
            logger.info(f"🔄 Nova tentativa em {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _reset_state(self):
        self.subscribed_symbols.clear()
        self.pending_trade = None
        self.pending_trade_time = 0
        self.active_trades.clear()
        self._balance_subscribed = False
        self.connected = False
        self.authorized = False
        self.streaming = False
        self._last_tick_time = None
        self.state = self.ST_DISCONNECTED

    # ── Autorização ────────────────────────────────────────────
    def _authorize(self):
        if not self.user_token:
            raise Exception("Token não configurado")
        self.ws.send(json.dumps({"authorize": self.user_token, "req_id": self._next_req()}))
        self._auth_time = time.time()
        logger.info("🔐 Pedido de autorização enviado")

    def _wait_for_authorization(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop_event.is_set():
            try:
                msg = self.ws.recv()
                data = json.loads(msg)
                if data.get('msg_type') == 'authorize':
                    if data.get('error'):
                        logger.error(f"❌ Auth erro: {data['error']}")
                        return False
                    logger.info("✅ Autorizado com sucesso!")
                    self.authorized = True
                    return True
            except Exception as e:
                logger.error(f"Erro ao aguardar autorização: {e}")
                return False
        return False

    # ── Loop de leitura de mensagens ────────────────────────────
    def _read_loop(self):
        while not self._stop_event.is_set():
            try:
                msg = self.ws.recv()
                if not msg:
                    break
                self._on_message(msg)
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                logger.error(f"Erro no recv: {e}")
                break

    def _on_message(self, message):
        try:
            data = json.loads(message)
            msg_type = data.get('msg_type', '')
            if msg_type not in ['tick', 'balance', 'time', 'ping']:
                logger.info(f"📨 [{msg_type}]")
            handlers = {
                'tick':                   self._on_tick,
                'balance':                self._on_balance,
                'proposal':               self._on_proposal,
                'buy':                    self._on_buy_response,
                'proposal_open_contract': self._on_poc,
                'error':                  self._on_api_error,
                'ping':                   self._on_pong,
            }
            handler = handlers.get(msg_type)
            if handler:
                handler(data)
        except json.JSONDecodeError:
            logger.error("Mensagem JSON inválida: %s", message[:200])
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {e}", exc_info=True)

    def _teardown_connection(self):
        self.connected = False
        self.authorized = False
        self.streaming = False
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
            self.ws = None
        self._stop_keep_alive()
        self.state = self.ST_DISCONNECTED

    # ── Keep‑alive e Watchdog ────────────────────────────────────
    def _start_keep_alive(self):
        self._stop_keep_alive()
        self._keep_alive_thread = threading.Thread(target=self._keep_alive_loop, daemon=True)
        self._keep_alive_thread.start()

    def _stop_keep_alive(self):
        if self._keep_alive_thread and self._keep_alive_thread.is_alive():
            self._keep_alive_thread = None

    def _keep_alive_loop(self):
        while not self._stop_event.is_set() and self.ws:
            time.sleep(30)
            if self.ws and self.connected:
                try:
                    self.ws.send(json.dumps({"ping": 1, "req_id": self._next_req()}))
                    logger.debug("📶 Ping manual enviado")
                except Exception as e:
                    logger.warning(f"Falha ao enviar ping: {e}")
                    break
            else:
                break

    def _start_watchdog(self):
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread = None
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _watchdog_loop(self):
        while not self._stop_event.is_set() and self.ws:
            time.sleep(10)
            if self.streaming and self._last_tick_time is not None:
                if time.time() - self._last_tick_time > 60:
                    logger.warning("🛑 Watchdog: >60s sem ticks! Forçando reconexão.")
                    self._close_connection()
                    break
            elif self.connected and not self.streaming:
                if time.time() - self._auth_time > 120:
                    logger.warning("🛑 Watchdog: 120s sem stream. Forçando reconexão.")
                    self._close_connection()
                    break

    def _close_connection(self):
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
            self.ws = None
        self.connected = False
        self.authorized = False
        self.streaming = False

    # ── Saldo ───────────────────────────────────────────────────
    def _subscribe_balance(self):
        try:
            self.ws.send(json.dumps({"balance": 1, "subscribe": 1, "req_id": self._next_req()}))
            self._balance_subscribed = True
        except Exception as e:
            logger.error(f"Erro subs. saldo: {e}")

    def _on_balance(self, data):
        bd = data.get('balance', {})
        if bd:
            self.balance = float(bd.get('balance', 0))
            self.currency = bd.get('currency', 'USD')
            if self.trading_bot:
                self.trading_bot.balance = self.balance
                self.trading_bot.currency = self.currency
            logger.info(f"💰 Saldo: {self.balance:.2f} {self.currency}")

    def get_balance(self, force=False):
        if not self._balance_subscribed:
            self._subscribe_balance()
        elif force:
            try:
                self.ws.send(json.dumps({"balance": 1, "subscribe": 1, "req_id": self._next_req()}))
            except Exception as e:
                logger.error(f"Erro ao pedir saldo: {e}")

    # ── Ticks ───────────────────────────────────────────────────
    def _subscribe_ticks(self, symbol):
        if not self.authorized:
            return
        if symbol in self.subscribed_symbols:
            return
        try:
            self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": self._next_req()}))
            self.subscribed_symbols.add(symbol)
            self.current_symbol = symbol
            logger.info(f"📊 Subscrição de ticks para {symbol} enviada")
        except Exception as e:
            logger.error(f"Erro subs. ticks: {e}")

    def _on_tick(self, data):
        tick = data.get('tick', {})
        if not tick:
            return
        if not self.streaming:
            self.streaming = True
            self.state = self.ST_STREAMING
            logger.info("📡 Estado STREAMING ativado!")
        self._last_tick_time = time.time()
        if self.on_tick_callback:
            self.on_tick_callback({
                'symbol':    tick.get('symbol', self.current_symbol),
                'price':     float(tick.get('quote', 0)),
                'timestamp': tick.get('epoch', time.time())
            })

    # ── Trade ───────────────────────────────────────────────────
    def _next_req(self):
        self._req_counter += 1
        return self._req_counter

    def place_trade(self, contract_type, amount, is_digit=False):
        with self._trade_lock:
            if not self.streaming:
                logger.warning("🚫 Trade bloqueado: sem streaming")
                return False
            if amount > self.balance * 0.02:
                logger.warning("🚫 Trade excede 2% do saldo")
                return False
            if time.time() - self._last_trade_time < 2:
                logger.warning("⏱️ Intervalo mínimo de 2s")
                return False
            if not self.authorized:
                logger.warning("🚫 Não autorizado")
                return False
            if self.pending_trade is not None:
                if time.time() - self.pending_trade_time > 60:
                    self.pending_trade = None
                else:
                    logger.warning("Trade pendente")
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
                'amount': amount,
                'contract_type': contract_type,
                'is_digit': is_digit,
                'timestamp': time.time(),
                'status': 'waiting_proposal',
                'req_id': req_id
            }
            self.pending_trade_time = time.time()

            try:
                self.ws.send(json.dumps({
                    "proposal": 1,
                    "amount": amount,
                    "basis": "stake",
                    "contract_type": contract_type_full,
                    "currency": self.currency,
                    "duration": duration,
                    "duration_unit": duration_unit,
                    "symbol": self.current_symbol,
                    "req_id": req_id
                }))
                logger.info(f"📝 Proposta enviada (req_id={req_id})")
                return True
            except Exception as e:
                logger.error(f"❌ Erro ao enviar trade: {e}")
                self.pending_trade = None
                return False

    # ── Handlers de trade ────────────────────────────────────────
    def _on_proposal(self, data):
        if self.pending_trade is None or data.get('req_id') != self.pending_trade.get('req_id'):
            return
        if data.get('error'):
            self.pending_trade = None
            return
        p = data.get('proposal', {})
        pid, ask = p.get('id'), p.get('ask_price')
        if not pid or ask is None:
            self.pending_trade = None
            return
        if 'proposal_id' in self.pending_trade:
            logger.warning("⚠️ BUY já enviado")
            return
        self.pending_trade['proposal_id'] = pid
        self.ws.send(json.dumps({"buy": pid, "price": ask, "req_id": self._next_req()}))
        logger.info(f"📤 BUY enviado para {pid}")

    def _on_buy_response(self, data):
        if data.get('error'):
            self.pending_trade = None
            return
        bd = data.get('buy', {})
        cid, bp = bd.get('contract_id'), bd.get('buy_price', 0)
        if not cid:
            self.pending_trade = None
            return
        logger.info(f"✅ Contrato {cid} por ${bp}")
        if self.pending_trade:
            amt = self.pending_trade.get('amount', 0)
            action = self.pending_trade.get('contract_type', '')
            if self.trading_bot:
                self.trading_bot.register_trade({
                    'contract_id': cid,
                    'symbol': self.current_symbol,
                    'action': action,
                    'amount': amt,
                    'price': bp,
                    'result': 'pending',
                    'confidence': 70
                })
            self.active_trades[cid] = {
                'contract_id': cid,
                'amount': amt,
                'buy_price': bp,
                'timestamp': time.time(),
                'action': action
            }
            self._subscribe_contract(cid)
            self.pending_trade = None

    def _subscribe_contract(self, cid):
        try:
            self.ws.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": cid,
                "subscribe": 1,
                "req_id": self._next_req()
            }))
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
        bp = c.get('buy_price', 0)
        sp = c.get('sell_price', 0)
        profit = sp - bp
        amt = self.active_trades.get(cid, {}).get('amount', bp)
        logger.info(f"📊 RESULTADO [{cid}]: {'✅ GANHO' if profit > 0 else '❌ PERDA'} ${abs(profit):.2f}")
        if self.trading_bot:
            self.trading_bot.on_trade_result({
                'contract_id': cid,
                'buy_price': bp,
                'sell_price': sp,
                'profit': profit,
                'amount': amt,
                'is_win': profit > 0
            })
        if cid in self.active_trades:
            del self.active_trades[cid]

    def _on_api_error(self, data):
        err = data.get('error', {})
        logger.error(f"Erro da API Deriv: {err.get('message', 'desconhecido')} (código: {err.get('code', 'N/A')})")

    def _on_pong(self, data):
        logger.debug("📶 Pong recebido")

    def request_deposit(self, amount, currency, method):
        return {'status': 'pending', 'message': f'Depósito ${amount} solicitado.', 'amount': amount, 'method': method}

    def request_withdrawal(self, amount, currency, method):
        if amount > self.balance:
            return {'error': 'Saldo insuficiente'}
        return {'status': 'pending', 'message': f'Saque ${amount} solicitado.', 'amount': amount, 'method': method}
