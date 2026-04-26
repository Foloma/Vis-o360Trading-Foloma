import websocket, json, threading, time, logging

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
        self._last_balance_request = 0
        self._balance_interval = 5
        self._trade_lock = threading.Lock()
        self._digit_analyzer = None

    def set_digit_analyzer(self, a): self._digit_analyzer = a
    def set_trading_bot(self, b):    self.trading_bot = b
    def set_payment_system(self, p): self.payment_system = p
    def set_user_token(self, t):     self.user_token = t

    def connect(self):
        try:
            self.ws = websocket.WebSocketApp(self.config.WS_URL,
                on_open=self.on_open, on_message=self.on_message,
                on_error=self.on_error, on_close=self.on_close)
            threading.Thread(target=self.ws.run_forever, daemon=True).start()
            logger.info("🔌 Conectando..."); return True
        except Exception as e:
            logger.error(f"Erro: {e}"); return False

    def on_open(self, ws):
        self.connected = True; self.reconnect_attempts = 0; self.authorize()

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            mt = data.get('msg_type')
            if mt not in ['tick','balance','time']: logger.info(f"📨 [{mt}]")
            h = {'authorize':self.on_authorize,'tick':self.on_tick,
                 'proposal':self.on_proposal,'buy':self.on_buy_response,
                 'proposal_open_contract':self.on_poc,'balance':self.on_balance,
                 'error':self.on_error_msg}
            if mt in h: h[mt](data)
        except Exception as e: logger.error(f"Erro msg: {e}")

    def on_error(self, ws, error):
        logger.error(f"WS error: {error}")
        self.connected = False; self.authorized = False
        if self.should_reconnect: self.schedule_reconnect()

    def on_close(self, ws, *a):
        self.connected = False; self.authorized = False
        if self.should_reconnect: self.schedule_reconnect()

    def schedule_reconnect(self):
        self.reconnect_attempts += 1
        delay = min(5 * self.reconnect_attempts, 30)
        threading.Timer(delay, self.reconnect).start()

    def reconnect(self):
        if not self.should_reconnect: return
        self.subscribed_symbols.clear(); self.processed_contracts.clear()
        self.pending_trade = None; self.connect()

    def authorize(self):
        if not self.user_token: return
        self.ws.send(json.dumps({"authorize": self.user_token, "req_id": 1}))

    def on_authorize(self, data):
        if data.get('error'):
            logger.error(f"❌ Auth: {data['error']}"); self.authorized = False
        else:
            logger.info("✅ Autorizado!"); self.authorized = True
            self.get_balance()
            # ✅ FIX: re-subscrever ticks após reconexão automática
            # subscribed_symbols foi limpo no reconnect(), por isso subscribe_ticks
            # não vai ignorar o pedido
            if self.current_symbol:
                self.subscribe_ticks(self.current_symbol)
                logger.info(f"🔄 Ticks re-subscritos após reconexão: {self.current_symbol}")

    def on_balance(self, data):
        try:
            bd = data.get('balance', {})
            if bd:
                self.balance = bd.get('balance', 0); self.currency = bd.get('currency', 'USD')
                if self.trading_bot:
                    self.trading_bot.balance = self.balance
                    self.trading_bot.currency = self.currency
        except Exception as e: logger.error(f"Erro saldo: {e}")

    def get_balance(self, force=False):
        now = time.time()
        if not force and now - self._last_balance_request < self._balance_interval: return
        self._last_balance_request = now
        try: self.ws.send(json.dumps({"balance": 1, "req_id": 2}))
        except: pass

    def subscribe_ticks(self, symbol):
        if symbol in self.subscribed_symbols: return
        try:
            self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": 3}))
            self.subscribed_symbols.add(symbol); self.current_symbol = symbol
        except Exception as e: logger.error(f"Erro subscrever: {e}")

    def unsubscribe_ticks(self, symbol):
        if symbol in self.subscribed_symbols:
            try:
                self.ws.send(json.dumps({"forget_all": "ticks", "req_id": 4}))
                self.subscribed_symbols.discard(symbol)
            except: pass

    def change_symbol(self, symbol):
        if symbol != self.current_symbol:
            self.unsubscribe_ticks(self.current_symbol)
            time.sleep(0.5); self.subscribe_ticks(symbol); self.current_symbol = symbol

    def on_tick(self, data):
        try:
            tick = data.get('tick', {})
            if tick and self.on_tick_callback:
                self.on_tick_callback({
                    'symbol': tick.get('symbol', self.current_symbol),
                    'price': tick.get('quote', 0),
                    'timestamp': tick.get('epoch', time.time())
                })
        except Exception as e: logger.error(f"Erro tick: {e}")

    def place_trade(self, contract_type, amount, is_digit=False):
        with self._trade_lock:
            try:
                if not self.authorized: return False
                if self.pending_trade is not None:
                    logger.warning("⚠️ Trade pendente — aguarde."); return False

                if is_digit:
                    # ✅ SOLUÇÃO DEFINITIVA DO TIMING:
                    # Obter quantos ticks faltam para o próximo dígito lento.
                    # Esses ticks são contados pelo mesmo WebSocket que a Deriv usa
                    # para contar a duração do contrato → sincronização perfeita.
                    if self._digit_analyzer is not None:
                        ticks_rem = self._digit_analyzer.get_ticks_remaining()
                    else:
                        ticks_rem = 10

                    # Se faltam menos de 3 ticks, usar o PRÓXIMO ciclo completo
                    # para dar tempo ao utilizador de ver o resultado
                    if ticks_rem < 3:
                        ticks_rem += self._digit_analyzer.TICKS_PER_DIGIT if self._digit_analyzer else 10

                    # Respeitar limites da Deriv: min 5, max 10
                    duration = max(5, min(10, ticks_rem))
                    duration_unit = 't'
                    contract_type_full = 'DIGITODD' if contract_type == 'CALL' else 'DIGITEVEN'
                    logger.info(f"🎲 {contract_type_full} ${amount} | {duration} ticks (restavam {ticks_rem})")
                else:
                    duration = 5; duration_unit = 't'
                    contract_type_full = 'CALL' if contract_type == 'CALL' else 'PUT'

                self.pending_trade = {
                    'amount': amount, 'contract_type': contract_type_full,
                    'is_digit': is_digit, 'timestamp': time.time(), 'status': 'waiting_proposal'
                }

                self.ws.send(json.dumps({
                    "proposal": 1, "amount": amount, "basis": "stake",
                    "contract_type": contract_type_full, "currency": self.currency,
                    "duration": duration, "duration_unit": duration_unit,
                    "symbol": self.current_symbol, "req_id": 100
                }))
                return True
            except Exception as e:
                logger.error(f"❌ {e}"); self.pending_trade = None; return False

    def on_proposal(self, data):
        try:
            if data.get('error'):
                logger.error(f"❌ Proposta: {data['error'].get('message','')}");
                self.pending_trade = None; return
            p = data.get('proposal', {}); pid = p.get('id'); ask = p.get('ask_price')
            if not pid or ask is None: self.pending_trade = None; return
            self.ws.send(json.dumps({"buy": pid, "price": ask, "req_id": 101}))
            if self.pending_trade:
                self.pending_trade['status'] = 'waiting_buy'
                self.pending_trade['proposal_id'] = pid
        except Exception as e:
            logger.error(f"Erro proposta: {e}"); self.pending_trade = None

    def on_buy_response(self, data):
        try:
            if data.get('error'):
                logger.error(f"❌ Compra: {data['error'].get('message','')}");
                self.pending_trade = None; return
            bd = data.get('buy', {}); cid = bd.get('contract_id'); bp = bd.get('buy_price', 0)
            if not cid: self.pending_trade = None; return
            logger.info(f"✅ Contrato {cid} comprado por ${bp}")
            if self.pending_trade:
                amt = self.pending_trade.get('amount', 0); action = self.pending_trade.get('contract_type', '')
                if self.trading_bot:
                    self.trading_bot.register_trade({
                        'contract_id': cid, 'symbol': self.current_symbol,
                        'action': action, 'amount': amt, 'price': bp,
                        'result': 'pending', 'confidence': 70
                    })
                self.active_trades[cid] = {'contract_id':cid,'amount':amt,'buy_price':bp,'timestamp':time.time(),'action':action}
                self._subscribe_contract(cid)
                self.pending_trade = None
                self._last_balance_request = 0; self.get_balance(force=True)
        except Exception as e:
            logger.error(f"Erro compra: {e}"); self.pending_trade = None

    def _subscribe_contract(self, cid):
        try:
            self.ws.send(json.dumps({"proposal_open_contract":1,"contract_id":cid,"subscribe":1,"req_id":200}))
        except Exception as e: logger.error(f"Erro sub contrato: {e}")

    def on_poc(self, data):
        try:
            c = data.get('proposal_open_contract', {}); cid = c.get('contract_id')
            if not cid or not c.get('is_sold'): return
            if cid in self.processed_contracts: return
            self.processed_contracts.add(cid)
            bp = c.get('buy_price',0); sp = c.get('sell_price',0); profit = sp - bp
            amt = self.active_trades.get(cid,{}).get('amount', bp)
            logger.info(f"📊 [{cid}] {'✅ GANHO' if profit>0 else '❌ PERDA'} ${abs(profit):.2f}")
            if self.trading_bot:
                self.trading_bot.on_trade_result({'contract_id':cid,'buy_price':bp,'sell_price':sp,'profit':profit,'amount':amt,'is_win':profit>0})
            if cid in self.active_trades: del self.active_trades[cid]
            self._last_balance_request = 0; self.get_balance(force=True)
        except Exception as e: logger.error(f"Erro poc: {e}")

    def on_error_msg(self, data):
        logger.error(f"API: {data.get('error',{}).get('message','?')}")

    def request_deposit(self, amount, currency, method):
        return {'status':'pending','message':f'Depósito ${amount} solicitado.','amount':amount,'method':method}

    def request_withdrawal(self, amount, currency, method):
        if amount > self.balance: return {'error':'Saldo insuficiente'}
        return {'status':'pending','message':f'Saque ${amount} solicitado.','amount':amount,'method':method}
