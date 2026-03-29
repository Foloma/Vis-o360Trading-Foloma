import json
import logging
from datetime import datetime
from collections import deque

logger = logging.getLogger(__name__)

class PaymentSystem:
    """Sistema de gestão de pagamentos (depósitos e levantamentos)"""
    
    def __init__(self, deriv_client):
        self.client = deriv_client
        self.transactions = deque(maxlen=100)
        self.pending_withdrawals = []
        
    def get_deposit_info(self, verification_code=None):
        """Obtém informações para depósito"""
        if not self.client or not self.client.authorized:
            return {'error': 'Não autorizado'}
        
        try:
            request = {
                "cashier": 1,
                "type": "deposit",
                "provider": "cryptocurrency"
            }
            
            if verification_code:
                request["verification_code"] = verification_code
            
            self.client.ws.send(json.dumps(request))
            logger.info("💰 Solicitando informações de depósito...")
            
            transaction = {
                'type': 'deposit',
                'amount': 0,
                'currency': 'USD',
                'timestamp': datetime.now().isoformat(),
                'status': 'solicitado'
            }
            self.transactions.append(transaction)
            
            return {'status': 'solicitado'}
            
        except Exception as e:
            logger.error(f"Erro ao obter informações de depósito: {e}")
            return {'error': str(e)}
    
    def get_withdrawal_info(self, verification_code=None):
        """Obtém informações para levantamento"""
        if not self.client or not self.client.authorized:
            return {'error': 'Não autorizado'}
        
        try:
            request = {
                "cashier": 1,
                "type": "withdraw",
                "provider": "cryptocurrency"
            }
            
            if verification_code:
                request["verification_code"] = verification_code
            
            self.client.ws.send(json.dumps(request))
            logger.info("💰 Solicitando informações de levantamento...")
            
            return {'status': 'solicitado'}
            
        except Exception as e:
            logger.error(f"Erro ao obter informações de levantamento: {e}")
            return {'error': str(e)}
    
    def transfer_between_accounts(self, from_account, to_account, amount, currency):
        """Transfere fundos entre contas do mesmo usuário"""
        if not self.client or not self.client.authorized:
            return {'error': 'Não autorizado'}
        
        try:
            transfer_request = {
                "transfer_between_accounts": 1,
                "account_from": from_account,
                "account_to": to_account,
                "amount": amount,
                "currency": currency
            }
            
            self.client.ws.send(json.dumps(transfer_request))
            
            transaction = {
                'type': 'transfer',
                'from': from_account,
                'to': to_account,
                'amount': amount,
                'currency': currency,
                'timestamp': datetime.now().isoformat(),
                'status': 'pending'
            }
            self.transactions.append(transaction)
            
            logger.info(f"💸 Transferência solicitada: {amount} {currency}")
            return {'status': 'solicitado', 'transaction': transaction}
            
        except Exception as e:
            logger.error(f"Erro na transferência: {e}")
            return {'error': str(e)}
    
    def process_withdrawal(self, amount, currency, withdrawal_method):
        """Processa um pedido de levantamento"""
        if not self.client or not self.client.authorized:
            return {'error': 'Não autorizado'}
        
        if self.client.balance < amount:
            return {'error': 'Saldo insuficiente'}
        
        withdrawal_request = {
            'amount': amount,
            'currency': currency,
            'method': withdrawal_method,
            'timestamp': datetime.now().isoformat(),
            'status': 'pending'
        }
        
        self.pending_withdrawals.append(withdrawal_request)
        
        transaction = {
            'type': 'withdrawal',
            'amount': amount,
            'currency': currency,
            'method': withdrawal_method,
            'timestamp': datetime.now().isoformat(),
            'status': 'pending'
        }
        self.transactions.append(transaction)
        
        logger.info(f"💰 Pedido de levantamento: {amount} {currency}")
        return {'status': 'pendente', 'withdrawal': withdrawal_request}
    
    def get_transaction_history(self, limit=20):
        """Retorna histórico de transações"""
        return list(self.transactions)[-limit:]
    
    def get_payout_currencies(self):
        """Lista as moedas disponíveis para pagamento"""
        if not self.client or not self.client.authorized:
            return {'error': 'Não autorizado'}
        
        try:
            request = {"payout_currencies": 1}
            self.client.ws.send(json.dumps(request))
            logger.info("💱 Solicitando lista de moedas...")
            
            return {'status': 'solicitado'}
            
        except Exception as e:
            logger.error(f"Erro ao obter moedas: {e}")
            return {'error': str(e)}
