import websocket
import json
import threading
import time
import logging
from datetime import datetime

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
        self.trade_history = []
        self.pending_trade = None
        self.reconnect_attempts = 0
        self.should_reconnect = True

        # Contratos já processados — evita duplicação
        self.processed_contracts = set()

        # Controlo de saldo
        self._last_balance_request = 0
        self._balance_interval = 5

        # Lock para evitar trades simultâneos
        self._trade_lock = threading.Lock()

        # Referência ao digit_analyzer
        self._digit_analyzer = None

    def set_digit_analyzer(self, analyzer):
        self._digit_analyzer = analyzer

    def set_trading_bot(self, bot):
        self.trading_bot = bot

    def set_payment_system(self, payment_system):
        self.payment_system = payment_system

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
            logger.error(f"Erro na conexão: {e}")
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
                logger.info(f"📨 [{msg_type}] recebido")
            if msg_type == 'authorize':
                self.on_authorize(data)
            elif msg_type == 'tick':
                self.on_tick(data)
            elif msg_type == 'proposal':
                self.on_proposal(data)
            elif msg_type == 'buy':
                self.on_buy_response(data)
            elif msg_type == 'proposal_open_contract':
                self.on_proposal_open_contract(data)
            elif msg_type == 'balance':
                self.on_balance(data)
            elif msg_type == 'error':
                self.on_error_response(data)
        except Exception as e:
            logger.error(f"Erro ao processar mensagem: {e}")

    def on_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")
        self.connected = False
        self.authorized = False
        if self.should_reconnect:
            self.schedule_reconnect()

    def on_close(self, ws, close_status_code, close_msg):
        logger.info("🔌 WebSocket desconectado")
        self.connected = False
        self.authorized = False
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
        self.connect()

    # ── Autenticação ───────────────────────────────────────────
    def authorize(self):
        if not self.user_token:
            logger.error("❌ Token não configurado!")
            return
        self.ws.send(json.dumps({"authorize": self.user_token, "req_id": 1}))
        logger.info("🔐 Enviando autorização...")

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
            balance_data = data.get('balance', {})
            if balance_data:
                self.balance = balance_data.get('balance', 0)
                self.currency = balance_data.get('currency', 'USD')
                if self.trading_bot:
                    self.trading_bot.balance = self.balance
                    self.trading_bot.currency = self.currency
                logger.info(f"💰 Saldo: {self.balance:.2f} {self.currency}")
        except Exception as e:
            logger.error(f"Erro ao atualizar saldo: {e}")

    def get_balance(self, force=False):
        now = time.time()
        if not force and (now - self._last_balance_request < self._balance_interval):
            return
        self._last_balance_request = now
        try:
            self.ws.send(json.dumps({"balance": 1, "req_id": 2}))
        except Exception as e:
            logger.error(f"Erro ao pedir saldo: {e}")

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
            logger.error(f"Erro ao subscrever ticks: {e}")

    def unsubscribe_ticks(self, symbol):
        if symbol in self.subscribed_symbols:
            try:
                self.ws.send(json.dumps({"forget_all": "ticks", "req_id": 4}))
                self.subscribed_symbols.discard(symbol)
            except Exception as e:
                logger.error(f"Erro ao dessubscrever: {e}")

    def change_symbol(self, symbol):
        if symbol != self.current_symbol:
            self.unsubscribe_ticks(self.current_symbol)
            time.sleep(0.5)
            self.subscribe_ticks(symbol)
            self.current_symbol = symbol

    def on_tick(self, data):
        try:
            tick = data.get('tick', {})
            if tick:
                tick_data = {
                    'symbol': tick.get('symbol', self.current_symbol),
                    'price': tick.get('quote', 0),
                    'timestamp': tick.get('epoch', time.time())
                }
                if self.on_tick_callback:
                    self.on_tick_callback(tick_data)
        except Exception as e:
            logger.error(f"Erro no tick: {e}")

    # ── Colocar Trade ──────────────────────────────────────────
    def place_trade(self, contract_type, amount, is_digit=False):
        with self._trade_lock:
            try:
                if not self.authorized:
                    logger.error("❌ Não autorizado")
                    return False

                if self.pending_trade is not None:
                    logger.warning("⚠️ Trade pendente em curso. Aguarde.")
                    return False

                if is_digit:
                    # ✅ FIX CRÍTICO: Deriv só aceita TICKS ('t') para DIGITODD/DIGITEVEN
                    # 1 tick ≈ 1 segundo nos índices de volatilidade
                    # Calculamos quantos ticks faltam para o próximo dígito de 15s
                    if self._digit_analyzer is not None:
                        ticks_to_next = self._digit_analyzer.get_seconds_to_next_digit()
                    else:
                        ticks_to_next = 15

                    # Garantir mínimo de 5 ticks (requisito da Deriv)
                    duration = max(5, int(ticks_to_next))
                    duration_unit = 't'   # ✅ TICKS, não segundos!

                    contract_type_full = 'DIGITODD' if contract_type == 'CALL' else 'DIGITEVEN'

                    logger.info(
                        f"🎲 DÍGITO: {contract_type_full} | "
                        f"Duração: {duration} ticks | "
                        f"Próximo dígito em ~{ticks_to_next}s"
                    )
                else:
                    duration = 5
                    duration_unit = 't'
                    contract_type_full = 'CALL' if contract_type == 'CALL' else 'PUT'

                self.pending_trade = {
                    'amount': amount,
                    'contract_type': contract_type_full,
                    'is_digit': is_digit,
                    'duration': duration,
                    'duration_unit': duration_unit,
                    'timestamp': time.time(),
                    'status': 'waiting_proposal'
                }

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

                logger.info(
                    f"📝 Proposta: {contract_type_full} ${amount} "
                    f"({duration}{duration_unit}) em {self.current_symbol}"
                )
                self.ws.send(json.dumps(proposal_msg))
                return True

            except Exception as e:
                logger.error(f"❌ Erro ao colocar trade: {e}")
                self.pending_trade = None
                return False

    # ── Proposta → Compra → Resultado ─────────────────────────
    def on_proposal(self, data):
        try:
            if data.get('error'):
                logger.error(f"❌ Proposta recusada: {data['error']}")
                self.pending_trade = None
                return

            proposal = data.get('proposal', {})
            proposal_id = proposal.get('id')
            ask_price = proposal.get('ask_price')

            if not proposal_id or ask_price is None:
                logger.error(f"❌ Proposta inválida: {proposal}")
                self.pending_trade = None
                return

            logger.info(f"📊 Proposta OK: ID={proposal_id} | Preço={ask_price}")
            self.ws.send(json.dumps({"buy": proposal_id, "price": ask_price, "req_id": 101}))

            if self.pending_trade:
                self.pending_trade['status'] = 'waiting_buy'
                self.pending_trade['proposal_id'] = proposal_id
                self.pending_trade['ask_price'] = ask_price

        except Exception as e:
            logger.error(f"Erro ao processar proposta: {e}")
            self.pending_trade = None

    def on_buy_response(self, data):
        try:
            if data.get('error'):
                logger.error(f"❌ Erro no trade: {data['error']}")
                self.pending_trade = None
                return

            buy_data = data.get('buy', {})
            contract_id = buy_data.get('contract_id')
            buy_price = buy_data.get('buy_price', 0)

            if not contract_id:
                logger.error("❌ Sem contract_id na resposta")
                self.pending_trade = None
                return

            logger.info(f"✅ Trade executado! Contrato: {contract_id} | Preço: ${buy_price}")

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

                # ✅ Atualizar saldo imediatamente após trade (mostrar débito)
                self._last_balance_request = 0
                self.get_balance(force=True)

        except Exception as e:
            logger.error(f"Erro ao processar compra: {e}")
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
            logger.error(f"Erro ao subscrever contrato: {e}")

    def on_proposal_open_contract(self, data):
        try:
            contract = data.get('proposal_open_contract', {})
            contract_id = contract.get('contract_id')
            if not contract_id:
                return

            # Só processar contratos fechados
            if not contract.get('is_sold'):
                return

            # Evitar duplicação
            if contract_id in self.processed_contracts:
                logger.warning(f"⚠️ Contrato {contract_id} já processado.")
                return
            self.processed_contracts.add(contract_id)

            buy_price  = contract.get('buy_price', 0)
            sell_price = contract.get('sell_price', 0)
            profit     = sell_price - buy_price

            active = self.active_trades.get(contract_id, {})
            amount = active.get('amount', buy_price)

            result_data = {
                'contract_id': contract_id,
                'buy_price': buy_price,
                'sell_price': sell_price,
                'profit': profit,
                'amount': amount,
                'is_win': profit > 0
            }

            logger.info(
                f"📊 RESULTADO [{contract_id}]: "
                f"{'✅ GANHO' if profit > 0 else '❌ PERDA'} ${abs(profit):.2f}"
            )

            if self.trading_bot:
                self.trading_bot.on_trade_result(result_data)

            if contract_id in self.active_trades:
                del self.active_trades[contract_id]

            # ✅ Atualizar saldo após resultado
            self._last_balance_request = 0
            self.get_balance(force=True)

        except Exception as e:
            logger.error(f"Erro ao processar contrato: {e}")

    def on_error_response(self, data):
        error = data.get('error', {})
        logger.error(f"API Error: {error.get('message', 'Erro desconhecido')}")

    def request_deposit(self, amount, currency, method):
        return {'status': 'pending', 'message': f'Depósito de ${amount} solicitado.', 'amount': amount, 'method': method}

    def request_withdrawal(self, amount, currency, method):
        if amount > self.balance:
            return {'error': 'Saldo insuficiente'}
        return {'status': 'pending', 'message': f'Saque de ${amount} solicitado.', 'amount': amount, 'method': method}
