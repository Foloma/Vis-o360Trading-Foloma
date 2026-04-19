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
        
        # Controle de trades
        self.active_trades = {}
        self.trade_history = []
        self.pending_trade = None
        self.pending_proposal = None
        
    def set_trading_bot(self, bot):
        self.trading_bot = bot
        
    def set_payment_system(self, payment_system):
        self.payment_system = payment_system
        
    def set_user_token(self, token):
        self.user_token = token
        logger.info(f"🔑 Token do utilizador configurado")
        
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
        
    def on_close(self, ws, close_status_code, close_msg):
        logger.info("🔌 WebSocket desconectado")
        self.connected = False
        self.authorized = False
        
    def authorize(self):
        if not self.user_token:
            logger.error("❌ Token do utilizador não configurado!")
            return
        auth_msg = {"authorize": self.user_token, "req_id": 1}
        self.ws.send(json.dumps(auth_msg))
        logger.info("🔐 Enviando autorização...")
    
    def on_authorize(self, data):
        if data.get('error'):
            logger.error(f"❌ Erro na autorização: {data['error']}")
            self.authorized = False
        else:
            logger.info("✅ Autorizado com sucesso!")
            self.authorized = True
            self.get_balance()
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
        balance_msg = {"balance": 1, "req_id": 2}
        self.ws.send(json.dumps(balance_msg))
    
    def subscribe_ticks(self, symbol):
        if symbol in self.subscribed_symbols:
            return
        subscribe_msg = {"ticks": symbol, "subscribe": 1, "req_id": 3}
        self.ws.send(json.dumps(subscribe_msg))
        self.subscribed_symbols.add(symbol)
        self.current_symbol = symbol
        logger.info(f"📊 Inscrito em ticks: {symbol}")
    
    def unsubscribe_ticks(self, symbol):
        if symbol in self.subscribed_symbols:
            unsubscribe_msg = {"forget": symbol, "req_id": 4}
            self.ws.send(json.dumps(unsubscribe_msg))
            self.subscribed_symbols.discard(symbol)
    
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
        try:
            if not self.authorized:
                logger.error("❌ Não autorizado")
                return False

            # Define duração conforme o tipo de trade
            if is_digit:
                # Dígitos: duração de 15 segundos (alinhado com exibição lenta)
                duration = 15
                duration_unit = 's'
                if contract_type == 'CALL':
                    contract_type_full = 'DIGITODD'
                else:
                    contract_type_full = 'DIGITEVEN'
            else:
                # Ativos: duração de 5 ticks
                duration = 5
                duration_unit = 't'
                contract_type_full = 'CALL' if contract_type == 'CALL' else 'PUT'

            # Guarda informação do trade pendente
            self.pending_trade = {
                'amount': amount,
                'contract_type': contract_type_full,
                'is_digit': is_digit,
                'action': 'ÍMPAR' if (is_digit and contract_type == 'CALL') else ('PAR' if is_digit else contract_type),
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

            logger.info(f"📝 Solicitando proposta: {contract_type_full} ${amount} (duração {duration}{duration_unit})")
            self.ws.send(json.dumps(proposal_msg))

            return True

        except Exception as e:
            logger.error(f"❌ Erro ao colocar trade: {e}")
            return False
    
    def on_proposal(self, data):
        try:
            proposal = data.get('proposal', {})
            proposal_id = proposal.get('id')
            ask_price = proposal.get('ask_price')
            if not proposal_id or not ask_price:
                logger.error(f"❌ Proposta inválida: {proposal}")
                return
            logger.info(f"📊 Proposta recebida: ID={proposal_id}, Preço={ask_price}")
            buy_msg = {"buy": proposal_id, "price": ask_price, "req_id": 101}
            logger.info(f"💰 Executando compra...")
            self.ws.send(json.dumps(buy_msg))
            if self.pending_trade:
                self.pending_trade['status'] = 'waiting_buy'
                self.pending_trade['proposal_id'] = proposal_id
                self.pending_trade['ask_price'] = ask_price
        except Exception as e:
            logger.error(f"Erro ao processar proposta: {e}")
    
    def on_buy_response(self, data):
        try:
            if data.get('error'):
                logger.error(f"❌ Erro no trade: {data['error']}")
                return
            buy_data = data.get('buy', {})
            contract_id = buy_data.get('contract_id')
            buy_price = buy_data.get('buy_price', 0)
            if self.pending_trade:
                amount = self.pending_trade.get('amount', 0)
                action = self.pending_trade.get('contract_type', '')
                logger.info(f"✅ Trade executado! ID: {contract_id}, Preço: ${buy_price}")
                if self.trading_bot:
                    trade_data = {
                        'symbol': self.current_symbol,
                        'action': action,
                        'amount': amount,
                        'price': buy_price,
                        'result': 'pending',
                        'confidence': 70
                    }
                    self.trading_bot.register_trade(trade_data)
                    logger.info(f"📝 Trade registado no histórico")
                if contract_id:
                    self.active_trades[contract_id] = {
                        'contract_id': contract_id,
                        'amount': amount,
                        'buy_price': buy_price,
                        'timestamp': time.time(),
                        'action': action
                    }
                    logger.info(f"📌 Mapeado contract_id {contract_id}")
                if contract_id:
                    self.subscribe_contract(contract_id)
                self.pending_trade = None
        except Exception as e:
            logger.error(f"Erro ao processar compra: {e}")
    
    def subscribe_contract(self, contract_id):
        subscribe_msg = {"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1, "req_id": 200}
        self.ws.send(json.dumps(subscribe_msg))
        logger.info(f"📡 Acompanhando contrato: {contract_id}")
    
    def on_proposal_open_contract(self, data):
        try:
            contract = data.get('proposal_open_contract', {})
            contract_id = contract.get('contract_id')
            if not contract_id:
                return
            if contract.get('is_sold'):
                buy_price = contract.get('buy_price', 0)
                sell_price = contract.get('sell_price', 0)
                profit = sell_price - buy_price
                is_win = profit > 0
                result_data = {
                    'contract_id': contract_id,
                    'buy_price': buy_price,
                    'sell_price': sell_price,
                    'profit': profit,
                    'amount': contract.get('buy_price', 0),
                    'is_win': is_win
                }
                logger.info(f"📊 CONTRATO FECHADO: {'✅ GANHO' if is_win else '❌ PERDA'} de ${abs(profit):.2f}")
                if self.trading_bot:
                    if contract_id in self.active_trades:
                        logger.info(f"📌 Encontrado trade para contract_id {contract_id}")
                    self.trading_bot.on_trade_result(result_data)
                if contract_id in self.active_trades:
                    del self.active_trades[contract_id]
        except Exception as e:
            logger.error(f"Erro ao processar contrato: {e}")
    
    def on_error_response(self, data):
        error = data.get('error', {})
        logger.error(f"API Error: {error.get('message', 'Unknown error')}")
    
    def request_deposit(self, amount, currency, method):
        logger.info(f"💰 Depósito solicitado: ${amount} via {method}")
        return {'status': 'pending', 'message': f'Depósito de ${amount} solicitado.', 'amount': amount, 'method': method}
    
    def request_withdrawal(self, amount, currency, method):
        if amount > self.balance:
            return {'error': 'Saldo insuficiente'}
        logger.info(f"💸 Saque solicitado: ${amount} via {method}")
        return {'status': 'pending', 'message': f'Saque de ${amount} solicitado.', 'amount': amount, 'method': method}
