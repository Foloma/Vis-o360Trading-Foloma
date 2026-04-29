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

    # ── Conexão (usa WebSocketApp com run_forever e ping) ──────
    def connect(self):
        if self.ws:
            try:
                self.ws.keep_running = False
                self.ws.close()
            except:
                pass
        self.ws = websocket.WebSocketApp(
            self.config.WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        self.ws_thread = threading.Thread(
            target=lambda: self.ws.run_forever(ping_interval=25, ping_timeout=10),
            daemon=True
        )
        self.ws_thread.start()
        logger.info("🔌 Conectando à Deriv...")

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
            if self.current_symbol:
                self._subscribe_ticks(self.current_symbol)

    def _subscribe_balance(self):
        try:
            self.ws.send(json.dumps({"balance": 1, "subscribe": 1, "req_id": 2}))
            self._balance_subscribed = True
            logger.info("💰 Subscrito a actualizações de saldo")
        except Exception as e:
            logger.error(f"Erro subscrever saldo: {e}")

    def get_balance(self, force=False):
        if not self._balance_subscribed:
            self._subscribe_balance()
        elif force:
            try:
                self.ws.send(json.dumps({"balance": 1, "req_id": 2}))
            except Exception as e:
                logger.error(f"Erro pedir saldo: {e}")

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
        if symbol in self.subscribed_symbols:
            return
        try:
            self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": 3}))
            self.subscribed_symbols.add(symbol)
            self.current_symbol = symbol
            logger.info(f"📊 Subscrito ticks: {symbol}")
        except Exception as e:
            logger.error(f"Erro subscrever ticks: {e}")

    def unsubscribe_ticks(self, symbol):
        if symbol in self.subscribed_symbols:
            try:
                self.ws.send(json.dumps({"forget_all": "ticks", "req_id": 4}))
                self.subscribed_symbols.discard(symbol)
                logger.info(f"📊 Dessubscrito: {symbol}")
            except Exception as e:
                logger.error(f"Erro dessubscrever: {e}")

    def change_symbol(self, symbol):
        if symbol != self.current_symbol:
            self.unsubscribe_ticks(self.current_symbol)
            time.sleep(0.5)
            self._subscribe_ticks(symbol)
            self.current_symbol = symbol

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

    # ── Colocar Trade ──────────────────────────────────────────
    def place_trade(self, contract_type, amount, is_digit=False):
        with self._trade_lock:
            try:
                if not self.authorized:
                    return False
                if self.pending_trade is not None:
                    start = time.time()
                    while self.pending_trade is not None and time.time() - start < 30:
                        time.sleep(0.5)
                    if self.pending_trade is not None:
                        self.pending_trade = None

                original_action = contract_type
                if is_digit:
                    duration = self.config.DIGIT_CONTRACT_DURATION
                    duration_unit = 't'
                    contract_type_full = 'DIGITODD' if contract_type == 'CALL' else 'DIGITEVEN'
                else:
                    duration = self.config.CONTRACT_DURATION
                    duration_unit = self.config.CONTRACT_DURATION_UNIT
                    contract_type_full = 'CALL' if contract_type == 'CALL' else 'PUT'

                self.pending_trade = {
                    'amount': amount,
                    'contract_type': original_action,
                    'is_digit': is_digit,
                    'timestamp': time.time(),
                    'status': 'waiting_proposal'
                }

                self.ws.send(json.dumps({
                    "proposal": 1,
                    "amount": amount,
                    "basis": "stake",
                    "contract_type": contract_type_full,
                    "currency": self.currency,
                    "duration": duration,
                    "duration_unit": duration_unit,
                    "symbol": self.current_symbol,
                    "req_id": 100
                }))
                return True
            except Exception as e:
                logger.error(f"❌ Erro trade: {e}")
                self.pending_trade = None
                return False

    def _on_proposal(self, data):
        try:
            if data.get('error'):
                self.pending_trade = None
                return
            p = data.get('proposal', {})
            pid, ask = p.get('id'), p.get('ask_price')
            if not pid or ask is None:
                self.pending_trade = None
                return
            self.ws.send(json.dumps({"buy": pid, "price": ask, "req_id": 101}))
            if self.pending_trade:
                self.pending_trade['status'] = 'waiting_buy'
                self.pending_trade['proposal_id'] = pid
        except Exception as e:
            logger.error(f"Erro proposta: {e}")
            self.pending_trade = None

    def _on_buy_response(self, data):
        try:
            if data.get('error'):
                self.pending_trade = None
                return
            bd = data.get('buy', {})
            cid, bp = bd.get('contract_id'), bd.get('buy_price', 0)
            if not cid:
                self.pending_trade = None
                return
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
        except Exception as e:
            logger.error(f"Erro compra: {e}")
            self.pending_trade = None

    def _subscribe_contract(self, cid):
        try:
            self.ws.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": cid,
                "subscribe": 1,
                "req_id": 200
            }))
        except Exception as e:
            logger.error(f"Erro sub contrato: {e}")

    def _on_poc(self, data):
        try:
            c = data.get('proposal_open_contract', {})
            cid = c.get('contract_id')
            if not cid or not c.get('is_sold') or cid in self.processed_contracts:
                return
            self.processed_contracts.add(cid)
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
        except Exception as e:
            logger.error(f"Erro poc: {e}")

    def _on_error(self, data):
        logger.error(f"API Error: {data.get('error', {}).get('message', 'desconhecido')}")

    def request_deposit(self, amount, currency, method):
        return {'status': 'pending', 'message': f'Depósito ${amount} solicitado.', 'amount': amount, 'method': method}

    def request_withdrawal(self, amount, currency, method):
        if amount > self.balance:
            return {'error': 'Saldo insuficiente'}
        return {'status': 'pending', 'message': f'Saque ${amount} solicitado.', 'amount': amount, 'method': method}
