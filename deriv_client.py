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
        self.reconnect_attempts = 0
        self.should_reconnect = True
        self.processed_contracts = set()
        self._trade_lock = threading.Lock()
        self._digit_analyzer = None
        self._balance_subscribed = False

    def set_digit_analyzer(self, a):
        self._digit_analyzer = a

    def set_trading_bot(self, b):
        self.trading_bot = b

    def set_payment_system(self, p):
        self.payment_system = p

    def set_user_token(self, t):
        self.user_token = t
        logger.info("🔑 Token configurado")

    def connect(self):
        try:
            self.ws = websocket.WebSocketApp(
                self.config.WS_URL,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close
            )
            threading.Thread(
                target=lambda: self.ws.run_forever(ping_interval=20, ping_timeout=10),
                daemon=True
            ).start()
            logger.info("🔌 Conectando à Deriv...")
            return True
        except Exception as e:
            logger.error(f"Erro conexão: {e}")
            return False

    def on_open(self, ws):
        logger.info("✅ WebSocket conectado")
        self.connected = True
        self.reconnect_attempts = 0
        self._balance_subscribed = False
        self.authorize()

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            mt = data.get('msg_type')
            if mt not in ['tick', 'balance', 'time']:
                logger.info(f"📨 [{mt}]")
            handlers = {
                'authorize': self.on_authorize,
                'tick': self.on_tick,
                'proposal': self.on_proposal,
                'buy': self.on_buy_response,
                'proposal_open_contract': self.on_poc,
                'balance': self.on_balance,
                'error': self.on_error_msg,
            }
            if mt in handlers:
                handlers[mt](data)
        except Exception as e:
            logger.error(f"Erro processar msg: {e}")

    def on_error(self, ws, error):
        logger.error(f"WS error: {error}")
        self.connected = False
        self.authorized = False
        if self.should_reconnect:
            self.schedule_reconnect()

    def on_close(self, ws, *args):
        logger.warning("🔌 WebSocket desconectado — a reconectar...")
        self.connected = False
        self.authorized = False
        self._balance_subscribed = False
        if self.should_reconnect:
            self.schedule_reconnect()

    def schedule_reconnect(self):
        self.reconnect_attempts += 1
        delay = min(5 * self.reconnect_attempts, 30)
        logger.info(f"🔄 Reconectar em {delay}s (tentativa {self.reconnect_attempts})")
        threading.Timer(delay, self.reconnect).start()

    def reconnect(self):
        if not self.should_reconnect:
            return
        self.subscribed_symbols.clear()
        self.processed_contracts.clear()
        self.pending_trade = None
        self._balance_subscribed = False
        self.connect()

    def authorize(self):
        if not self.user_token:
            logger.error("❌ Token não configurado!")
            return
        try:
            self.ws.send(json.dumps({"authorize": self.user_token, "req_id": 1}))
            logger.info("🔐 A autorizar...")
        except Exception as e:
            logger.error(f"Erro autorizar: {e}")

    def on_authorize(self, data):
        if data.get('error'):
            logger.error(f"❌ Auth erro: {data['error']}")
            self.authorized = False
        else:
            logger.info("✅ Autorizado com sucesso!")
            self.authorized = True
            self._subscribe_balance()
            if self.current_symbol:
                self.subscribe_ticks(self.current_symbol)

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

    def on_balance(self, data):
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

    def subscribe_ticks(self, symbol):
        if symbol in self.subscribed_symbols:
            logger.info(f"⚠️ Já subscrito em {symbol}")
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
            self.subscribe_ticks(symbol)
            self.current_symbol = symbol

    def on_tick(self, data):
        try:
            tick = data.get('tick', {})
            if tick and self.on_tick_callback:
                self.on_tick_callback({
                    'symbol': tick.get('symbol', self.current_symbol),
                    'price': tick.get('quote', 0),
                    'timestamp': tick.get('epoch', time.time())
                })
        except Exception as e:
            logger.error(f"Erro tick: {e}")

    # ── Colocar Trade ──────────────────────────────────────────
    def place_trade(self, contract_type, amount, is_digit=False):
        with self._trade_lock:
            try:
                if not self.authorized:
                    logger.error("❌ Não autorizado")
                    return False
                if self.pending_trade is not None:
                    logger.warning("⚠️ Trade pendente — aguarde resultado.")
                    return False

                original_action = contract_type  # 'CALL' ou 'PUT'

                if is_digit:
                    duration = self.config.DIGIT_CONTRACT_DURATION  # 10
                    duration_unit = 't'
                    contract_type_full = 'DIGITODD' if contract_type == 'CALL' else 'DIGITEVEN'
                    if self._digit_analyzer:
                        tr = self._digit_analyzer.get_ticks_remaining()
                        if tr < 3:
                            logger.warning("⏳ Menos de 3 ticks para o próximo dígito – considere aguardar.")
                    logger.info(f"🎲 {contract_type_full} ${amount} | {duration} ticks")
                else:
                    duration = self.config.CONTRACT_DURATION
                    duration_unit = self.config.CONTRACT_DURATION_UNIT
                    contract_type_full = 'CALL' if contract_type == 'CALL' else 'PUT'

                self.pending_trade = {
                    'amount': amount,
                    'contract_type': original_action,   # sempre CALL/PUT
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
                logger.info(f"📝 Proposta: {contract_type_full} ${amount} {duration}{duration_unit}")
                return True

            except Exception as e:
                logger.error(f"❌ Erro trade: {e}")
                self.pending_trade = None
                return False

    # ── Proposta → Compra → Resultado ──────────────────────────
    def on_proposal(self, data):
        try:
            if data.get('error'):
                logger.error(f"❌ Proposta recusada: {data['error'].get('message', '')}")
                self.pending_trade = None
                return
            p = data.get('proposal', {})
            pid = p.get('id')
            ask = p.get('ask_price')
            if not pid or ask is None:
                logger.error(f"❌ Proposta inválida: {p}")
                self.pending_trade = None
                return
            logger.info(f"📊 Proposta OK: {pid} ${ask}")
            self.ws.send(json.dumps({"buy": pid, "price": ask, "req_id": 101}))
            if self.pending_trade:
                self.pending_trade['status'] = 'waiting_buy'
                self.pending_trade['proposal_id'] = pid
        except Exception as e:
            logger.error(f"Erro proposta: {e}")
            self.pending_trade = None

    def on_buy_response(self, data):
        try:
            if data.get('error'):
                logger.error(f"❌ Erro compra: {data['error'].get('message','')}")
                self.pending_trade = None
                return
            bd = data.get('buy', {})
            cid = bd.get('contract_id')
            bp = bd.get('buy_price', 0)
            if not cid:
                logger.error("❌ Sem contract_id")
                self.pending_trade = None
                return
            logger.info(f"✅ Contrato {cid} | ${bp}")
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
            logger.info(f"📡 A seguir contrato: {cid}")
        except Exception as e:
            logger.error(f"Erro sub contrato: {e}")

    def on_poc(self, data):
        try:
            c = data.get('proposal_open_contract', {})
            cid = c.get('contract_id')
            if not cid or not c.get('is_sold'):
                return
            if cid in self.processed_contracts:
                return
            self.processed_contracts.add(cid)

            bp = c.get('buy_price', 0)
            sp = c.get('sell_price', 0)
            profit = sp - bp
            amt = self.active_trades.get(cid, {}).get('amount', bp)

            logger.info(
                f"📊 RESULTADO [{cid}]: "
                f"{'✅ GANHO' if profit > 0 else '❌ PERDA'} ${abs(profit):.2f}"
            )
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

    def on_error_msg(self, data):
        error = data.get('error', {})
        logger.error(f"API Error: {error.get('message', 'desconhecido')}")

    def request_deposit(self, amount, currency, method):
        return {'status': 'pending', 'message': f'Depósito ${amount} solicitado.',
                'amount': amount, 'method': method}

    def request_withdrawal(self, amount, currency, method):
        if amount > self.balance:
            return {'error': 'Saldo insuficiente'}
        return {'status': 'pending', 'message': f'Saque ${amount} solicitado.',
                'amount': amount, 'method': method}
