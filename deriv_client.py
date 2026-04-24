
# Criar deriv_client.py COMPLETO e funcional

deriv_completo = '''import websocket
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
        self._last_balance_request = 0
        self._balance_interval = 5
        self._trade_lock = threading.Lock()
        self._digit_analyzer = None
        self._pending_trade_timer = None

    def set_digit_analyzer(self, analyzer):
        self._digit_analyzer = analyzer

    def set_trading_bot(self, bot):
        self.trading_bot = bot

    def set_payment_system(self, ps):
        self.payment_system = ps

    def set_user_token(self, token):
        self.user_token = token
        logger.info("🔑 Token configurado")

    # ── Conexão ────────────────────────────────────────────────
    def connect(self):
        try:
            self.ws = websocket.WebSocketApp(
                self.config.WS_URL,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close
            )
            threading.Thread(target=self.ws.run_forever, daemon=True).start()
            logger.info("🔌 Conectando à Deriv...")
            return True
        except Exception as e:
            logger.error(f"Erro: {e}")
            return False

    def on_open(self, ws):
        logger.info("✅ WebSocket conectado")
        self.connected = True
        self.reconnect_attempts = 0
        self.authorize()

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get('msg_type')
            if msg_type not in ['tick', 'balance', 'time']:
                logger.info(f"📨 [{msg_type}]")
            handlers = {
                'authorize': self.on_authorize,
                'tick': self.on_tick,
                'proposal': self.on_proposal,
                'buy': self.on_buy_response,
                'proposal_open_contract': self.on_proposal_open_contract,
                'balance': self.on_balance,
                'error': self.on_error_response
            }
            if msg_type in handlers:
                handlers[msg_type](data)
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {e}")

    def on_error(self, ws, error):
        logger.error(f"WS error: {error}")
        self.connected = False
        self.authorized = False
        if self.should_reconnect:
            self.schedule_reconnect()

    def on_close(self, ws, *args):
        logger.info("🔌 Desconectado")
        self.connected = False
        self.authorized = False
        if self.should_reconnect:
            self.schedule_reconnect()

    def schedule_reconnect(self):
        self.reconnect_attempts += 1
        delay = min(5 * self.reconnect_attempts, 30)
        logger.info(f"🔄 Reconectar em {delay}s")
        threading.Timer(delay, self.reconnect).start()

    def reconnect(self):
        if not self.should_reconnect:
            return
        self.subscribed_symbols.clear()
        self.processed_contracts.clear()
        self.pending_trade = None
        self.connect()

    # ── Autorização ────────────────────────────────────────────
    def authorize(self):
        if not self.user_token:
            logger.error("❌ Token não configurado!")
            return
        self.ws.send(json.dumps({"authorize": self.user_token, "req_id": 1}))

    def on_authorize(self, data):
        if data.get('error'):
            logger.error(f"❌ Erro na autorização: {data['error']}")
            self.authorized = False
        else:
            logger.info("✅ Autorizado!")
            self.authorized = True
            self.get_balance()

    # ── Saldo ──────────────────────────────────────────────────
    def on_balance(self, data):
        try:
            bd = data.get('balance', {})
            if bd:
                self.balance  = bd.get('balance', 0)
                self.currency = bd.get('currency', 'USD')
                if self.trading_bot:
                    self.trading_bot.balance  = self.balance
                    self.trading_bot.currency = self.currency
                logger.info(f"💰 Saldo: {self.balance:.2f} {self.currency}")
        except Exception as e:
            logger.error(f"Erro saldo: {e}")

    def get_balance(self, force=False):
        now = time.time()
        if not force and now - self._last_balance_request < self._balance_interval:
            return
        self._last_balance_request = now
        try:
            self.ws.send(json.dumps({"balance": 1, "req_id": 2}))
        except Exception as e:
            logger.error(f"Erro pedir saldo: {e}")

    # ── Ticks ──────────────────────────────────────────────────
    def subscribe_ticks(self, symbol):
        if symbol in self.subscribed_symbols:
            return
        try:
            self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": 3}))
            self.subscribed_symbols.add(symbol)
            self.current_symbol = symbol
            logger.info(f"📊 Subscrito: {symbol}")
        except Exception as e:
            logger.error(f"Erro subscrever: {e}")

    def unsubscribe_ticks(self, symbol):
        if symbol in self.subscribed_symbols:
            try:
                self.ws.send(json.dumps({"forget_all": "ticks", "req_id": 4}))
                self.subscribed_symbols.discard(symbol)
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

    # Timeout para pending_trade (5s para dígitos, 10s para normais)
    def _clear_pending_trade_timeout(self, is_digit=False):
        timeout = 5 if is_digit else 10
        if self.pending_trade:
            elapsed = time.time() - self.pending_trade.get('timestamp', 0)
            if elapsed > timeout:
                logger.warning(f"⏱️ Timeout: pending_trade limpo após {elapsed:.1f}s")
                self.pending_trade = None

    # ── Colocar Trade ──────────────────────────────────────────
    def place_trade(self, contract_type, amount, is_digit=False):
        """
        Para dígitos (is_digit=True):
          ✅ duration = 5 ticks SEMPRE (fixo e dentro do limite Deriv: max 10t)
          ✅ Cada tick ≈ 1s nos índices de volatilidade
          ✅ Timeout de 5s para não perder o ciclo de 15s

        Para ativos normais:
          duration = 5 ticks
        """
        with self._trade_lock:
            try:
                if not self.authorized:
                    logger.error("❌ Não autorizado")
                    return False

                # Verificar se pending_trade está preso (timeout dinâmico)
                if self.pending_trade is not None:
                    elapsed = time.time() - self.pending_trade.get('timestamp', 0)
                    timeout = 5 if self.pending_trade.get('is_digit') else 10
                    if elapsed > timeout:
                        logger.warning(f"🧹 pending_trade preso há {elapsed:.1f}s — limpando")
                        self.pending_trade = None
                    else:
                        logger.warning(f"⚠️ Trade pendente ({elapsed:.1f}s/{timeout}s). Aguarde.")
                        return False

                if is_digit:
                    duration      = 5
                    duration_unit = 't'
                    contract_type_full = 'DIGITODD' if contract_type == 'CALL' else 'DIGITEVEN'
                    logger.info(f"🎲 DÍGITO: {contract_type_full} ${amount} | 5 ticks")
                else:
                    duration      = 5
                    duration_unit = 't'
                    contract_type_full = 'CALL' if contract_type == 'CALL' else 'PUT'

                self.pending_trade = {
                    'amount': amount,
                    'contract_type': contract_type_full,
                    'is_digit': is_digit,
                    'timestamp': time.time(),
                    'status': 'waiting_proposal'
                }

                # Agendar timeout automático (5s para dígitos, 10s para normais)
                if self._pending_trade_timer:
                    self._pending_trade_timer.cancel()
                timeout_seconds = 5 if is_digit else 10
                self._pending_trade_timer = threading.Timer(timeout_seconds, self._clear_pending_trade_timeout, args=[is_digit])
                self._pending_trade_timer.start()

                proposal_msg = {
                    "proposal": 1,
                    "amount": amount,
                    "basis": "stake",
                    "contract_type": contract_type_full,
                    "currency": self.currency,
                    "duration": duration,
                    "duration_unit": duration_unit,
                    "symbol": self.current_symbol,
                    "req_id": 100
                }

                logger.info(f"📝 Proposta: {contract_type_full} ${amount} {duration}{duration_unit}")
                self.ws.send(json.dumps(proposal_msg))
                return True

            except Exception as e:
                logger.error(f"❌ Erro trade: {e}")
                self.pending_trade = None
                return False

    # ── Proposta ───────────────────────────────────────────────
    def on_proposal(self, data):
        try:
            if data.get('error'):
                logger.error(f"❌ Proposta recusada: {data['error'].get('message','')}")
                self.pending_trade = None
                return

            proposal   = data.get('proposal', {})
            proposal_id = proposal.get('id')
            ask_price  = proposal.get('ask_price')

            if not proposal_id or ask_price is None:
                logger.error(f"❌ Proposta inválida: {proposal}")
                self.pending_trade = None
                return

            # Verificar se pending_trade ainda é válido
            if not self.pending_trade:
                logger.warning("⚠️ pending_trade já foi limpo — ignorando proposta antiga")
                return
            
            is_digit = self.pending_trade.get('is_digit', False)
            elapsed = time.time() - self.pending_trade.get('timestamp', 0)
            max_wait = 4 if is_digit else 8
            
            if elapsed > max_wait:
                logger.warning(f"⏱️ Proposta descartada (demorou {elapsed:.1f}s > {max_wait}s)")
                self.pending_trade = None
                return

            logger.info(f"📊 Proposta OK: {proposal_id} | ${ask_price}")
            self.ws.send(json.dumps({"buy": proposal_id, "price": ask_price, "req_id": 101}))

            if self.pending_trade:
                self.pending_trade['status'] = 'waiting_buy'
                self.pending_trade['proposal_id'] = proposal_id

        except Exception as e:
            logger.error(f"Erro proposta: {e}")
            self.pending_trade = None

    # ── Compra confirmada ──────────────────────────────────────
    def on_buy_response(self, data):
        try:
            # Cancelar timer de timeout
            if self._pending_trade_timer:
                self._pending_trade_timer.cancel()
                self._pending_trade_timer = None

            if data.get('error'):
                logger.error(f"❌ Erro compra: {data['error'].get('message','')}")
                self.pending_trade = None
                return

            buy_data    = data.get('buy', {})
            contract_id = buy_data.get('contract_id')
            buy_price   = buy_data.get('buy_price', 0)

            if not contract_id:
                logger.error("❌ Sem contract_id")
                self.pending_trade = None
                return

            logger.info(f"✅ Executado! ID: {contract_id} | ${buy_price}")

            if self.pending_trade:
                amount = self.pending_trade.get('amount', 0)
                action = self.pending_trade.get('contract_type', '')

                if self.trading_bot:
                    self.trading_bot.register_trade({
                        'contract_id': contract_id,
                        'symbol': self.current_symbol,
                        'action': action,
                        'amount': amount,
                        'price': buy_price,
                        'result': 'pending',
                        'confidence': 70
                    })

                self.active_trades[contract_id] = {
                    'contract_id': contract_id,
                    'amount': amount,
                    'buy_price': buy_price,
                    'timestamp': time.time(),
                    'action': action
                }

                self._subscribe_contract(contract_id)
                self.pending_trade = None

                # Atualizar saldo imediatamente (mostrar débito)
                self._last_balance_request = 0
                self.get_balance(force=True)

        except Exception as e:
            logger.error(f"Erro compra: {e}")
            self.pending_trade = None

    def _subscribe_contract(self, contract_id):
        try:
            self.ws.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": contract_id,
                "subscribe": 1,
                "req_id": 200
            }))
            logger.info(f"📡 A seguir contrato: {contract_id}")
        except Exception as e:
            logger.error(f"Erro subscrever contrato: {e}")

    # ── Resultado ──────────────────────────────────────────────
    def on_proposal_open_contract(self, data):
        try:
            contract    = data.get('proposal_open_contract', {})
            contract_id = contract.get('contract_id')
            if not contract_id or not contract.get('is_sold'):
                return

            if contract_id in self.processed_contracts:
                return
            self.processed_contracts.add(contract_id)

            buy_price  = contract.get('buy_price', 0)
            sell_price = contract.get('sell_price', 0)
            profit     = sell_price - buy_price
            active     = self.active_trades.get(contract_id, {})
            amount     = active.get('amount', buy_price)

            logger.info(
                f"📊 RESULTADO [{contract_id}]: "
                f"{'✅ GANHO' if profit > 0 else '❌ PERDA'} ${abs(profit):.2f}"
            )

            if self.trading_bot:
                self.trading_bot.on_trade_result({
                    'contract_id': contract_id,
                    'buy_price': buy_price,
                    'sell_price': sell_price,
                    'profit': profit,
                    'amount': amount,
                    'is_win': profit > 0
                })

            if contract_id in self.active_trades:
                del self.active_trades[contract_id]

            # Atualizar saldo após resultado
            self._last_balance_request = 0
            self.get_balance(force=True)

        except Exception as e:
            logger.error(f"Erro resultado: {e}")

    def on_error_response(self, data):
        error = data.get('error', {})
        logger.error(f"API Error: {error.get('message','Erro desconhecido')}")
        # Limpar pending_trade em caso de erro
        if self.pending_trade:
            logger.warning("🧹 Limpando pending_trade após erro da API")
            self.pending_trade = None
            if self._pending_trade_timer:
                self._pending_trade_timer.cancel()
                self._pending_trade_timer = None

    def request_deposit(self, amount, currency, method):
        return {'status': 'pending', 'message': f'Depósito de ${amount} solicitado.', 'amount': amount, 'method': method}

    def request_withdrawal(self, amount, currency, method):
        if amount > self.balance:
            return {'error': 'Saldo insuficiente'}
        return {'status': 'pending', 'message': f'Saque de ${amount} solicitado.', 'amount': amount, 'method': method}
'''

with open('/mnt/agents/output/deriv_client.py', 'w') as f:
    f.write(deriv_completo)

print("✅ deriv_client.py COMPLETO criado!")
print("   - Timeout: 5s para dígitos | 10s para normais")
print("   - Max wait na proposta: 4s para dígitos | 8s para normais")
