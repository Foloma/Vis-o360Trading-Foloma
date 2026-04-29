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

        # Extras necessários
        self._digit_analyzer = None
        self._trade_lock = threading.Lock()
        self.processed_contracts = set()

    def set_digit_analyzer(self, a): self._digit_analyzer = a
    def set_trading_bot(self, bot):  self.trading_bot = bot
    def set_payment_system(self, ps): self.payment_system = ps

    def set_user_token(self, token):
        self.user_token = token
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
            # ✅ ORIGINAL: run_forever sem ping_interval — estável
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
                logger.info(f"📨 [RECEBIDO] {msg_type}")
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
        logger.info("🔄 Reconectando...")
        self.subscribed_symbols.clear()
        self.processed_contracts.clear()
        self.pending_trade = None
        self.connect()

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
            logger.info("✅ Autorizado com sucesso!")
            self.authorized = True
            self.get_balance()
            # Re-subscrever ticks (necessário após reconexão)
            if self.current_symbol:
                self.subscribed_symbols.discard(self.current_symbol)
                self.subscribe_ticks(self.current_symbol)

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

    def get_balance(self):
        try:
            self.ws.send(json.dumps({"balance": 1, "req_id": 2}))
        except Exception as e:
            logger.error(f"Erro saldo: {e}")

    def subscribe_ticks(self, symbol):
        if symbol in self.subscribed_symbols:
            return
        try:
            self.ws.send(json.dumps({"ticks": symbol, "subscribe": 1, "req_id": 3}))
            self.subscribed_symbols.add(symbol)
            self.current_symbol = symbol
            logger.info(f"📊 Inscrito em ticks: {symbol}")
        except Exception as e:
            logger.error(f"Erro subscrever ticks: {e}")

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
                self.get_balance()
        except Exception as e:
            logger.error(f"Erro no tick: {e}")

    def place_trade(self, contract_type, amount, is_digit=False):
        with self._trade_lock:
            try:
                if not self.authorized:
                    logger.error("❌ Não autorizado")
                    return False

                if self.pending_trade is not None:
                    logger.warning("⚠️ Trade pendente — aguarde resultado.")
                    return False

                if is_digit:
                    if self._digit_analyzer is not None:
                        ticks_rem = self._digit_analyzer.get_ticks_remaining()
                        tpd = self._digit_analyzer.TICKS_PER_DIGIT
                    else:
                        ticks_rem = 10
                        tpd = 10

                    # Compensar latência de rede
                    if ticks_rem <= 3:
                        duration = tpd - 2
                    else:
                        duration = ticks_rem - 2

                    duration = max(5, min(10, duration))
                    duration_unit = 't'
                    contract_type_full = 'DIGITODD' if contract_type == 'CALL' else 'DIGITEVEN'
                    logger.info(f"🎲 {contract_type_full} ${amount} | {duration}t (restavam {ticks_rem})")
                else:
                    duration = 5
                    duration_unit = 't'
                    contract_type_full = 'CALL' if contract_type == 'CALL' else 'PUT'

                self.pending_trade = {
                    'amount': amount,
                    'contract_type': contract_type_full,
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
                logger.info(f"📝 Proposta: {contract_type_full} ${amount} ({duration}{duration_unit})")
                return True

            except Exception as e:
                logger.error(f"❌ Erro ao colocar trade: {e}")
                self.pending_trade = None
                return False

    def on_proposal(self, data):
        try:
            if data.get('error'):
                logger.error(f"❌ Proposta recusada: {data['error']}")
                self.pending_trade = None
                return
            proposal = data.get('proposal', {})
            proposal_id = proposal.get('id')
            ask_price = proposal.get('ask_price')
            if not proposal_id or not ask_price:
                logger.error(f"❌ Proposta inválida: {proposal}")
                self.pending_trade = None
                return
            logger.info(f"📊 Proposta: ID={proposal_id}, Preço={ask_price}")
            self.ws.send(json.dumps({"buy": proposal_id, "price": ask_price, "req_id": 101}))
            if self.pending_trade:
                self.pending_trade['status'] = 'waiting_buy'
                self.pending_trade['proposal_id'] = proposal_id
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
            if self.pending_trade:
                amount = self.pending_trade.get('amount', 0)
                action = self.pending_trade.get('contract_type', '')
                logger.info(f"✅ Trade executado! ID: {contract_id}, Preço: ${buy_price}")
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
                if contract_id:
                    self.active_trades[contract_id] = {
                        'contract_id': contract_id,
                        'amount': amount,
                        'buy_price': buy_price,
                        'timestamp': time.time(),
                        'action': action
                    }
                    self.subscribe_contract(contract_id)
                self.pending_trade = None
        except Exception as e:
            logger.error(f"Erro ao processar compra: {e}")
            self.pending_trade = None

    def subscribe_contract(self, contract_id):
        try:
            self.ws.send(json.dumps({
                "proposal_open_contract": 1,
                "contract_id": contract_id,
                "subscribe": 1,
                "req_id": 200
            }))
            logger.info(f"📡 Acompanhando contrato: {contract_id}")
        except Exception as e:
            logger.error(f"Erro subscrever contrato: {e}")

    def on_proposal_open_contract(self, data):
        try:
            contract = data.get('proposal_open_contract', {})
            contract_id = contract.get('contract_id')
            if not contract_id:
                return
            if not contract.get('is_sold'):
                return
            if contract_id in self.processed_contracts:
                return
            self.processed_contracts.add(contract_id)

            buy_price = contract.get('buy_price', 0)
            sell_price = contract.get('sell_price', 0)
            profit = sell_price - buy_price
            amount = self.active_trades.get(contract_id, {}).get('amount', buy_price)

            result_data = {
                'contract_id': contract_id,
                'buy_price': buy_price,
                'sell_price': sell_price,
                'profit': profit,
                'amount': amount,
                'is_win': profit > 0
            }
            logger.info(f"📊 RESULTADO: {'✅ GANHO' if profit > 0 else '❌ PERDA'} ${abs(profit):.2f}")
            if self.trading_bot:
                self.trading_bot.on_trade_result(result_data)
            if contract_id in self.active_trades:
                del self.active_trades[contract_id]
        except Exception as e:
            logger.error(f"Erro ao processar contrato: {e}")

    def on_error_response(self, data):
        error = data.get('error', {})
        logger.error(f"API Error: {error.get('message', 'Unknown error')}")

    def request_deposit(self, amount, currency, method):
        logger.info(f"💰 Depósito: ${amount} via {method}")
        return {'status': 'pending', 'message': f'Depósito de ${amount} solicitado.',
                'amount': amount, 'method': method}

    def request_withdrawal(self, amount, currency, method):
        if amount > self.balance:
            return {'error': 'Saldo insuficiente'}
        logger.info(f"💸 Saque: ${amount} via {method}")
        return {'status': 'pending', 'message': f'Saque de ${amount} solicitado.',
                'amount': amount, 'method': method}
